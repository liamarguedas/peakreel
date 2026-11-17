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
from peakreel.publish import youtube as yt
from peakreel.publish.metadata import PostMetadata
from peakreel.publish.publishers import ClipPackage, PublishError
from peakreel.publish.queue import process_due, queue_clip
from peakreel.review.store import get_clip, view_of
from tests.conftest import MemoryKeyring, WriteConfig

T0 = 1_795_000_000.0


# -- fake Google ------------------------------------------------------------------------------


@dataclass
class FakeGoogle:
    refresh_tokens: set[str] = field(default_factory=lambda: {"refresh-1"})
    codes: dict[str, str] = field(default_factory=dict[str, str])  # code -> PKCE verifier
    token_calls: int = 0
    uploads: dict[str, bytearray] = field(default_factory=dict[str, bytearray])
    upload_meta: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fail_put_once_at: int | None = None  # drop the connection when this offset arrives
    quota_exceeded: bool = False
    videos: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])


@pytest.fixture
def google() -> Iterator[tuple[FakeGoogle, str]]:
    state = FakeGoogle()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(
            self, code: int, body: object = None, headers: dict[str, str] | None = None
        ) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _authorized(self) -> bool:
            return self.headers.get("Authorization", "").startswith("Bearer access-")

        def do_POST(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/token":
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self._body().decode()).items()}
                state.token_calls += 1
                if form["grant_type"] == "authorization_code":
                    if state.codes.get(form["code"]) is None:
                        self._send(400, {"error": "invalid_grant"})
                        return
                    self._send(200, {"access_token": "access-new", "refresh_token": "refresh-new",
                                     "expires_in": 3600})  # fmt: skip
                    return
                if form["refresh_token"] not in state.refresh_tokens:
                    self._send(
                        400,
                        {
                            "error": "invalid_grant",
                            "error_description": "Token has been expired or revoked.",
                        },
                    )
                    return
                self._send(200, {"access_token": "access-1", "expires_in": 3600})
                return
            if url.path == "/upload":
                assert self._authorized()
                if state.quota_exceeded:
                    self._send(
                        403,
                        {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}},
                    )
                    return
                state.upload_meta.append(json.loads(self._body()))
                session = f"s{len(state.upload_meta)}"
                state.uploads[session] = bytearray()
                host = self.headers["Host"]
                self._send(200, {}, {"Location": f"http://{host}/session/{session}"})
                return
            self._send(404, {})

        def do_PUT(self) -> None:
            session = self.path.rsplit("/", 1)[1]
            data = self._body()
            stored = state.uploads[session]
            content_range = self.headers["Content-Range"]  # "bytes a-b/total" or "bytes */total"
            spec, total = content_range.removeprefix("bytes ").split("/")
            if spec != "*":
                start = int(spec.split("-")[0])
                if state.fail_put_once_at == start:
                    state.fail_put_once_at = None
                    stored.extend(
                        data[: len(data) // 2]
                    )  # half arrived, then the connection dropped
                    self._send(
                        503,
                        {
                            "error": {
                                "message": "backend error",
                                "errors": [{"reason": "backendError"}],
                            }
                        },
                    )
                    return
                assert start == len(stored), (start, len(stored))
                stored.extend(data)
            if len(stored) == int(total):
                video_id = f"vid{session}"
                state.videos[video_id] = {"status": {"uploadStatus": "uploaded"}}
                self._send(200, {"id": video_id})
                return
            headers = {"Range": f"bytes=0-{len(stored) - 1}"} if stored else {}
            self._send(308, None, headers)

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            assert self._authorized()
            if url.path == "/api/channels":
                self._send(200, {"items": [{"id": "UC123", "snippet": {"title": "Clips EN"}}]})
            elif url.path == "/api/videos":
                video = state.videos.get(query["id"][0])
                self._send(200, {"items": [{"id": query["id"][0], **video}] if video else []})
            else:
                self._send(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def api_for(base: str) -> yt.YouTubeApi:
    return yt.YouTubeApi(
        yt.OAuthClient("cid", "secret"), auth_url=f"{base}/auth", token_url=f"{base}/token",
        api_base=f"{base}/api", upload_url=f"{base}/upload",
    )  # fmt: skip


TOKEN = yt.ChannelToken("en", "refresh-1", "UC123", "Clips EN")


# -- credentials and login --------------------------------------------------------------------


def test_client_file_and_credential_store(tmp_path: Path, memory_keyring: MemoryKeyring) -> None:
    good = tmp_path / "client.json"
    good.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "s3"}}), encoding="utf-8"
    )
    client = yt.OAuthClient.from_file(good)
    assert "s3" not in repr(client)
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Desktop app"):
        yt.OAuthClient.from_file(web)

    assert yt.load_client() is None
    yt.save_client(client)
    assert yt.load_client() == client
    yt.save_token(TOKEN)
    assert yt.load_token("en") == TOKEN
    assert "refresh-1" not in repr(TOKEN)
    yt.delete_token("en")
    assert yt.load_token("en") is None


def test_authorize_runs_the_loopback_flow(google: tuple[FakeGoogle, str]) -> None:
    state, base = google
    api = api_for(base)

    def browser(url: str) -> None:
        params = {
            k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
        }
        assert params["access_type"] == "offline"
        assert params["code_challenge_method"] == "S256"
        assert set(params["scope"].split()) == set(yt.SCOPES)
        state.codes["code-1"] = params["code_challenge"]
        redirect = f"{params['redirect_uri']}?code=code-1&state={params['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()  # noqa: S310

    assert yt.authorize(api, open_browser=browser, timeout_s=5) == ("refresh-new", "access-new")

    def forged(url: str) -> None:
        params = {
            k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
        }
        redirect = f"{params['redirect_uri']}?code=code-1&state=attacker"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()  # noqa: S310

    with pytest.raises(PublishError, match="did not match"):
        yt.authorize(api, open_browser=forged, timeout_s=5)


def test_access_token_is_cached_and_expiry_is_explained(google: tuple[FakeGoogle, str]) -> None:
    state, base = google
    api = api_for(base)
    assert api.access_token(TOKEN) == "access-1"
    assert api.access_token(TOKEN) == "access-1"
    assert state.token_calls == 1
    revoked = yt.ChannelToken("es", "refresh-dead", "UC9", "Clips ES")
    with pytest.raises(PublishError, match="In production"):
        api.access_token(revoked)
    assert api.my_channel("access-1") == ("UC123", "Clips EN")


# -- upload -----------------------------------------------------------------------------------


def test_resumable_upload_survives_a_dropped_connection(
    google: tuple[FakeGoogle, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, base = google
    monkeypatch.setattr(yt, "_CHUNK", 1000)
    video = tmp_path / "clip.mp4"
    payload = bytes(range(256)) * 10  # 2560 bytes: 3 chunks
    video.write_bytes(payload)
    state.fail_put_once_at = 1000  # the second chunk is cut off half-way
    video_id = api_for(base).upload("access-1", video, {"snippet": {"title": "t"}})
    assert video_id == "vids1"
    assert bytes(state.uploads["s1"]) == payload  # resumed exactly where YouTube stopped


def test_quota_errors_ask_to_retry_later(google: tuple[FakeGoogle, str], tmp_path: Path) -> None:
    state, base = google
    state.quota_exceeded = True
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    with pytest.raises(PublishError) as caught:
        api_for(base).upload("access-1", video, {})
    assert caught.value.retry_after_s == 3600
    assert caught.value.reason == "quotaExceeded"


def package(tmp_path: Path, account: str = "en") -> ClipPackage:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video" * 100)
    return ClipPackage(1, "c1", video, None, ChannelConfig(name="alpha"), None, [], account=account)


META = PostMetadata(
    "youtube", "Huge play", "Clip from twitch.tv/alpha\n\n#twitch", ("alpha", "twitch")
)


def test_publisher_uploads_a_short_and_follows_processing(
    google: tuple[FakeGoogle, str], tmp_path: Path
) -> None:
    state, base = google
    publisher = yt.YouTubePublisher(api_for(base), tokens={"en": TOKEN}.get)
    clip = package(tmp_path)
    assert publisher.validate(clip, META) == []
    assert "youtube login --account es" in publisher.validate(package(tmp_path, "es"), META)[0]

    result = publisher.publish(clip, META, "public")
    assert (result.state, result.remote_id) == ("processing", "vids1")
    assert result.url == "https://youtube.com/shorts/vids1"
    (resource,) = state.upload_meta
    assert resource["snippet"] == {
        "title": "Huge play",
        "description": META.text,
        "tags": ["alpha", "twitch"],
        "categoryId": "20",
    }
    assert resource["status"] == {
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
    }

    assert publisher.poll_status(clip, "vids1").state == "processing"  # type: ignore[union-attr]
    state.videos["vids1"] = {"status": {"uploadStatus": "processed", "privacyStatus": "public"}}
    assert publisher.poll_status(clip, "vids1").state == "published"  # type: ignore[union-attr]
    state.videos["vids1"]["status"]["privacyStatus"] = "private"
    assert publisher.poll_status(clip, "vids1").state == "private_only"  # type: ignore[union-attr]
    state.videos["vids1"] = {"status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}}
    with pytest.raises(PublishError, match="rejected the video: duplicate"):
        publisher.poll_status(clip, "vids1")
    assert publisher.poll_status(clip, None) is None  # unknown: check by hand
    with pytest.raises(PublishError, match="not on the channel"):
        publisher.poll_status(clip, "missing")
    assert publisher.remove(clip, result.url) is False  # never deletes automatically


# -- queue integration ------------------------------------------------------------------------


def test_only_platforms_with_an_adapter_can_post_through_the_api() -> None:
    assert (
        PublishConfig.model_validate({"modes": {"youtube": "public"}}).mode_for("youtube")
        == "public"
    )
    assert (
        PublishConfig.model_validate({"modes": {"youtube": "public"}}).mode_for("tiktok")
        == "export"
    )
    with pytest.raises(ValidationError, match="tiktok can only be exported"):
        PublishConfig.model_validate({"modes": {"tiktok": "public"}})


@pytest.fixture
def queue_setup(write_config: WriteConfig) -> tuple[Config, sqlite3.Connection, int, int]:
    path = write_config(
        {
            "channels": [
                {"name": "alpha", "language": "en", "permission": "granted"},
                {"name": "beta", "language": "en"},  # permission unknown
            ],
            "publish": {
                "modes": {"youtube": "public"},
                "accounts": [{"id": "en", "languages": ["en"]}],
                "max_attempts": 3,
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
        (folder / "c.mp4").write_bytes(b"video" * 300)
        (folder / "c.json").write_text("{}", encoding="utf-8")
        ids.append(insert_clip(
            conn, channel_id=ensure_channel(conn, name), spike_id=None, start_ts=T0, end_ts=T0 + 30,
            path=folder / "c.mp4", sidecar_path=folder / "c.json",
        ))  # fmt: skip
    conn.execute("UPDATE clips SET title = 'Huge play'")
    conn.commit()
    return config, conn, ids[0], ids[1]


def queue(config: Config, conn: sqlite3.Connection, clip_id: int, now: float) -> list[str]:
    row = get_clip(conn, clip_id)
    assert row is not None
    return queue_clip(config, conn, view_of(row), row, ["youtube"], now=now)


def test_queue_publishes_public_then_polls_until_published(
    google: tuple[FakeGoogle, str], queue_setup: tuple[Config, sqlite3.Connection, int, int]
) -> None:
    state, base = google
    config, conn, granted, unknown = queue_setup
    publishers = {"youtube": yt.YouTubePublisher(api_for(base), tokens={"en": TOKEN}.get)}
    assert queue(config, conn, granted, T0) == []
    assert queue(config, conn, unknown, T0) == ["youtube: private (permission of beta unknown)"]

    done = process_due(config, conn, publishers, now=T0)  # type: ignore[arg-type]
    assert [d.state for d in done] == ["processing"]  # the second waits for its time slot
    assert [m["status"]["privacyStatus"] for m in state.upload_meta] == ["public"]
    row = conn.execute("SELECT * FROM publications WHERE clip_id = ?", (granted,)).fetchone()
    assert (row["state"], row["remote_id"], row["mode"]) == ("processing", "vids1", "public")

    state.videos["vids1"] = {"status": {"uploadStatus": "processed", "privacyStatus": "public"}}
    later = T0 + 31 * 60  # past the 30 min spacing: the second clip is due, the first is polled
    done = process_due(config, conn, publishers, now=later)  # type: ignore[arg-type]
    assert sorted(d.state for d in done) == ["processing", "published"]
    assert [m["status"]["privacyStatus"] for m in state.upload_meta] == ["public", "private"]
    row = conn.execute("SELECT * FROM publications WHERE clip_id = ?", (granted,)).fetchone()
    assert (row["state"], row["url"]) == ("published", "https://youtube.com/shorts/vids1")


def test_quota_postpones_without_using_attempts(
    google: tuple[FakeGoogle, str], queue_setup: tuple[Config, sqlite3.Connection, int, int]
) -> None:
    state, base = google
    config, conn, granted, _ = queue_setup
    state.quota_exceeded = True
    publishers = {"youtube": yt.YouTubePublisher(api_for(base), tokens={"en": TOKEN}.get)}
    queue(config, conn, granted, T0)
    for hour in range(5):  # more than max_attempts (3) quota refusals
        process_due(config, conn, publishers, now=T0 + hour * 3600)  # type: ignore[arg-type]
    row = conn.execute("SELECT * FROM publications WHERE clip_id = ?", (granted,)).fetchone()
    assert (row["state"], row["attempts"]) == ("approved", 0)
    assert "quota" in row["error"]
    state.quota_exceeded = False
    (done,) = process_due(config, conn, publishers, now=T0 + 5 * 3600)  # type: ignore[arg-type]
    assert done.state == "processing"


def test_missing_channel_login_fails_with_the_fix(
    google: tuple[FakeGoogle, str], queue_setup: tuple[Config, sqlite3.Connection, int, int]
) -> None:
    _, base = google
    config, conn, granted, _ = queue_setup
    publishers = {"youtube": yt.YouTubePublisher(api_for(base), tokens=lambda account: None)}
    queue(config, conn, granted, T0)
    (done,) = process_due(config, conn, publishers, now=T0)  # type: ignore[arg-type]
    assert done.state == "failed"
    assert "peakreel youtube login --account en" in done.detail
    # No OAuth client stored at all: the publication fails with a clear reason.
    queue(config, conn, granted, T0)
    (done,) = process_due(config, conn, {}, now=T0)
    assert "no youtube publisher for public mode" in done.detail


def test_approving_never_waits_for_an_upload(
    google: tuple[FakeGoogle, str], queue_setup: tuple[Config, sqlite3.Connection, int, int]
) -> None:
    state, _ = google
    config, conn, granted, _ = queue_setup
    from peakreel.review.store import update_clip

    update_clip(config, conn, granted, state="approved", platforms=["youtube", "tiktok"])
    rows = {
        r["platform"]: r["state"]
        for r in conn.execute("SELECT * FROM publications WHERE clip_id = ?", (granted,))
    }
    assert rows == {"youtube": "approved", "tiktok": "exported"}  # export now, upload later
    assert state.upload_meta == []


def test_review_server_runs_the_queue_in_the_background(
    queue_setup: tuple[Config, sqlite3.Connection, int, int],
) -> None:
    import time

    from fastapi.testclient import TestClient

    from peakreel.review.app import create_app

    config, conn, granted, _ = queue_setup
    queue_clip_row = get_clip(conn, granted)
    assert queue_clip_row is not None
    queue_clip(
        config, conn, view_of(queue_clip_row), queue_clip_row, ["tiktok"], now=time.time() + 0.3
    )
    conn.execute("UPDATE publications SET next_attempt_at = ?", (time.time() + 0.3,))
    conn.commit()
    state = "approved"
    with TestClient(create_app(config, publish_interval_s=0.1), base_url="http://127.0.0.1"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = conn.execute("SELECT state FROM publications").fetchone()[0]
            if state == "exported":
                break
            time.sleep(0.05)
    assert state == "exported"
