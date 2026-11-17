"""Kick chat through Kick's official event webhooks.

Kick does not offer a documented live chat socket; its official API delivers
chat as signed webhook calls to one public HTTPS address per developer app (set
in kick.com/settings/developer). Peakreel runs a small HTTP endpoint on this
computer and a tunnel (Cloudflare Tunnel or ngrok) gives it that public address.

Every request is checked before it is used:

- its RSA signature (PKCS#1 v1.5, SHA-256 over ``message-id.timestamp.body``)
  against Kick's published key, fetched at start and again once if a signature
  fails (Kick rotates the key);
- its timestamp, which must be recent (replays are refused);
- its message id, so a delivery Kick retries is used once.

Then it becomes the same :class:`ChatMessage` the Twitch source produces: chat
lines as ``privmsg``, and subscriptions, gifted subscriptions and gifted Kicks as
alerts, so the spike detector, the chat log and the alerts check work unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, cast

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from peakreel.chat.base import ChatMessage, ConnectionEvent, Emit
from peakreel.config import KickConfig
from peakreel.kick.api import KickApi, KickError
from peakreel.log import get_logger
from peakreel.platforms import channel_key, split_key

__all__ = ["KickChat", "SignatureError", "verify_signature"]

log = get_logger(__name__)

_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
_MAX_BODY = 1_000_000
_SEEN = 5000


class SignatureError(ValueError):
    """A webhook request did not come from Kick (or was tampered with)."""


def verify_signature(
    public_key_pem: str, message_id: str, timestamp: str, body: bytes, signature: str
) -> None:
    """Check a webhook request's ``Kick-Event-Signature``.

    Raises
    ------
    SignatureError
        If the signature is malformed or does not match.
    """
    try:
        key = RSA.import_key(public_key_pem)
        digest = SHA256.new(f"{message_id}.{timestamp}.".encode() + body)
        pkcs1_15.new(key).verify(digest, base64.b64decode(signature, validate=True))
    except (ValueError, TypeError) as exc:
        raise SignatureError("invalid Kick signature") from exc


def _ts(value: object) -> float | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return round(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp(), 3)
    return None


def _user(data: dict[str, Any], field: str) -> dict[str, Any]:
    return cast("dict[str, Any]", data.get(field) or {})


def to_messages(event: str, data: dict[str, Any], received: float) -> list[ChatMessage]:
    """Turn one webhook payload into chat events (empty for events Peakreel ignores).

    Examples
    --------
    >>> to_messages("chat.message.sent", {
    ...     "message_id": "m1", "broadcaster": {"channel_slug": "WestCol"},
    ...     "sender": {"username": "Fan", "user_id": 7, "identity": {"badges": []}},
    ...     "content": "GG [emote:37226:KEKW]", "created_at": "2026-11-19T20:00:00Z"}, 0.0)[0].text
    'GG KEKW'
    """
    broadcaster = _user(data, "broadcaster")
    slug = str(broadcaster.get("channel_slug") or broadcaster.get("username") or "").lower()
    if not slug:
        return []
    channel = channel_key("kick", slug)
    if event == "chat.message.sent":
        sender = _user(data, "sender")
        identity = cast("dict[str, Any]", sender.get("identity") or {})
        badges = cast("list[dict[str, Any]]", identity.get("badges") or [])
        kinds = {str(b.get("type")) for b in badges}
        tags = {
            "badges": ",".join(f"{b.get('type')}/{b.get('count', 1)}" for b in badges),
            "mod": "1" if "moderator" in kinds else "0",
            "subscriber": "1" if "subscriber" in kinds else "0",
        }
        username = str(sender.get("username") or "")
        return [
            ChatMessage(
                ts=received,
                channel=channel,
                user=username.lower(),
                text=_EMOTE.sub(r"\1", str(data.get("content") or "")),
                server_ts=_ts(data.get("created_at")),
                display_name=username or None,
                user_id=str(sender.get("user_id")) if sender.get("user_id") else None,
                msg_id=str(data.get("message_id")) if data.get("message_id") else None,
                tags={k: v for k, v in tags.items() if v},
            )
        ]
    alerts = {
        "channel.subscription.new": ("subscriber", "sub"),
        "channel.subscription.renewal": ("subscriber", "resub"),
        "channel.subscription.gifts": ("gifter", "subgift"),
    }
    if event in alerts:
        role, msg_id = alerts[event]
        user = _user(data, role)
        return [
            ChatMessage(
                ts=received,
                channel=channel,
                user=str(user.get("username") or "anonymous").lower(),
                text="",
                type="usernotice",
                tags={"msg-id": msg_id},
            )
        ]
    if event == "kicks.gifted":
        sender = _user(data, "sender")
        gift = cast("dict[str, Any]", data.get("gift") or {})
        return [
            ChatMessage(
                ts=received,
                channel=channel,
                user=str(sender.get("username") or "").lower(),
                text=str(gift.get("message") or ""),
                tags={"bits": str(gift.get("amount") or 1)},
            )
        ]
    return []


class KickChat:
    """Kick chat source: subscribes channels to webhook events and serves the webhook.

    Parameters
    ----------
    channels : sequence of str
        Channel keys (``kick-<slug>``).
    api : KickApi
    settings : KickConfig
        Where the local webhook endpoint listens.
    clock : callable, optional
    """

    def __init__(
        self,
        channels: Sequence[str],
        api: KickApi,
        settings: KickConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._channels: list[str] = list(dict.fromkeys(channels))
        self._api = api
        self._settings = settings
        self._clock = clock
        self._emit: Emit | None = None
        self._public_key: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._subscriptions: dict[str, list[str]] = {}
        self._joined: set[str] = set()
        self.port: int | None = None

    @property
    def channels(self) -> tuple[str, ...]:
        """Channels this source follows."""
        return tuple(self._channels)

    @property
    def joined(self) -> frozenset[str]:
        """Channels whose webhook subscriptions are active."""
        return frozenset(self._joined)

    # -- subscriptions --------------------------------------------------------------------

    async def _subscribe(self, key: str) -> None:
        slug = split_key(key)[1]
        try:
            found = await asyncio.to_thread(self._api.channels, [slug])
            channel = found.get(slug)
            if channel is None:
                log.warning("kick channel not found", channel=key)
                return
            ids = await asyncio.to_thread(self._api.subscribe, channel.broadcaster_user_id)
        except KickError as exc:
            log.error("kick chat subscription failed", channel=key, error=str(exc))
            return
        self._subscriptions[key] = ids
        self._joined.add(key)
        if self._emit is not None:
            self._emit(ConnectionEvent(ts=round(self._clock(), 3), channel=key, event="joined"))
        log.info("kick chat subscribed", channel=key)

    async def _unsubscribe(self, key: str, reason: str) -> None:
        ids = self._subscriptions.pop(key, [])
        with contextlib.suppress(KickError):
            await asyncio.to_thread(self._api.unsubscribe, ids)
        if key in self._joined:
            self._joined.discard(key)
            if self._emit is not None:
                now = round(self._clock(), 3)
                self._emit(ConnectionEvent(ts=now, channel=key, event="stopped", reason=reason,
                                           last_alive_ts=now))  # fmt: skip

    async def _remove_stale(self) -> None:
        # Subscriptions left by an earlier run (a crash) would deliver twice.
        try:
            existing = await asyncio.to_thread(self._api.subscriptions)
            stale = [str(s["id"]) for s in existing if s.get("id")]
            await asyncio.to_thread(self._api.unsubscribe, stale)
        except KickError as exc:
            log.warning("could not clean old kick subscriptions", error=str(exc))

    async def join(self, channel: str) -> None:
        """Start receiving ``channel``'s chat while running."""
        if channel in self._channels:
            return
        self._channels.append(channel)
        if self._emit is not None:
            await self._subscribe(channel)

    async def part(self, channel: str) -> None:
        """Stop receiving ``channel``'s chat."""
        if channel not in self._channels:
            return
        self._channels.remove(channel)
        await self._unsubscribe(channel, "channel removed")

    # -- webhook ----------------------------------------------------------------------------

    async def run(self, emit: Emit) -> None:
        """Serve the webhook endpoint and subscribe every channel, until cancelled."""
        self._emit = emit
        self._public_key = await asyncio.to_thread(self._api.public_key)
        server = await asyncio.start_server(
            self._serve, self._settings.webhook_host, self._settings.webhook_port
        )
        self.port = int(server.sockets[0].getsockname()[1])
        log.info("kick webhook listening", host=self._settings.webhook_host, port=self.port,
                 path=self._settings.webhook_path)  # fmt: skip
        try:
            await self._remove_stale()
            for key in list(self._channels):
                await self._subscribe(key)
            async with server:
                await server.serve_forever()
        finally:
            for key in list(self._subscriptions):
                await self._unsubscribe(key, "recorder stopped")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = 400
        try:
            status = await self._handle(reader)
        except Exception:
            log.exception("kick webhook request failed")
            status = 500
        finally:
            reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}.get(
                status, "Error"
            )
            with contextlib.suppress(Exception):
                head = (
                    f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                writer.write(head.encode())
                await writer.drain()
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader) -> int:
        request = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").split()
        if len(request) < 2:
            return 400
        headers: dict[str, str] = {}
        while True:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").strip()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        method, path = request[0], request[1].split("?")[0]
        if method != "POST" or path != self._settings.webhook_path:
            return 404
        length = int(headers.get("content-length") or 0)
        if not 0 < length <= _MAX_BODY:
            return 400
        body = await asyncio.wait_for(reader.readexactly(length), 10)
        message_id = headers.get("kick-event-message-id", "")
        timestamp = headers.get("kick-event-message-timestamp", "")
        signature = headers.get("kick-event-signature", "")
        if not (message_id and timestamp and signature):
            return 401
        if not await self._verified(message_id, timestamp, body, signature):
            log.warning("kick webhook with a bad signature refused")
            return 401
        sent = _ts(timestamp)
        if sent is None or abs(self._clock() - sent) > self._settings.max_age_s:
            log.warning("kick webhook too old or from the future refused", timestamp=timestamp)
            return 401
        if message_id in self._seen:
            return 200  # Kick retried a delivery we already used
        self._seen[message_id] = None
        while len(self._seen) > _SEEN:
            self._seen.popitem(last=False)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return 400
        if isinstance(data, dict) and self._emit is not None:
            for message in to_messages(
                headers.get("kick-event-type", ""),
                cast("dict[str, Any]", data),
                round(self._clock(), 3),
            ):
                if message.channel in self._joined:
                    self._emit(message)
        return 200

    async def _verified(self, message_id: str, timestamp: str, body: bytes, signature: str) -> bool:
        for attempt in range(2):
            if self._public_key is None:
                return False
            try:
                verify_signature(self._public_key, message_id, timestamp, body, signature)
                return True
            except SignatureError:
                if attempt == 0:
                    # Kick may have rotated its key: fetch it once and try again.
                    with contextlib.suppress(KickError):
                        self._public_key = await asyncio.to_thread(self._api.public_key)
        return False
