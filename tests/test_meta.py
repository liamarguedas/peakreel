from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from peakreel.config import ChannelConfig, Config, PublishConfig, load_config
from peakreel.db import connect, ensure_channel, init_db, insert_clip
from peakreel.publish import meta
from peakreel.publish.metadata import PostMetadata
from peakreel.publish.publishers import ClipPackage, PublishError
from peakreel.publish.queue import process_due, queue_clip
from peakreel.review.store import get_clip, view_of
from tests.conftest import MemoryKeyring, WriteConfig

T0 = 1_795_000_000.0


@dataclass
class FakeGraph:
    containers: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    reels: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    uploads: dict[str, bytes] = field(default_factory=dict[str, bytes])
    publishes: list[str] = field(default_factory=list[str])
    finishes: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    error: dict[str, Any] | None = None  # next response is this Graph error
    declined: tuple[str, ...] = ()  # permissions the user did not grant


@pytest.fixture
def graph() -> Iterator[tuple[FakeGraph, str]]:
    state = FakeGraph()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, code: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> tuple[list[str], dict[str, str]]:
            url = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            return url.path.strip("/").split("/"), query

        def _error(self) -> bool:
            if state.error is None:
                return False
            error, state.error = state.error, None
            self._send(400, {"error": error})
            return True

        def do_GET(self) -> None:
            parts, query = self._route()
            if self._error():
                return
            host = self.headers["Host"]
            if parts == ["oauth", "access_token"]:
                if "code" in query:
                    assert query["redirect_uri"].startswith("http://localhost:")
                    self._send(200, {"access_token": "short"})
                else:
                    assert query["fb_exchange_token"] == "short"
                    self._send(200, {"access_token": "long-user"})
                return
            if parts == ["me", "permissions"]:
                assert query["access_token"] == "long-user"
                self._send(200, {"data": [
                    {"permission": p, "status": "declined" if p in state.declined else "granted"}
                    for p in (*meta.SCOPES, "public_profile")
                ]})  # fmt: skip
                return
            if parts == ["me", "accounts"]:
                assert query["access_token"] == "long-user"
                self._send(200, {"data": [
                    {"id": "P1", "name": "Clips EN", "access_token": "page-en",
                     "instagram_business_account": {"id": "IG1", "username": "clips_en"}},
                    {"id": "P2", "name": "Clips ES", "access_token": "page-es"},
                ]})  # fmt: skip
                return
            target = parts[0]
            if query.get("fields") == "status_code,status":
                container = state.containers[target]
                self._send(
                    200, {"status_code": container["status"], "status": container.get("detail", "")}
                )
            elif query.get("fields") == "permalink":
                self._send(200, {"permalink": f"https://www.instagram.com/reel/{target}/"})
            elif query.get("fields") == "status":
                self._send(200, {"status": state.reels[target]["status"]})
            else:
                self._send(404, {"error": {"message": f"no route {self.path} {host}"}})

        def do_POST(self) -> None:
            parts, query = self._route()
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self._error():
                return
            host = self.headers["Host"]
            if parts[0] in ("ig-upload", "video-upload"):
                assert self.headers["Authorization"].startswith("OAuth page-")
                assert self.headers["offset"] == "0"
                assert int(self.headers["file_size"]) == len(body)
                state.uploads[parts[1]] = body
                if parts[0] == "ig-upload":
                    state.containers[parts[1]]["status"] = "IN_PROGRESS"
                self._send(200, {"success": True})
                return
            if parts[1:] == ["media"]:
                assert query["media_type"] == "REELS"
                assert query["upload_type"] == "resumable"
                container = f"C{len(state.containers) + 1}"
                state.containers[container] = {"status": "IN_PROGRESS", "caption": query["caption"]}
                self._send(200, {"id": container, "uri": f"http://{host}/ig-upload/{container}"})
                return
            if parts[1:] == ["media_publish"]:
                container = state.containers[query["creation_id"]]
                assert container["status"] == "FINISHED", "published before processing finished"
                container["status"] = "PUBLISHED"
                state.publishes.append(query["creation_id"])
                self._send(200, {"id": f"M{len(state.publishes)}"})
                return
            if parts[1:] == ["video_reels"]:
                if query["upload_phase"] == "start":
                    video = f"V{len(state.reels) + 1}"
                    state.reels[video] = {"status": {"video_status": "upload_complete"}}
                    self._send(
                        200,
                        {"video_id": video, "upload_url": f"http://{host}/video-upload/{video}"},
                    )
                else:
                    state.finishes.append(query)
                    self._send(200, {"success": True})
                return
            self._send(404, {"error": {"message": f"no route {self.path}"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def api_for(base: str) -> meta.MetaApi:
    return meta.MetaApi(meta.MetaApp("123", "secret"), graph_url=base, dialog_url=f"{base}/dialog")


EN = meta.PageToken("en", "P1", "Clips EN", "page-en", "IG1", "clips_en")
ES_NO_INSTAGRAM = meta.PageToken("es", "P2", "Clips ES", "page-es", None, None)
TOKENS = {"en": EN, "es": ES_NO_INSTAGRAM}.get
META = PostMetadata("instagram", None, "Huge play\n\nClip from twitch.tv/alpha", ())


def package(tmp_path: Path, account: str = "en") -> ClipPackage:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"reel" * 500)
    return ClipPackage(1, "c1", video, None, ChannelConfig(name="alpha"), None, [], account=account)


# -- sign-in and credentials ------------------------------------------------------------------


def test_sign_in_names_permissions_meta_did_not_grant(
    graph: tuple[FakeGraph, str], memory_keyring: MemoryKeyring
) -> None:
    # Seen live: an app without the Instagram permissions enabled granted only
    # pages_show_list, and the Page then looked as if it had no Instagram account.
    state, base = graph
    state.declined = ("instagram_basic", "instagram_content_publish")
    api = api_for(base)

    def browser(url: str) -> None:
        params = {
            k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
        }
        redirect = f"{params['redirect_uri']}?code=abc&state={params['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()  # noqa: S310

    missing = "did not grant: instagram_basic, instagram_content_publish"
    with pytest.raises(PublishError, match=missing):
        meta.sign_in(api, open_browser=browser, port=0, timeout_s=5)


def test_sign_in_returns_pages_with_instagram(
    graph: tuple[FakeGraph, str], memory_keyring: MemoryKeyring
) -> None:
    _, base = graph
    api = api_for(base)

    def browser(url: str) -> None:
        params = {
            k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
        }
        assert set(params["scope"].split(",")) == set(meta.SCOPES)
        assert params["redirect_uri"].startswith("http://localhost:")
        redirect = f"{params['redirect_uri']}?code=abc&state={params['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()  # noqa: S310

    user_token, pages = meta.sign_in(api, open_browser=browser, port=0, timeout_s=5)
    business = meta.MetaApi(meta.MetaApp("123", "s", "cfg9"), graph_url=base)
    assert "config_id=cfg9" in business.consent_url("http://localhost:1/", "st")
    assert "scope=" not in business.consent_url("http://localhost:1/", "st")
    assert user_token == "long-user"
    token = meta.page_token_from("en", pages[0])
    assert token == EN
    assert "page-en" not in repr(token)
    assert meta.page_token_from("es", pages[1]).instagram_id is None

    meta.save_app(meta.MetaApp("123", "s", "cfg9"))
    assert meta.load_app() == meta.MetaApp("123", "s", "cfg9")
    meta.save_page(token)
    assert meta.load_page("en") == token
    meta.delete_page("en")
    assert meta.load_page("en") is None


def test_graph_errors_explain_what_to_do(graph: tuple[FakeGraph, str], tmp_path: Path) -> None:
    state, base = graph
    publisher = meta.FacebookPublisher(api_for(base), tokens=TOKENS)
    state.error = {"message": "Error validating access token", "code": 190}
    with pytest.raises(PublishError, match="meta login"):
        publisher.publish(package(tmp_path), META, "public")
    state.error = {"message": "Application request limit reached", "code": 4}
    with pytest.raises(PublishError) as caught:
        publisher.publish(package(tmp_path), META, "public")
    assert caught.value.retry_after_s == 3600


# -- Instagram --------------------------------------------------------------------------------


def test_instagram_uploads_then_publishes_once_processed(
    graph: tuple[FakeGraph, str], tmp_path: Path
) -> None:
    state, base = graph
    publisher = meta.InstagramPublisher(api_for(base), tokens=TOKENS)
    clip = package(tmp_path)
    assert publisher.validate(clip, META) == []
    assert "no Instagram account linked" in publisher.validate(package(tmp_path, "es"), META)[0]
    with pytest.raises(PublishError, match="no private posts"):
        publisher.publish(clip, META, "private")

    result = publisher.publish(clip, META, "public")
    assert (result.state, result.remote_id) == ("processing", "C1")
    assert state.uploads["C1"] == clip.video.read_bytes()  # direct upload, no public URL
    assert state.containers["C1"]["caption"] == META.text

    assert publisher.poll_status(clip, "C1").state == "processing"  # type: ignore[union-attr]
    assert state.publishes == []  # never published before processing finished
    state.containers["C1"]["status"] = "FINISHED"
    done = publisher.poll_status(clip, "C1")
    assert done is not None
    assert (done.state, done.remote_id, done.url) == (
        "published", "M1", "https://www.instagram.com/reel/M1/",
    )  # fmt: skip
    # The process died before recording it: polling again sees PUBLISHED, never publishes twice.
    again = publisher.poll_status(clip, "C1")
    assert again is not None
    assert again.state == "published"
    assert state.publishes == ["C1"]

    state.containers["C1"] = {"status": "ERROR", "detail": "Video aspect ratio not supported"}
    with pytest.raises(PublishError, match="aspect ratio"):
        publisher.poll_status(clip, "C1")
    state.containers["C1"] = {"status": "EXPIRED"}
    with pytest.raises(PublishError, match="expired"):
        publisher.poll_status(clip, "C1")


# -- Facebook ---------------------------------------------------------------------------------


def test_facebook_reels_publish_or_stay_draft(graph: tuple[FakeGraph, str], tmp_path: Path) -> None:
    state, base = graph
    publisher = meta.FacebookPublisher(api_for(base), tokens=TOKENS)
    clip = package(tmp_path)
    result = publisher.publish(clip, META, "public")
    assert (result.state, result.remote_id, result.url) == (
        "processing", "V1", "https://www.facebook.com/reel/V1",
    )  # fmt: skip
    assert state.uploads["V1"] == clip.video.read_bytes()
    assert state.finishes[0]["video_state"] == "PUBLISHED"
    assert state.finishes[0]["description"] == META.text
    assert publisher.poll_status(clip, "V1").state == "processing"  # type: ignore[union-attr]
    state.reels["V1"]["status"] = {
        "video_status": "ready",
        "processing_phase": {"status": "complete"},
        "publishing_phase": {"status": "complete", "publish_status": "published"},
    }
    assert publisher.poll_status(clip, "V1").state == "published"  # type: ignore[union-attr]

    draft = publisher.publish(clip, META, "private")
    assert draft.remote_id == "draft:V2"
    assert state.finishes[1]["video_state"] == "DRAFT"
    state.reels["V2"]["status"] = {
        "video_status": "ready",
        "processing_phase": {"status": "complete"},
    }
    assert publisher.poll_status(clip, "draft:V2").state == "private_only"  # type: ignore[union-attr]
    state.reels["V2"]["status"] = {"video_status": "error", "processing_phase": {"status": "error"}}
    with pytest.raises(PublishError, match="could not process"):
        publisher.poll_status(clip, "draft:V2")


# -- configuration and queue ------------------------------------------------------------------


def test_instagram_has_no_private_mode() -> None:
    with pytest.raises(ValidationError, match="instagram has no private posts"):
        PublishConfig.model_validate({"modes": {"instagram": "private"}})
    settings = PublishConfig.model_validate(
        {"modes": {"instagram": "public", "facebook": "private"}}
    )
    assert (settings.mode_for("instagram"), settings.mode_for("facebook")) == ("public", "private")


@pytest.fixture
def queue_setup(write_config: WriteConfig) -> tuple[Config, sqlite3.Connection, int, int]:
    path = write_config(
        {
            "channels": [
                {"name": "alpha", "language": "en", "permission": "granted"},
                {"name": "beta", "language": "en"},
            ],
            "publish": {
                "modes": {"instagram": "public", "facebook": "public"},
                "accounts": [{"id": "en", "languages": ["en"]}],
                "instagram": {"min_spacing_min": 0},
                "facebook": {"min_spacing_min": 0},
            },
        }
    )
    config = load_config(path)
    init_db(config.db_path)
    conn = connect(config.db_path)
    ids: list[int] = []
    for name in ("alpha", "beta"):
        folder = config.outbox_dir / name
        folder.mkdir(parents=True)
        (folder / "c.mp4").write_bytes(b"reel" * 300)
        (folder / "c.json").write_text("{}", encoding="utf-8")
        ids.append(insert_clip(
            conn, channel_id=ensure_channel(conn, name), spike_id=None, start_ts=T0, end_ts=T0 + 30,
            path=folder / "c.mp4", sidecar_path=folder / "c.json",
        ))  # fmt: skip
    conn.execute("UPDATE clips SET title = 'Huge play'")
    conn.commit()
    return config, conn, ids[0], ids[1]


def test_queue_posts_to_instagram_and_facebook(
    graph: tuple[FakeGraph, str], queue_setup: tuple[Config, sqlite3.Connection, int, int]
) -> None:
    state, base = graph
    config, conn, granted, unknown = queue_setup
    api = api_for(base)
    publishers = {
        "instagram": meta.InstagramPublisher(api, tokens=TOKENS),
        "facebook": meta.FacebookPublisher(api, tokens=TOKENS),
    }
    for clip_id in (granted, unknown):
        row = get_clip(conn, clip_id)
        assert row is not None
        notes = queue_clip(config, conn, view_of(row), row, ["instagram", "facebook"], now=T0)
        if clip_id == unknown:
            assert notes == [
                "instagram: export (permission of beta unknown)",
                "facebook: private (permission of beta unknown)",
            ]
    done = process_due(config, conn, publishers, now=T0)  # type: ignore[arg-type]
    by = {(d.clip_id, d.platform): d.state for d in done}
    assert by == {
        (granted, "instagram"): "processing",
        (granted, "facebook"): "processing",
        (unknown, "instagram"): "exported",  # never public without permission
        (unknown, "facebook"): "processing",  # uploaded as a draft
    }
    assert [f["video_state"] for f in state.finishes] == ["PUBLISHED", "DRAFT"]

    state.containers["C1"]["status"] = "FINISHED"
    for video in state.reels.values():
        video["status"] = {
            "video_status": "ready",
            "processing_phase": {"status": "complete"},
            "publishing_phase": {"status": "complete"},
        }
    done = process_due(config, conn, publishers, now=T0 + 61)  # type: ignore[arg-type]
    by = {(d.clip_id, d.platform): d.state for d in done}
    assert by == {
        (granted, "instagram"): "published",
        (granted, "facebook"): "published",
        (unknown, "facebook"): "private_only",
    }
    assert state.publishes == ["C1"]
