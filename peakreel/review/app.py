"""FastAPI app behind ``peakreel review``.

Security model
--------------
The page binds to ``127.0.0.1`` by default and then needs no login. Once it can be
reached from elsewhere (another ``review.host``, or ``review.public_hosts`` such as
a Tailscale name) every request needs a session from :mod:`peakreel.review.auth`,
and the app refuses to start without a password. Two browser-based attacks reach
even a localhost service and are blocked here:

- **Cross-site requests**: any web page can make the browser POST to localhost.
  Every mutation requires a JSON body; a cross-origin JSON request needs a CORS
  preflight, which this app never grants.
- **DNS rebinding**: a hostile domain re-pointed at 127.0.0.1. The ``Host`` header
  must be ``127.0.0.1`` or ``localhost``, the configured host, or a public host.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from peakreel.backup import backup_db
from peakreel.blocklist import load_blocklist
from peakreel.config import PLATFORMS, Config, ConfigError, Platform
from peakreel.db import connect, init_db
from peakreel.log import get_logger
from peakreel.publish.promote import promote_once
from peakreel.publish.queue import Edit, clip_route, posts_preview, process_due
from peakreel.publish.stats import read_stats_once
from peakreel.review.activity import read_activity
from peakreel.review.auth import Credentials, LoginThrottle, load_credentials
from peakreel.review.auto import auto_review_once
from peakreel.review.dashboard import dashboard, schedule, uploads
from peakreel.review.gemini import exhausted_until
from peakreel.review.store import (
    ClipView,
    RejectReason,
    ReviewError,
    block_channels,
    get_clip,
    list_clips,
    update_clip,
    view_of,
)

log = get_logger(__name__)

__all__ = ["ClipUpdate", "create_app"]

_STATIC = Path(__file__).with_name("static")
_COOKIE = "peakreel_session"
_OPEN_PATHS = frozenset({"/login", "/api/login"})
_CREDENTIALS_TTL_S = 10.0  # a new password (or its new session key) applies within this


class Login(BaseModel):
    """Body of ``POST /api/login``."""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=1000)


class PipelineSwitch(BaseModel):
    """Recording on or off, from the page's header button."""

    model_config = ConfigDict(extra="forbid")

    on: bool


# status.json is rewritten every 30 s; older than this, the pipeline is gone.
_STATUS_STALE_S = 120.0


def _pipeline_running(data: dict[str, Any]) -> bool:
    """Whether the status file says running and is fresh (a killed pipeline says running)."""
    try:
        updated = datetime.fromisoformat(str(data["updated_utc"])).timestamp()
    except (KeyError, ValueError):
        return False
    return bool(data.get("running")) and time.time() - updated < _STATUS_STALE_S


class PostEdit(BaseModel):
    """The reviewer's edited post text for one platform."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    text: str | None = Field(default=None, max_length=10000)


class ClipUpdate(BaseModel):
    """Body of ``POST /api/clips/{id}``. Omitted fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["new", "approved", "rejected"] | None = None
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    platforms: list[Platform] | None = None
    posts: dict[Platform, PostEdit] | None = None
    language: str | None = Field(default=None, pattern=r"^([a-z]{2,3})?$")
    reason: RejectReason | None = None


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def create_app(
    config: Config, *, bind: str | None = None, publish_interval_s: float = 30.0
) -> FastAPI:
    """Build the review app for ``config``.

    Parameters
    ----------
    config : Config
    bind : str, optional
        The interface the server binds (``--host``), default ``review.host``. With
        :meth:`~peakreel.config.ReviewConfig.login_required` it decides whether
        the page needs a password.
    publish_interval_s : float, default 30.0
        How often the background loop processes the publication queue (scheduled
        API posts, platform processing) while the app runs.

    Returns
    -------
    fastapi.FastAPI

    Raises
    ------
    peakreel.config.ConfigError
        If the page needs a login and no password is set.
    """
    login_required = config.review.login_required(bind)
    creds = load_credentials() if login_required else None
    if login_required and creds is None:
        raise ConfigError(
            "the review page can be reached from other devices (review.host or "
            "review.public_hosts), so it needs a password: run `peakreel review password`"
        )
    session_s = config.review.session_days * 86400
    throttle = LoginThrottle()
    cache: dict[str, Any] = {"creds": creds, "at": time.monotonic()}

    def credentials() -> Credentials | None:
        """Return the current login, re-read now and then so a new password applies live."""
        if cache["creds"] is not None and time.monotonic() - cache["at"] > _CREDENTIALS_TTL_S:
            # If the store is unreadable, keep the previous key: never fail open.
            cache["creds"] = load_credentials() or cache["creds"]
            cache["at"] = time.monotonic()
        current: Credentials | None = cache["creds"]
        return current

    init_db(config.db_path)

    def publish_once(*, stats: bool = True) -> None:
        conn = connect(config.db_path)
        try:
            for done in process_due(config, conn):
                log.info("publication", clip_id=done.clip_id, account=done.account,
                         platform=done.platform, state=done.state, detail=done.detail)  # fmt: skip
            if stats:
                try:
                    read_stats_once(conn)  # numbers 3, 12, 24 and 72 hours after posting
                    promote_once(config, conn)  # Instagram posts doing well: YouTube, Facebook
                except Exception:  # never let a stats read stop publishing
                    log.exception("reading post stats or promoting failed")
                try:
                    backup_db(config)  # once a day; this page runs even when recording is off
                except Exception:
                    log.exception("database backup failed")
        finally:
            conn.close()

    def review_once() -> None:
        conn = connect(config.db_path)
        try:
            auto_review_once(config, conn)
        finally:
            conn.close()

    # Resolve publications interrupted by a crash, and anything already due. Auto review
    # waits for the background loop: Gemini calls must not delay opening the page.
    publish_once(stats=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async def loop(name: str, work: Callable[[], None]) -> None:
            while True:
                await asyncio.sleep(publish_interval_s)
                try:
                    await asyncio.to_thread(work)
                except Exception:
                    log.exception(f"{name} pass failed")

        # Scheduled API posts and platform processing run while the review page runs
        # (or by `peakreel publish run --watch`). Auto review has its own loop: an
        # upload can take minutes, and clips must not wait for it to be decided.
        tasks = [
            asyncio.create_task(loop("publishing", publish_once), name="publisher"),
            asyncio.create_task(loop("auto review", review_once), name="auto-review"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = FastAPI(
        title="Peakreel review", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.middleware("http")
    async def require_login(request: Request, call_next: Any) -> Response:
        current = credentials()
        if (
            current is None
            or request.url.path in _OPEN_PATHS
            or current.session_valid(request.cookies.get(_COOKIE), time.time())
        ):
            response: Response = await call_next(request)
            return response
        if request.url.path.startswith(("/api/", "/media/")):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    # Added last, so it runs first: unknown hosts are refused before anything else.
    hosts = {"127.0.0.1", "localhost", config.review.host, *config.review.public_hosts}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(hosts))

    @contextmanager
    def db() -> Generator[sqlite3.Connection]:
        conn = connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def ai_hold() -> float | None:
        """How long To review holds a clip back for auto review (``None``: no hold)."""
        auto = config.review.auto
        return (auto.ai_wait_min + 15) * 60 if auto.mode == "on" else None

    def with_route(conn: sqlite3.Connection, view: ClipView) -> dict[str, Any]:
        """Return the clip as JSON with where it would be posted (language, account)."""
        item = view.to_json()
        row = get_clip(conn, view.id)
        if row is not None:
            item["route"] = clip_route(config, view, row).to_json()
        return item

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{name}")
    def static_file(name: Literal["app.js", "app.css"]) -> FileResponse:
        """Serve the review page's script and styles (behind the login, like the page)."""
        media_type = "text/javascript" if name.endswith(".js") else "text/css"
        # no-cache: the browser checks for a newer file on every load (an update shows
        # on a plain reload), and reuses its copy when unchanged.
        return FileResponse(_STATIC / name, media_type=media_type,
                            headers={"Cache-Control": "no-cache"})  # fmt: skip

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        return (_STATIC / "login.html").read_text(encoding="utf-8")

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
        current = credentials()
        if current is None:
            return {"ok": True}  # no login on this machine-only page
        now = time.time()
        wait = throttle.retry_after(now)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong passwords. Try again in {-(-wait // 60)} min.",
                headers={"Retry-After": str(wait)},
            )
        if not current.check_password(body.password):
            throttle.failed(now)
            log.warning("review login failed", client=request.client and request.client.host)
            raise HTTPException(status_code=401, detail="Wrong password.")
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(
            _COOKIE,
            current.new_session(now, session_s),
            max_age=int(session_s),
            path="/",
            httponly=True,
            samesite="strict",
            secure=https,
        )
        log.info("review login", client=request.client and request.client.host)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(_COOKIE, path="/", httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/activity")
    def activity() -> list[dict[str, str]]:
        return read_activity(config.log_file)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        path = config.run_dir / "status.json"
        if not path.exists():
            return {"running": False}
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        data["running"] = _pipeline_running(data)
        data["stopping"] = data["running"] and (config.run_dir / "pipeline.stop").exists()
        # The AI models out of quota (asked from this process), and until when.
        data["models_out"] = {m: _iso_utc(t) for m, t in exhausted_until().items()}
        data["models"] = list(config.review.auto.gemini_models)
        return data

    @app.post("/api/pipeline")
    def pipeline_switch(switch: PipelineSwitch) -> dict[str, bool]:
        # Off: the pipeline's own stop file (`peakreel run stop`), so clips in flight
        # are finished. On: `peakreel run start --detach`, as the start-up script does.
        # Publishing, stats and auto review run here, in the review app, either way.
        stop_file = config.run_dir / "pipeline.stop"
        if not switch.on:
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.touch()
        else:
            status_path = config.run_dir / "status.json"
            data: dict[str, Any] = (
                json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
            )
            if _pipeline_running(data):
                raise HTTPException(409, "still running or stopping; try again in a minute")
            stop_file.unlink(missing_ok=True)
            subprocess.Popen(
                [sys.executable, "-m", "peakreel", "run", "start", "--detach"],
                cwd=config.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        log.info("recording switched from the page", on=switch.on)
        return {"ok": True}

    @app.get("/api/clips")
    def clips(
        state: Literal[
            "new", "checking", "duplicate", "approved", "rejected", "blocked", "published", "audit"
        ]
        | None = None,
    ) -> list[dict[str, Any]]:
        with db() as conn:
            try:
                block_channels(config, conn, load_blocklist(config.blocklist_path))
            except ConfigError:
                log.exception("blocklist unreadable; approvals are refused until it is fixed")
            return [with_route(conn, v) for v in list_clips(conn, state, ai_hold_s=ai_hold())]

    @app.post("/api/clips/{clip_id}")
    def update(clip_id: int, body: ClipUpdate) -> dict[str, Any]:
        with db() as conn:
            try:
                view = update_clip(
                    config,
                    conn,
                    clip_id,
                    state=body.state,
                    title=body.title,
                    description=body.description,
                    platforms=body.platforms,
                    language=body.language,
                    reason=body.reason,
                    posts=None
                    if body.posts is None
                    else {k: Edit(v.title, v.text) for k, v in body.posts.items()},
                )
            except ReviewError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return with_route(conn, view)

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return {
            "platforms": list(PLATFORMS),
            "enabled": list(config.publish.platforms),
            "mode": config.publish.mode,
            "modes": {p: config.publish.mode_for(p) for p in PLATFORMS},
            "login": cache["creds"] is not None,
            "names": {
                "youtube": "YouTube",
                "tiktok": "TikTok",
                "instagram": "Instagram",
                "facebook": "Facebook",
            },
            "accounts": [
                {
                    "id": a.id,
                    "label": a.label,
                    "languages": list(a.languages),
                    "platforms": list(a.platforms),
                    "instagram": a.handles.instagram,
                }
                for a in config.publish.accounts
            ],
        }

    @app.get("/api/clips/{clip_id}")
    def clip_one(clip_id: int) -> dict[str, Any]:
        with db() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such clip")
            return with_route(conn, view_of(row, conn))

    @app.get("/api/clips/{clip_id}/posts")
    def posts(
        clip_id: int, language: Annotated[str | None, Query(pattern=r"^[a-z]{2,3}$")] = None
    ) -> dict[str, Any]:
        with db() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such clip")
            return posts_preview(config, conn, view_of(row), row, language)

    @app.get("/api/uploads")
    def uploads_view() -> dict[str, Any]:
        with db() as conn:
            return uploads(conn, config)

    @app.get("/api/schedule")
    def queue_view() -> dict[str, Any]:
        with db() as conn:
            languages = {a.id: a.languages[0] for a in config.publish.accounts if a.languages}
            return schedule(conn, hold_s=ai_hold(), languages=languages)

    @app.get("/api/dashboard")
    def stats() -> dict[str, Any]:
        with db() as conn:
            return dashboard(conn)

    @app.get("/media/{clip_id}/{kind}")
    def media(clip_id: int, kind: Literal["vertical", "horizontal", "thumbnail"]) -> FileResponse:
        with db() as conn:
            row = get_clip(conn, clip_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such clip")
        column = {"vertical": "vertical_path", "horizontal": "path", "thumbnail": "thumbnail_path"}[
            kind
        ]
        value = row[column]
        if not value:
            raise HTTPException(status_code=404, detail=f"clip has no {kind}")
        path = Path(value).resolve()
        if config.data_dir.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail=f"{kind} file missing")
        media_type = "image/jpeg" if kind == "thumbnail" else "video/mp4"
        return FileResponse(path, media_type=media_type)  # supports Range (seeking)

    return app
