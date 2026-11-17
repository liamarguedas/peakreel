"""Platform-neutral chat event types and their JSON record format.

A chat source emits two kinds of events:

- :class:`ChatMessage`: something a viewer (or the platform, for raids and subs) said.
- :class:`ConnectionEvent`: a change in ingest liveness for one channel.

Connection events are stored in the same log as messages. That makes every hole in
the data explainable from the log alone: a quiet chat has no connection events around
it, an outage does.

Record format
-------------
One JSON object per line. Messages::

    {"kind": "msg", "ts": 1794000000.123, "server_ts": 1794000000.05,
     "channel": "somestreamer", "user": "viewer1", "display_name": "Viewer1",
     "user_id": "123", "msg_id": "abc-def", "type": "privmsg", "text": "LETS GO",
     "filtered": null, "tags": {"subscriber": "1"}}

Connection events::

    {"kind": "conn", "ts": 1794000000.5, "channel": "somestreamer",
     "event": "disconnected", "reason": "stalled: no pong within 3.0s",
     "last_alive_ts": 1794000000.1}

All timestamps are UTC wall-clock epoch seconds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

__all__ = [
    "ChatEvent",
    "ChatMessage",
    "ChatSource",
    "ConnectionEvent",
    "ConnectionEventName",
    "Emit",
    "MessageType",
    "RecordError",
    "event_from_record",
    "event_to_record",
]

MessageType = Literal["privmsg", "usernotice"]
ConnectionEventName = Literal["joined", "disconnected", "stopped"]

_MESSAGE_TYPES: frozenset[str] = frozenset({"privmsg", "usernotice"})
_CONNECTION_EVENTS: frozenset[str] = frozenset({"joined", "disconnected", "stopped"})


class RecordError(ValueError):
    """Raised when a JSON record cannot be converted to an event."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message.

    Attributes
    ----------
    ts : float
        Local receive time, UTC epoch seconds. This is the clock used to align chat
        with recorded video.
    channel : str
        Channel login, lower case, without ``#``.
    user : str
        Sender login, lower case.
    text : str
        Message text. Empty for platform notices without a user message.
    type : {"privmsg", "usernotice"}
        ``privmsg`` is a normal chat line. ``usernotice`` is a platform event such
        as a raid, sub or gift (the exact kind is in ``tags["msg-id"]``).
    server_ts : float or None
        Time the platform stamped on the message, UTC epoch seconds. Diagnostic
        only: it shows network delay but is not used for alignment.
    display_name : str or None
        Sender name as shown in chat.
    user_id : str or None
        Platform user id.
    msg_id : str or None
        Platform message id.
    filtered : str or None
        ``None`` for a normal message, otherwise the reason it is excluded from
        detection (``"bot"`` or ``"command"``). Filtered messages are still logged.
    tags : dict of str to str
        Selected platform metadata (badges, bits, raid viewer count, ...).
    """

    ts: float
    channel: str
    user: str
    text: str
    type: MessageType = "privmsg"
    server_ts: float | None = None
    display_name: str | None = None
    user_id: str | None = None
    msg_id: str | None = None
    filtered: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """A change in ingest liveness for one channel.

    Attributes
    ----------
    ts : float
        When the change was observed, UTC epoch seconds.
    channel : str
        Channel login.
    event : {"joined", "disconnected", "stopped"}
        ``joined``: the platform confirmed we are receiving this channel.
        ``disconnected``: the connection was lost or judged dead.
        ``stopped``: the recorder shut down on purpose.
    reason : str or None
        Why the connection ended. ``None`` for ``joined``.
    last_alive_ts : float or None
        For ``disconnected`` and ``stopped``: the last moment the connection was
        proven alive (a frame or a keepalive pong arrived). Data may be missing from
        this time until the next ``joined``.
    """

    ts: float
    channel: str
    event: ConnectionEventName
    reason: str | None = None
    last_alive_ts: float | None = None


ChatEvent = ChatMessage | ConnectionEvent
Emit = Callable[[ChatEvent], None]


class ChatSource(Protocol):
    """A live chat source for one platform.

    Implementations push events to a callback instead of exposing an iterator, so
    that cancelling :meth:`run` is the single, unambiguous way to stop and clean up.
    """

    async def run(self, emit: Emit) -> None:
        """Connect and deliver events until cancelled.

        Parameters
        ----------
        emit : callable
            Called synchronously for every event, in arrival order. It must not
            block for long.

        Notes
        -----
        Implementations reconnect on their own and never return normally. On
        cancellation they emit a ``stopped`` :class:`ConnectionEvent` for every
        joined channel before the cancellation propagates.
        """
        ...


def event_to_record(event: ChatEvent) -> dict[str, Any]:
    """Convert an event to its JSON-serialisable record.

    Parameters
    ----------
    event : ChatMessage or ConnectionEvent

    Returns
    -------
    dict
        Record with a fixed key order, so identical events serialise to identical
        bytes.

    Examples
    --------
    >>> event_to_record(ConnectionEvent(ts=1.0, channel="a", event="joined"))
    {'kind': 'conn', 'ts': 1.0, 'channel': 'a', 'event': 'joined', 'reason': None, \
'last_alive_ts': None}
    """
    if isinstance(event, ConnectionEvent):
        return {
            "kind": "conn",
            "ts": event.ts,
            "channel": event.channel,
            "event": event.event,
            "reason": event.reason,
            "last_alive_ts": event.last_alive_ts,
        }
    return {
        "kind": "msg",
        "ts": event.ts,
        "server_ts": event.server_ts,
        "channel": event.channel,
        "user": event.user,
        "display_name": event.display_name,
        "user_id": event.user_id,
        "msg_id": event.msg_id,
        "type": event.type,
        "text": event.text,
        "filtered": event.filtered,
        "tags": dict(sorted(event.tags.items())),
    }


def _number(record: Mapping[str, Any], key: str, *, required: bool) -> float | None:
    value = record.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecordError(f"{key!r} must be a number, got {value!r}")
    return float(value)


def _text(record: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    value = record.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RecordError(f"{key!r} must be a string, got {value!r}")
    return value


def event_from_record(record: Mapping[str, Any]) -> ChatEvent:
    """Convert a JSON record back to an event.

    This is the exact inverse of :func:`event_to_record`.

    Parameters
    ----------
    record : mapping
        A decoded JSON line.

    Returns
    -------
    ChatMessage or ConnectionEvent

    Raises
    ------
    RecordError
        If ``kind`` is unknown or a field is missing or has the wrong type.
    """
    kind = record.get("kind")
    ts = cast("float", _number(record, "ts", required=True))
    channel = cast("str", _text(record, "channel", required=True))
    if kind == "conn":
        event = _text(record, "event", required=True)
        if event not in _CONNECTION_EVENTS:
            raise RecordError(f"unknown connection event {event!r}")
        return ConnectionEvent(
            ts=ts,
            channel=channel,
            event=cast("ConnectionEventName", event),
            reason=_text(record, "reason", required=False),
            last_alive_ts=_number(record, "last_alive_ts", required=False),
        )
    if kind == "msg":
        message_type = _text(record, "type", required=True)
        if message_type not in _MESSAGE_TYPES:
            raise RecordError(f"unknown message type {message_type!r}")
        tags: object = record.get("tags")
        if tags is None:
            tags = {}
        if not isinstance(tags, dict):
            raise RecordError(f"'tags' must be an object, got {tags!r}")
        return ChatMessage(
            ts=ts,
            channel=channel,
            user=cast("str", _text(record, "user", required=True)),
            text=cast("str", _text(record, "text", required=True)),
            type=cast("MessageType", message_type),
            server_ts=_number(record, "server_ts", required=False),
            display_name=_text(record, "display_name", required=False),
            user_id=_text(record, "user_id", required=False),
            msg_id=_text(record, "msg_id", required=False),
            filtered=_text(record, "filtered", required=False),
            tags={str(k): str(v) for k, v in cast("dict[object, object]", tags).items()},
        )
    raise RecordError(f"unknown record kind {kind!r}")
