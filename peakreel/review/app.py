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
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from peakreel.blocklist import load_blocklist
from peakreel.config import PLATFORMS, Config, ConfigError, Platform
from peakreel.db import connect, init_db
from peakreel.log import get_logger
from peakreel.publish.queue import Edit, clip_route, posts_preview, process_due
from peakreel.publish.stats import read_stats_once
from peakreel.review.activity import read_activity
from peakreel.review.auth import Credentials, LoginThrottle, load_credentials
from peakreel.review.auto import auto_review_once
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

    def publish_once(*, review: bool = True) -> None:
        conn = connect(config.db_path)
        try:
            for done in process_due(config, conn):
                log.info("publication", clip_id=done.clip_id, account=done.account,
                         platform=done.platform, state=done.state, detail=done.detail)  # fmt: skip
            # After publishing: Gemini can be slow, and its approvals post next pass.
            if review:
                auto_review_once(config, conn)
                try:
                    read_stats_once(conn)  # numbers 12, 24 and 72 hours after posting
                except Exception:  # never let a stats read stop publishing
                    log.exception("reading post stats failed")
        finally:
            conn.close()

    # Resolve publications interrupted by a crash, and anything already due. Auto review
    # waits for the background loop: Gemini calls must not delay opening the page.
    publish_once(review=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async def loop() -> None:
            # Scheduled API posts and platform processing are handled here, while the
            # review page runs (or by `peakreel publish run --watch`).
            while True:
                await asyncio.sleep(publish_interval_s)
                try:
                    await asyncio.to_thread(publish_once)
                except Exception:
                    log.exception("publishing pass failed")

        task = asyncio.create_task(loop(), name="publisher")
        try:
            yield
        finally:
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
        return data

    @app.get("/api/clips")
    def clips(
        state: Literal["new", "duplicate", "approved", "rejected", "blocked", "published", "audit"]
        | None = None,
    ) -> list[dict[str, Any]]:
        with db() as conn:
            try:
                block_channels(config, conn, load_blocklist(config.blocklist_path))
            except ConfigError:
                log.exception("blocklist unreadable; approvals are refused until it is fixed")
            return [with_route(conn, view) for view in list_clips(conn, state)]

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
                }
                for a in config.publish.accounts
            ],
        }

    @app.get("/api/clips/{clip_id}/posts")
    def posts(
        clip_id: int, language: Annotated[str | None, Query(pattern=r"^[a-z]{2,3}$")] = None
    ) -> dict[str, Any]:
        with db() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such clip")
            return posts_preview(config, conn, view_of(row), row, language)

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
