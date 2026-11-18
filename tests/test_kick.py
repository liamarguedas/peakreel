from __future__ import annotations

import asyncio
import base64
import json
import threading
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from pydantic import ValidationError

from peakreel.chat.base import ChatEvent, ChatMessage, ConnectionEvent, Emit
from peakreel.chat.multi import MultiChat
from peakreel.config import ChannelConfig, KickConfig, PublishAccount, PublishConfig
from peakreel.kick.api import KickApi, KickChannel, KickCredentials, KickError
from peakreel.kick.chat import KickChat, SignatureError, to_messages, verify_signature
from peakreel.publish.metadata import ClipFacts, build_metadata, validate
from peakreel.record.recorder import is_twitch_hls
from peakreel.render.vertical import credit_text
from tests.conftest import WriteConfig

NOW = 1_795_000_000.0


# -- identity and credits -----------------------------------------------------------------


def test_kick_channels_get_a_prefixed_key() -> None:
    channel = ChannelConfig.model_validate({"name": "WestCol", "platform": "kick"})
    assert channel.name == "kick-westcol"
    assert channel.display == "westcol"
    assert channel.credit_text == "Clip from kick.com/westcol"
    assert ChannelConfig.model_validate({"name": "kick-west-col", "platform": "kick"}).name == (
        "kick-west-col"
    )
    with pytest.raises(ValidationError, match="platform: kick"):
        ChannelConfig(name="kick-westcol")
    with pytest.raises(ValidationError, match="not a valid Twitch login"):
        ChannelConfig(name="west-col")
    assert credit_text("kick-westcol", None) == "Clip from kick.com/westcol"
    assert credit_text("xqc", None) == "Clip from twitch.tv/xqc"


def test_account_credit_lines_follow_the_platform() -> None:
    legacy = PublishAccount(id="es", credit="Clip de twitch.tv/{channel}")
    assert legacy.credit_line("kick-westcol", "WestCol") == "Clip de kick.com/westcol"
    assert legacy.credit_line("ibai", "Ibai") == "Clip de twitch.tv/ibai"
    modern = PublishAccount(id="ja", credit="クリップ元: {channel_url}")
    assert modern.credit_line("kick-westcol", "WestCol") == "クリップ元: kick.com/westcol"
    facts = ClipFacts(ChannelConfig.model_validate({"name": "westcol", "platform": "kick"}), "Wow")
    settings = PublishConfig()
    meta = build_metadata(facts, "youtube", settings, account=modern)
    assert "クリップ元: kick.com/westcol" in meta.text
    assert "https://kick.com/westcol" in meta.text
    assert validate(meta, settings.youtube) == []


# -- API ------------------------------------------------------------------------------------


@dataclass
class FakeKick:
    requests: list[str] = field(default_factory=list[str])
    bodies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    refuse: str | None = None
    public_key: str = ""


@pytest.fixture
def kick_api() -> Iterator[tuple[FakeKick, str]]:
    state = FakeKick()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, code: int, body: object) -> None:
            data = json.dumps(body).encode() if not isinstance(body, bytes) else body
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> tuple[str, dict[str, list[str]]]:
            url = urllib.parse.urlsplit(self.path)
            return url.path, urllib.parse.parse_qs(url.query)

        def do_POST(self) -> None:
            path, _ = self._route()
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state.requests.append(f"POST {path}")
            if path == "/oauth/token":
                form = urllib.parse.parse_qs(body.decode())
                ok = form["client_secret"] == ["secret"] and form["grant_type"] == [
                    "client_credentials"
                ]
                self._send(
                    200 if ok else 401,
                    {"access_token": "app-token", "expires_in": 3600} if ok else {},
                )
                return
            assert self.headers["Authorization"] == "Bearer app-token"
            payload = json.loads(body)
            state.bodies.append(payload)
            rows = [
                {"name": e["name"], "version": e["version"], "subscription_id": f"sub-{e['name']}",
                 "error": state.refuse if state.refuse and e["name"] == "kicks.gifted" else ""}
                for e in payload["events"]
            ]  # fmt: skip
            self._send(200, {"data": rows, "message": "OK"})

        def do_GET(self) -> None:
            path, query = self._route()
            state.requests.append(f"GET {path}?{urllib.parse.urlencode(query, doseq=True)}")
            if path == "/public/v1/public-key":
                self._send(200, {"data": {"public_key": state.public_key}})
                return
            assert self.headers["Authorization"] == "Bearer app-token"
            if path == "/public/v1/channels":
                rows = [
                    {"broadcaster_user_id": 42, "slug": "westcol", "stream_title": "GTA VI día 1",
                     "category": {"name": "Grand Theft Auto VI"},
                     "stream": {"is_live": True, "viewer_count": 51000, "language": "es",
                                "start_time": "2026-11-19T18:00:00Z"}},
                ]  # fmt: skip
                self._send(200, {"data": [r for r in rows if r["slug"] in query.get("slug", [])]})
            elif path == "/public/v2/categories":
                found = query["name"] == ["Grand Theft Auto VI"]
                self._send(200, {"data": [{"id": 9001}] if found else [], "pagination": {}})
            elif path == "/public/v2/livestreams":

                def row(slug: str, viewers: int, lang: str) -> dict[str, Any]:
                    return {"channel": {"slug": slug}, "category": {"name": "Grand Theft Auto VI"},
                            "title": "", "viewer_count": viewers, "language_code": lang,
                            "started_at": "2026-11-19T18:00:00Z"}  # fmt: skip

                pages = {"": ([row("small", 10, "en"), row("big", 90000, "es")], "c2"),
                         "c2": ([row("mid", 500, "pt-BR")], "")}  # fmt: skip
                data, cursor = pages[query.get("cursor", [""])[0]]
                self._send(200, {"data": data, "pagination": {"next_cursor": cursor}})
            elif path == "/public/v1/events/subscriptions":
                self._send(200, {"data": [{"id": "old-1", "broadcaster_user_id": 7}]})
            else:
                self._send(404, {})

        def do_DELETE(self) -> None:
            path, query = self._route()
            state.requests.append(f"DELETE {path}?{urllib.parse.urlencode(query, doseq=True)}")
            self._send(204, b"")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def api_for(base: str, secret: str = "secret") -> KickApi:
    return KickApi(KickCredentials("cid", secret), api_base=base, auth_base=base)


def test_api_channels_categories_and_streams(kick_api: tuple[FakeKick, str]) -> None:
    state, base = kick_api
    api = api_for(base)
    (channel,) = api.channels(["WestCol", "missing"]).values()
    assert (channel.slug, channel.broadcaster_user_id, channel.live, channel.language) == (
        "westcol", 42, True, "es",
    )  # fmt: skip
    stream = channel.stream()
    assert stream is not None
    assert (stream.login, stream.viewers, stream.game) == (
        "kick-westcol",
        51000,
        "Grand Theft Auto VI",
    )
    assert api.category_id("Grand Theft Auto VI") == 9001
    assert api.category_id("GTA 6") is None
    streams = api.top_streams(9001, ["en", "es", "pt"])
    assert [(s.login, s.language) for s in streams] == [
        ("kick-big", "es"), ("kick-mid", "pt"), ("kick-small", "en"),
    ]  # fmt: skip
    assert any("language_code=es" in r for r in state.requests)
    assert sum(1 for r in state.requests if r.startswith("GET /oauth")) == 0
    assert sum(1 for r in state.requests if r == "POST /oauth/token") == 1  # token reused
    with pytest.raises(KickError, match="401"):
        api_for(base, "wrong").token()


def test_api_subscriptions(kick_api: tuple[FakeKick, str]) -> None:
    state, base = kick_api
    api = api_for(base)
    ids = api.subscribe(42)
    assert ids[0] == "sub-chat.message.sent"
    assert state.bodies[0]["broadcaster_user_id"] == 42
    assert state.bodies[0]["method"] == "webhook"
    assert {e["name"] for e in state.bodies[0]["events"]} >= {"chat.message.sent", "kicks.gifted"}
    state.refuse = "not allowed"
    with pytest.raises(KickError, match=r"kicks\.gifted: not allowed"):
        api.subscribe(42)
    assert api.subscriptions() == [{"id": "old-1", "broadcaster_user_id": 7}]
    api.unsubscribe(["a", "b"])
    assert state.requests[-1] == "DELETE /public/v1/events/subscriptions?id=a&id=b"


# -- signatures and payloads ----------------------------------------------------------------


KEY = RSA.generate(2048)
PUBLIC = KEY.publickey().export_key().decode()
OTHER = RSA.generate(2048)


def sign(message_id: str, timestamp: str, body: bytes, key: RSA.RsaKey = KEY) -> str:
    digest = SHA256.new(f"{message_id}.{timestamp}.".encode() + body)
    return base64.b64encode(pkcs1_15.new(key).sign(digest)).decode()


def test_signature_verification() -> None:
    body = b'{"x": 1}'
    verify_signature(
        PUBLIC, "m1", "2026-11-19T20:00:00Z", body, sign("m1", "2026-11-19T20:00:00Z", body)
    )
    with pytest.raises(SignatureError):
        verify_signature(
            PUBLIC,
            "m1",
            "2026-11-19T20:00:00Z",
            b'{"x": 2}',
            sign("m1", "2026-11-19T20:00:00Z", body),
        )
    with pytest.raises(SignatureError):
        verify_signature(PUBLIC, "m1", "t", body, "not base64!")


def test_payloads_become_chat_events() -> None:
    broadcaster = {"channel_slug": "WestCol"}
    sender = {
        "username": "Fan_1",
        "user_id": 9,
        "identity": {"badges": [{"type": "moderator"}, {"type": "subscriber", "count": 3}]},
    }
    payload = {
        "message_id": "m1",
        "broadcaster": broadcaster,
        "content": "LOL [emote:1:KEKW]",
        "created_at": "2026-11-19T20:00:00Z",
        "sender": sender,
    }
    (chat,) = to_messages("chat.message.sent", payload, NOW)
    assert (chat.channel, chat.user, chat.text, chat.type) == (
        "kick-westcol",
        "fan_1",
        "LOL KEKW",
        "privmsg",
    )
    assert chat.tags["mod"] == "1"
    assert chat.server_ts == datetime(2026, 11, 19, 20, tzinfo=UTC).timestamp()
    (gift,) = to_messages(
        "channel.subscription.gifts", {"broadcaster": broadcaster, "gifter": {"username": "G"}}, NOW
    )
    assert (gift.type, gift.tags["msg-id"]) == ("usernotice", "subgift")
    gifted = {
        "broadcaster": broadcaster,
        "sender": {"username": "K"},
        "gift": {"amount": 500, "message": "gg"},
    }
    (kicks,) = to_messages("kicks.gifted", gifted, NOW)
    assert kicks.tags["bits"] == "500"
    assert to_messages("channel.followed", {"broadcaster": broadcaster}, NOW) == []


# -- the webhook endpoint -------------------------------------------------------------------


class StubKick(KickApi):
    def __init__(self) -> None:
        super().__init__(KickCredentials("cid", "secret"))
        self.key = PUBLIC
        self.subscribed: list[int] = []
        self.unsubscribed: list[list[str]] = []

    def public_key(self) -> str:
        return self.key

    def channels(self, slugs: Sequence[str]) -> dict[str, KickChannel]:
        ids = {"westcol": 42, "spreen": 43}
        return {
            s: KickChannel(s, ids[s], True, "", "GTA", 1000, "es", None) for s in slugs if s in ids
        }

    def subscribe(self, broadcaster_user_id: int, events: Any = None) -> list[str]:
        self.subscribed.append(broadcaster_user_id)
        return [f"sub-{broadcaster_user_id}"]

    def subscriptions(self) -> list[dict[str, Any]]:
        return [{"id": "stale-1"}]

    def unsubscribe(self, ids: Sequence[str]) -> None:
        self.unsubscribed.append(list(ids))


async def post(port: int, body: bytes, headers: dict[str, str]) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = ["POST /kick/webhook HTTP/1.1", "Host: x", f"Content-Length: {len(body)}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    await writer.drain()
    status = int((await reader.readline()).split()[1])
    writer.close()
    return status


def signed(event: str, payload: dict[str, Any], message_id: str, *, when: float = NOW,
           key: RSA.RsaKey = KEY) -> tuple[bytes, dict[str, str]]:  # fmt: skip
    body = json.dumps(payload).encode()
    timestamp = datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return body, {
        "Kick-Event-Message-Id": message_id, "Kick-Event-Message-Timestamp": timestamp,
        "Kick-Event-Signature": sign(message_id, timestamp, body, key), "Kick-Event-Type": event,
        "Kick-Event-Version": "1",
    }  # fmt: skip


async def test_webhook_endpoint_verifies_and_delivers_chat() -> None:
    api = StubKick()
    chat = KickChat(["kick-westcol"], api, KickConfig(webhook_port=0), clock=lambda: NOW)
    events: list[ChatEvent] = []
    task = asyncio.create_task(chat.run(events.append))
    async with asyncio.timeout(5):
        while chat.port is None or not chat.joined:
            await asyncio.sleep(0.01)
    assert api.unsubscribed == [["stale-1"]]  # left over from an earlier run
    assert api.subscribed == [42]
    assert isinstance(events[0], ConnectionEvent) and events[0].event == "joined"

    message = {"message_id": "m1", "broadcaster": {"channel_slug": "westcol"},
               "sender": {"username": "fan"}, "content": "no way"}  # fmt: skip
    body, headers = signed("chat.message.sent", message, "id-1")
    assert await post(chat.port, body, headers) == 200
    assert await post(chat.port, body, headers) == 200  # Kick retried: used once
    forged = {
        **headers,
        "Kick-Event-Signature": sign("id-2", headers["Kick-Event-Message-Timestamp"], body, OTHER),
    }
    assert await post(chat.port, body, {**forged, "Kick-Event-Message-Id": "id-2"}) == 401
    old_body, old = signed("chat.message.sent", message, "id-3", when=NOW - 3600)
    assert await post(chat.port, old_body, old) == 401  # replayed
    other_body, other = signed(
        "chat.message.sent", {**message, "broadcaster": {"channel_slug": "spreen"}}, "id-4"
    )
    assert await post(chat.port, other_body, other) == 200  # not joined: ignored

    # Kick rotated its key: the first check fails, the key is fetched again, it passes.
    api.key = OTHER.publickey().export_key().decode()
    rotated_body, rotated = signed("chat.message.sent", message, "id-5", key=OTHER)
    assert await post(chat.port, rotated_body, rotated) == 200

    texts = [e for e in events if isinstance(e, ChatMessage)]
    assert [(m.channel, m.user, m.text) for m in texts] == [
        ("kick-westcol", "fan", "no way"), ("kick-westcol", "fan", "no way"),
    ]  # fmt: skip

    await chat.join("kick-spreen")
    assert api.subscribed == [42, 43]
    await chat.part("kick-westcol")
    assert api.unsubscribed[-1] == ["sub-42"]
    assert any(isinstance(e, ConnectionEvent) and e.event == "stopped" for e in events)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.unsubscribed[-1] == ["sub-43"]  # unsubscribed on shutdown


async def test_multichat_routes_by_platform() -> None:
    calls: list[str] = []

    class Source:
        def __init__(self, name: str) -> None:
            self.name = name
            self.channels: tuple[str, ...] = ()

        async def run(self, emit: Emit) -> None:
            await asyncio.Event().wait()

        async def join(self, channel: str) -> None:
            calls.append(f"{self.name} join {channel}")

        async def part(self, channel: str) -> None:
            calls.append(f"{self.name} part {channel}")

    chat = MultiChat({"twitch": Source("twitch"), "kick": Source("kick")})  # type: ignore[dict-item]
    await chat.join("xqc")
    await chat.join("kick-westcol")
    await chat.part("kick-westcol")
    assert calls == ["twitch join xqc", "kick join kick-westcol", "kick part kick-westcol"]


def test_kick_streams_are_recordable() -> None:
    class Reader:
        pass

    kick_stream = type("KickHLSStream", (), {"__reader__": Reader})()
    other = type("HLSStream", (), {"__reader__": Reader})()
    assert is_twitch_hls(kick_stream)
    assert not is_twitch_hls(other)


def test_pipeline_uses_kick_for_kick_channels(write_config: WriteConfig) -> None:
    from peakreel.config import load_config
    from peakreel.kick.chat import KickChat
    from peakreel.notify import Notifier
    from peakreel.pipeline import LivePipeline

    config = load_config(
        write_config(
            {
                "channels": [{"name": "xqc"}, {"name": "westcol", "platform": "kick"}],
                "captions": {"enabled": False},
            }
        )
    )
    names = [c.name for c in config.channels]
    assert names == ["xqc", "kick-westcol"]
    pipeline = LivePipeline(
        config, names, Path("ffmpeg"), notifier=Notifier(config.notify, senders=[]),
        helix=None, kick=StubKick(),
    )  # fmt: skip
    source = pipeline._base_source  # pyright: ignore[reportPrivateUsage]
    assert isinstance(source, MultiChat)
    assert set(source.channels) == {"xqc", "kick-westcol"}
    live = pipeline._live_streams()  # pyright: ignore[reportPrivateUsage]
    assert set(live) == {"kick-westcol"}  # no Twitch client: only Kick is polled
    assert live["kick-westcol"].language == "es"
    assert any(isinstance(s, KickChat) for s in source._sources.values())  # pyright: ignore[reportPrivateUsage]

    # Without the Kick app, Kick chat is off (logged) and Twitch keeps working.
    plain = LivePipeline(
        config, names, Path("ffmpeg"), notifier=Notifier(config.notify, senders=[]),
        helix=None, kick=None,
    )  # fmt: skip
    assert not isinstance(plain._base_source, MultiChat)  # pyright: ignore[reportPrivateUsage]


def test_blocklist_names_kick_channels(tmp_path: Path) -> None:
    from peakreel.blocklist import load_blocklist

    path = tmp_path / "blocklist.yaml"
    path.write_text(
        "channels:\n"
        "  - name: WestCol\n    platform: kick\n"
        "  - https://kick.com/Spreen/\n"
        "  - kick-already\n"
        "  - name: xqc\n"
        "  - twitch.tv/Ibai\n",
        encoding="utf-8",
    )
    assert load_blocklist(path) == {"kick-westcol", "kick-spreen", "kick-already", "xqc", "ibai"}
