"""Streaming platforms: how a channel is named, linked and credited.

Every channel has a *key* used everywhere inside Peakreel (folders, database,
locks, chat events): a Twitch login as is (``xqc``), or ``kick-`` plus the Kick
slug (``kick-westcol``). Twitch logins cannot contain ``-``, so the two never
collide.

Examples
--------
>>> channel_key("kick", "WestCol")
'kick-westcol'
>>> split_key("kick-westcol"), split_key("xqc")
(('kick', 'westcol'), ('twitch', 'xqc'))
>>> channel_url("kick-westcol"), channel_url("xqc")
('kick.com/westcol', 'twitch.tv/xqc')
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "KICK_PREFIX",
    "StreamPlatform",
    "channel_key",
    "channel_link",
    "channel_name",
    "channel_url",
    "split_key",
]

StreamPlatform = Literal["twitch", "kick"]

KICK_PREFIX = "kick-"


def channel_key(platform: StreamPlatform, name: str) -> str:
    """Return the internal key of ``name`` on ``platform``."""
    name = name.strip().lstrip("#@").lower()
    return f"{KICK_PREFIX}{name}" if platform == "kick" else name


def split_key(key: str) -> tuple[StreamPlatform, str]:
    """Return the platform and the platform's own channel name of ``key``."""
    if key.startswith(KICK_PREFIX):
        return "kick", key[len(KICK_PREFIX) :]
    return "twitch", key


def channel_name(key: str) -> str:
    """Return the channel name on its platform (Twitch login or Kick slug)."""
    return split_key(key)[1]


def channel_url(key: str) -> str:
    """Return the channel address as written in credits (``twitch.tv/x``)."""
    platform, name = split_key(key)
    return f"kick.com/{name}" if platform == "kick" else f"twitch.tv/{name}"


def channel_link(key: str) -> str:
    """Return the channel full URL."""
    platform, name = split_key(key)
    return f"https://kick.com/{name}" if platform == "kick" else f"https://www.twitch.tv/{name}"
