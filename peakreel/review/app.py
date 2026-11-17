"""FastAPI app behind ``peakreel review``.

Security model
--------------
The tool has no login, so it binds to ``127.0.0.1`` by default. Two browser-based
attacks still reach a localhost service and are blocked here:

- **Cross-site requests**: any web page can make the browser POST to localhost.
  Every mutation requires a JSON body; a cross-origin JSON request needs a CORS
  preflight, which this app never grants.
- **DNS rebinding**: a hostile domain re-pointed at 127.0.0.1. The ``Host`` header
  must be ``127.0.0.1`` or ``localhost`` (or the configured host).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from peakreel.blocklist import load_blocklist
from peakreel.config import PLATFORMS, Config, ConfigError, Platform
from peakreel.db import connect, init_db
from peakreel.log import get_logger
from peakreel.publish.queue import Edit, posts_preview, process_due
from peakreel.review.store import (
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


def create_app(config: Config) -> FastAPI:
    """Build the review app for ``config``.

    Parameters
    ----------
    config : Config

    Returns
    -------
    fastapi.FastAPI
    """
    init_db(config.db_path)
    conn = connect(config.db_path)
    try:
        # Resolve publications interrupted by a crash, and anything due.
        for done in process_due(config, conn):
            log.info("publication resumed", clip_id=done.clip_id, platform=done.platform,
                     state=done.state)  # fmt: skip
    finally:
        conn.close()
    app = FastAPI(title="Peakreel review", docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {"127.0.0.1", "localhost", config.review.host}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(hosts))

    @contextmanager
    def db() -> Generator[sqlite3.Connection]:
        conn = connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        path = config.run_dir / "status.json"
        if not path.exists():
            return {"running": False}
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    @app.get("/api/clips")
    def clips(
        state: Literal["new", "duplicate", "approved", "rejected", "blocked"] | None = None,
    ) -> list[dict[str, Any]]:
        with db() as conn:
            try:
                block_channels(config, conn, load_blocklist(config.blocklist_path))
            except ConfigError:
                log.exception("blocklist unreadable; approvals are refused until it is fixed")
            return [view.to_json() for view in list_clips(conn, state)]

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
                    posts=None
                    if body.posts is None
                    else {k: Edit(v.title, v.text) for k, v in body.posts.items()},
                )
            except ReviewError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return view.to_json()

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return {
            "platforms": list(PLATFORMS),
            "enabled": list(config.publish.platforms),
            "mode": config.publish.mode,
            "names": {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"},
        }

    @app.get("/api/clips/{clip_id}/posts")
    def posts(clip_id: int) -> dict[str, Any]:
        with db() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such clip")
            return posts_preview(config, conn, view_of(row), row)

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
