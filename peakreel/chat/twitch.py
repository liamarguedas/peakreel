"""Anonymous Twitch chat client.

Connects to the Twitch IRC gateway over a websocket with a ``justinfan`` nickname,
which needs no token and is read-only. One connection carries every channel.

Liveness
--------
The client does not rely on chat traffic to know the connection is alive, since a
quiet chat and a dead socket look the same. Instead it sends a websocket ping every
``keepalive_interval_s`` and expects the pong within ``keepalive_timeout_s``. A
missed pong marks the connection as stalled, tears it down and reconnects. The last
time the connection was proven alive is reported in every ``disconnected`` event, so
the possible data hole is bounded and visible in the log.

Reconnects use exponential backoff with jitter, and every channel is rejoined.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable, Sequence
from typing import cast

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from peakreel.chat.base import ChatMessage, ConnectionEvent, Emit, MessageType
from peakreel.chat.irc import IrcMessage, IrcParseError, parse_line
from peakreel.config import ChatConfig
from peakreel.log import get_logger

__all__ = ["KEPT_TAGS", "TwitchChat", "message_from_irc"]

log = get_logger(__name__)

KEPT_TAGS: frozenset[str] = frozenset(
    {
        "badges",
        "bits",
        "emote-only",
        "first-msg",
        "mod",
        "msg-id",
        "msg-param-cumulative-months",
        "msg-param-mass-gift-count",
        "msg-param-sub-plan",
        "msg-param-viewerCount",
        "returning-chatter",
        "subscriber",
        "system-msg",
        "vip",
    }
)
"""Tags copied into :attr:`ChatMessage.tags`. Empty values are dropped."""

_ACTION_PREFIX = "\x01ACTION "
_JOIN_CONFIRM_TIMEOUT_S = 15.0


def _channel_name(raw: str) -> str:
    return raw.strip().lstrip("#").lower()


def message_from_irc(irc: IrcMessage, ts: float) -> ChatMessage | None:
    """Convert a ``PRIVMSG`` or ``USERNOTICE`` line to a :class:`ChatMessage`.

    Parameters
    ----------
    irc : IrcMessage
        A parsed line.
    ts : float
        Local receive time, UTC epoch seconds.

    Returns
    -------
    ChatMessage or None
        ``None`` when the line is another command or lacks a channel or sender.

    Notes
    -----
    ``/me`` actions arrive wrapped in a CTCP ``ACTION`` envelope, which is removed so
    the text reads like any other message.

    Examples
    --------
    >>> line = "@display-name=Vi;tmi-sent-ts=1794000000500 :vi!vi@vi.tmi.twitch.tv PRIVMSG #c :hi"
    >>> msg = message_from_irc(parse_line(line), ts=1794000000.75)
    >>> msg.channel, msg.user, msg.text, msg.server_ts
    ('c', 'vi', 'hi', 1794000000.5)
    """
    if irc.command not in ("PRIVMSG", "USERNOTICE") or not irc.params:
        return None
    channel = _channel_name(irc.params[0])
    text = irc.params[1] if len(irc.params) > 1 else ""
    message_type: MessageType
    if irc.command == "PRIVMSG":
        message_type = "privmsg"
        user = irc.nick
        if text.startswith(_ACTION_PREFIX):
            text = text[len(_ACTION_PREFIX) :].rstrip("\x01")
    else:
        message_type = "usernotice"
        user = irc.tags.get("login")
    if not channel or not user:
        return None
    sent = irc.tags.get("tmi-sent-ts", "")
    return ChatMessage(
        ts=ts,
        channel=channel,
        user=user.lower(),
        text=text,
        type=message_type,
        server_ts=int(sent) / 1000 if sent.isdigit() else None,
        display_name=irc.tags.get("display-name") or None,
        user_id=irc.tags.get("user-id") or None,
        msg_id=irc.tags.get("id") or None,
        tags={k: v for k, v in irc.tags.items() if k in KEPT_TAGS and v},
    )


class TwitchChat:
    """Read-only Twitch chat source for a fixed set of channels.

    Implements :class:`peakreel.chat.base.ChatSource`.

    Parameters
    ----------
    channels : sequence of str
        Channel logins, with or without ``#``. Duplicates are ignored.
    settings : ChatConfig
        The ``chat:`` section of the configuration (URL, keepalive, backoff, join
        batching).
    clock : callable, optional
        Returns the current UTC epoch time in seconds. Defaults to
        :func:`time.time`. Injectable for tests.

    Raises
    ------
    ValueError
        If no channel is given.

    Examples
    --------
    >>> import asyncio
    >>> from peakreel.config import ChatConfig
    >>> chat = TwitchChat(["somestreamer"], ChatConfig())
    >>> asyncio.run(asyncio.wait_for(chat.run(print), timeout=60))  # doctest: +SKIP
    """

    def __init__(
        self,
        channels: Sequence[str],
        settings: ChatConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._channels: tuple[str, ...] = tuple(
            dict.fromkeys(name for name in map(_channel_name, channels) if name)
        )
        if not self._channels:
            raise ValueError("at least one channel is required")
        self._settings = settings
        self._clock = clock
        self._nick = ""
        self._joined: set[str] = set()
        self._last_alive: float | None = None
        self._attempt = 0

    @property
    def channels(self) -> tuple[str, ...]:
        """Channels this source was asked to join, in order."""
        return self._channels

    @property
    def joined(self) -> frozenset[str]:
        """Channels currently confirmed as joined."""
        return frozenset(self._joined)

    def _now(self) -> float:
        return round(self._clock(), 3)

    async def run(self, emit: Emit) -> None:
        """Connect, join every channel and deliver events until cancelled.

        Parameters
        ----------
        emit : callable
            Called synchronously with each :class:`ChatMessage` and
            :class:`ConnectionEvent`, in arrival order.

        Notes
        -----
        This coroutine never returns normally. It reconnects after every failure:
        refused or timed-out connects, closed sockets, missed keepalive pongs, and
        the server's ``RECONNECT`` command.

        For each channel, ``joined`` is emitted when Twitch confirms the JOIN, and
        ``disconnected`` when the connection ends. When the coroutine is cancelled
        (shutdown), ``stopped`` is emitted for every joined channel and the socket
        is closed before the cancellation propagates.
        """
        try:
            while True:
                reason = await self._connect_and_serve(emit)
                self._end_session(emit, "disconnected", reason)
                delay = self._backoff_delay()
                self._attempt += 1
                log.warning("chat disconnected", reason=reason, retry_in_s=round(delay, 2))
                await asyncio.sleep(delay)
        finally:
            self._end_session(emit, "stopped", "recorder stopped")

    def _backoff_delay(self) -> float:
        settings = self._settings
        ceiling = min(
            settings.reconnect_max_delay_s,
            settings.reconnect_initial_delay_s * (2 ** min(self._attempt, 16)),
        )
        # Jitter only spreads reconnects; it is not security sensitive.
        return ceiling * random.uniform(0.5, 1.0)  # noqa: S311

    def _end_session(self, emit: Emit, event: str, reason: str) -> None:
        now = self._now()
        for channel in sorted(self._joined):
            emit(
                ConnectionEvent(
                    ts=now,
                    channel=channel,
                    event="stopped" if event == "stopped" else "disconnected",
                    reason=reason,
                    last_alive_ts=self._last_alive if self._last_alive is not None else now,
                )
            )
        self._joined.clear()
        self._last_alive = None

    async def _connect_and_serve(self, emit: Emit) -> str:
        """Run one connection to completion and return why it ended."""
        settings = self._settings
        try:
            ws = await connect(
                settings.url,
                open_timeout=settings.connect_timeout_s,
                ping_interval=None,  # keepalive is done here, to record proof of life
                close_timeout=1.0,
                max_queue=None,  # never apply backpressure to Twitch; chat bursts are the signal
            )
        except (OSError, WebSocketException, TimeoutError) as exc:
            return f"connect failed: {type(exc).__name__}: {exc}"

        self._nick = f"justinfan{random.randint(10000, 99999)}"  # noqa: S311
        reader = asyncio.create_task(self._read(ws, emit), name="twitch-chat-reader")
        keepalive = asyncio.create_task(self._keepalive(ws), name="twitch-chat-keepalive")
        joiner = asyncio.create_task(self._join_all(ws), name="twitch-chat-joiner")
        tasks = (reader, keepalive, joiner)
        try:
            self._last_alive = self._now()
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send(f"NICK {self._nick}")
            done, _ = await asyncio.wait({reader, keepalive}, return_when=asyncio.FIRST_COMPLETED)
            # Prefer the keepalive verdict: a stall also makes the reader fail, later.
            first = keepalive if keepalive in done else reader
            return first.result()
        except ConnectionClosed as exc:
            return f"connection lost: {exc}"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # abort() drops the socket immediately. A graceful close would wait for a
            # reply that never comes when the network is down.
            transport = cast("asyncio.Transport | None", getattr(ws, "transport", None))
            if transport is not None:
                transport.abort()
            with contextlib.suppress(Exception):
                await asyncio.shield(ws.wait_closed())

    async def _keepalive(self, ws: ClientConnection) -> str:
        settings = self._settings
        while True:
            await asyncio.sleep(settings.keepalive_interval_s)
            try:
                pong = await ws.ping()
                async with asyncio.timeout(settings.keepalive_timeout_s):
                    await pong
            except TimeoutError:
                return f"stalled: no pong within {settings.keepalive_timeout_s}s"
            except ConnectionClosed as exc:
                return f"connection lost: {exc}"
            self._last_alive = self._now()

    async def _join_all(self, ws: ClientConnection) -> None:
        settings = self._settings
        size = settings.join_batch_size
        batches = [self._channels[i : i + size] for i in range(0, len(self._channels), size)]
        with contextlib.suppress(ConnectionClosed):
            for index, batch in enumerate(batches):
                if index:
                    await asyncio.sleep(settings.join_batch_interval_s)
                await ws.send("JOIN " + ",".join(f"#{name}" for name in batch))
            await asyncio.sleep(_JOIN_CONFIRM_TIMEOUT_S)
            missing = sorted(set(self._channels) - self._joined)
            if missing:
                log.warning(
                    "channels not joined (check the names)",
                    channels=missing,
                    waited_s=_JOIN_CONFIRM_TIMEOUT_S,
                )

    async def _read(self, ws: ClientConnection, emit: Emit) -> str:
        try:
            async for frame in ws:
                now = self._now()
                self._last_alive = now
                text = frame if isinstance(frame, str) else frame.decode("utf-8", "replace")
                for line in text.split("\r\n"):
                    if not line:
                        continue
                    try:
                        irc = parse_line(line)
                    except IrcParseError:
                        log.warning("unparseable chat line", line=line[:200])
                        continue
                    reason = await self._handle(ws, irc, now, emit)
                    if reason is not None:
                        return reason
        except ConnectionClosed as exc:
            return f"connection lost: {exc}"
        return "connection closed by server"

    async def _handle(
        self, ws: ClientConnection, irc: IrcMessage, now: float, emit: Emit
    ) -> str | None:
        """Process one line. Return a reason string if the session must end."""
        command = irc.command
        if command in ("PRIVMSG", "USERNOTICE"):
            message = message_from_irc(irc, now)
            if message is not None:
                emit(message)
        elif command == "PING":
            await ws.send("PONG :" + (irc.params[-1] if irc.params else "tmi.twitch.tv"))
        elif command == "JOIN":
            channel = _channel_name(irc.params[0]) if irc.params else ""
            if irc.nick == self._nick and channel and channel not in self._joined:
                self._joined.add(channel)
                self._attempt = 0
                emit(ConnectionEvent(ts=now, channel=channel, event="joined"))
                log.info("chat joined", channel=channel)
        elif command == "RECONNECT":
            return "server requested reconnect"
        elif command == "NOTICE":
            log.warning(
                "chat notice",
                channel=_channel_name(irc.params[0]) if irc.params else None,
                msg_id=irc.tags.get("msg-id"),
                text=irc.params[-1] if irc.params else "",
            )
        return None
