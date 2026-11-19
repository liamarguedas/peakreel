from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from peakreel.config import ChannelConfig, load_config
from peakreel.publish import instagram_login as ig
from peakreel.publish.meta import InstagramLogin, InstagramPublisher, MetaApi, MetaApp, PageToken
from peakreel.publish.metadata import PostMetadata
from peakreel.publish.publishers import ClipPackage, PublishError
from peakreel.review.app import create_app
from tests.conftest import MemoryKeyring, WriteConfig

PUBLIC = "https://pc.tail1.ts.net:8443"
APP = MetaApp("777", "ig-secret")
META = PostMetadata("instagram", None, "bro did NOT see that", ())


@dataclass
class FakeInstagram:
    containers: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    publishes: list[str] = field(default_factory=list[str])
    refreshed: list[str] = field(default_factory=list[str])


@pytest.fixture
def instagram() -> Iterator[tuple[FakeInstagram, str]]:
    state = FakeInstagram()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> tuple[list[str], dict[str, str]]:
            url = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            return url.path.strip("/").split("/"), query

        def do_GET(self) -> None:
            parts, query = self._route()
            if parts == ["access_token"]:
                assert (query["grant_type"], query["access_token"]) == (
                    "ig_exchange_token",
                    "short",
                )
                self._send({"access_token": "long", "expires_in": 5184000})
            elif parts == ["refresh_access_token"]:
                state.refreshed.append(query["access_token"])
                self._send({"access_token": "renewed"})
            elif parts == ["v26.0", "me"]:
                self._send({"user_id": "IGU", "username": "sode.jp"})
            elif query.get("fields") == "status_code,status":
                self._send({"status_code": state.containers[parts[1]]["status"]})
            elif query.get("fields") == "permalink":
                self._send({"permalink": f"https://www.instagram.com/reel/{parts[1]}/"})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            parts, query = self._route()
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            if parts == ["oauth", "access_token"]:
                form = dict(urllib.parse.parse_qsl(body))
                assert form["redirect_uri"] == f"{PUBLIC}/ig/callback"
                assert (form["client_secret"], form["code"]) == ("ig-secret", "the-code")
                self._send({"data": [{"access_token": "short", "user_id": "IGU"}]})
            elif parts[-1] == "media":
                assert "upload_type" not in query  # Instagram Login: fetched from a URL
                container = f"C{len(state.containers) + 1}"
                state.containers[container] = {"status": "IN_PROGRESS", "url": query["video_url"]}
                self._send({"id": container})
            elif parts[-1] == "media_publish":
                assert state.containers[query["creation_id"]]["status"] == "FINISHED"
                state.publishes.append(query["creation_id"])
                self._send({"id": "M1"})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield state, base
    server.shutdown()
    server.server_close()


def api_for(base: str) -> ig.InstagramLoginApi:
    return ig.InstagramLoginApi(
        APP,
        graph_root=base,
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/oauth/access_token",
    )


def clip(tmp_path: Path) -> ClipPackage:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"reel" * 500)
    return ClipPackage(9, "c9", video, None, ChannelConfig(name="alpha"), None, [], account="ja")


def test_an_account_without_a_page_posts_through_a_temporary_public_link(
    instagram: tuple[FakeInstagram, str], tmp_path: Path
) -> None:
    state, base = instagram
    now = time.time()
    route = ig.DirectRoute(
        api_for(base), PUBLIC, tmp_path / "public",
        logins={"ja": InstagramLogin("ja", "IGU", "sode.jp", "long", now)}.get,
        save=lambda _login: None, clock=lambda: now,
    )  # fmt: skip
    page_route = MetaApi(MetaApp("1", "fb"), graph_url=base)
    publisher = InstagramPublisher(page_route, tokens=lambda _a: None, direct=route)
    package = clip(tmp_path)
    assert publisher.validate(package, META) == []

    started = publisher.publish(package, META, "public")
    staged = tmp_path / "public" / ig.public_name("ig-secret", package)
    assert staged.read_bytes() == package.video.read_bytes()
    assert state.containers["C1"]["url"] == f"{PUBLIC}/ig/v/{staged.name}"

    state.containers["C1"]["status"] = "FINISHED"  # Instagram fetched and processed it
    done = publisher.poll_status(package, started.remote_id)
    assert done is not None
    assert (done.state, done.url) == ("published", "https://www.instagram.com/reel/M1/")
    assert state.publishes == ["C1"] and not staged.exists()  # the public link is gone


def test_a_page_still_wins_and_no_login_names_the_command(
    instagram: tuple[FakeInstagram, str], tmp_path: Path
) -> None:
    _, base = instagram
    page = PageToken("ja", "P1", "Sode JP", "page", "IG9", "sode.jp")
    route = ig.DirectRoute(api_for(base), PUBLIC, tmp_path, logins=lambda _a: None)
    publisher = InstagramPublisher(
        MetaApi(APP, graph_url=base), tokens={"ja": page}.get, direct=route
    )
    assert publisher._route("ja").direct is False  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(PublishError, match="peakreel instagram login --account es"):
        publisher._route("es")  # pyright: ignore[reportPrivateUsage]


def test_a_week_old_login_is_renewed(instagram: tuple[FakeInstagram, str], tmp_path: Path) -> None:
    state, base = instagram
    now = time.time()
    saved: list[InstagramLogin] = []
    old = InstagramLogin("ja", "IGU", "sode.jp", "long", now - 8 * 86400)
    route = ig.DirectRoute(
        api_for(base), PUBLIC, tmp_path, logins=lambda _a: old, save=saved.append, clock=lambda: now
    )
    login = route.login("ja")
    assert login is not None and (login.token, login.refreshed_at) == ("renewed", now)
    assert state.refreshed == ["long"] and saved == [login]


def test_login_is_finished_once_with_the_started_state(
    instagram: tuple[FakeInstagram, str], tmp_path: Path
) -> None:
    _, base = instagram
    api = api_for(base)
    now = time.time()
    url = ig.start_login(api, "ja", PUBLIC, tmp_path, now=now)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["redirect_uri"] == f"{PUBLIC}/ig/callback"
    assert set(query["scope"].split(",")) == set(ig.SCOPES)

    with pytest.raises(PublishError, match="does not match"):
        ig.finish_login(api, "the-code", "forged", PUBLIC, tmp_path, now=now)
    url = ig.start_login(api, "ja", PUBLIC, tmp_path, now=now)
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
    saved: list[InstagramLogin] = []
    login = ig.finish_login(api, "the-code", state, PUBLIC, tmp_path, now=now, save=saved.append)
    assert (login.account, login.user_id, login.username, login.token) == (
        "ja",
        "IGU",
        "sode.jp",
        "long",
    )
    assert saved == [login]
    with pytest.raises(PublishError, match="no Instagram login was started"):  # used once
        ig.finish_login(api, "the-code", state, PUBLIC, tmp_path, now=now)


def test_only_ig_paths_are_public_on_a_password_protected_page(
    write_config: WriteConfig, memory_keyring: MemoryKeyring
) -> None:
    from peakreel.review.auth import set_password

    set_password("correct horse battery")
    config = load_config(write_config({"review": {"public_hosts": ["pc.tail1.ts.net"]}}))
    client = TestClient(create_app(config), base_url="http://pc.tail1.ts.net")
    staged = config.data_dir / "public" / ("a" * 32 + ".mp4")
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"reel")
    assert client.get(f"/ig/v/{staged.name}").content == b"reel"  # Instagram fetches it
    assert client.get("/ig/v/..%2Fpeakreel.db").status_code == 404  # only staged names
    assert client.get("/ig/v/" + "b" * 32 + ".mp4").status_code == 404
    assert client.get("/api/clips").status_code == 401  # the rest still needs the password
    page = client.get("/ig/callback", params={"code": "x", "state": "y"})
    assert page.status_code == 200 and "Not connected" in page.text
