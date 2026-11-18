"""Takedown blocklist.

``blocklist.yaml`` lists channels whose content must never be recorded or kept.
It is consulted before chat recording starts and (from M4) before any clip is written.

File format::

    channels:
      - name: somestreamer
        reason: "asked for removal by email"
        date: 2026-11-19

A plain string entry (``- somestreamer``) is accepted as shorthand.

Kick channels: ``platform: kick`` on the entry, or a ``kick.com/<slug>`` link, or
the internal key ``kick-<slug>``::

    channels:
      - name: westcol
        platform: kick
      - kick.com/spreen
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from peakreel.config import ConfigError
from peakreel.platforms import KICK_PREFIX, channel_key

__all__ = ["load_blocklist"]


def load_blocklist(path: Path) -> frozenset[str]:
    """Read the set of blocked channel logins.

    Parameters
    ----------
    path : pathlib.Path
        Path to ``blocklist.yaml``.

    Returns
    -------
    frozenset of str
        Lower-case Twitch logins without a leading ``#``.

    Raises
    ------
    ConfigError
        If the file is missing, unreadable or malformed. A missing blocklist is an
        error on purpose: silently treating it as empty could publish content from a
        streamer who asked for removal.

    Examples
    --------
    >>> blocked = load_blocklist(Path("blocklist.yaml"))  # doctest: +SKIP
    >>> "somestreamer" in blocked  # doctest: +SKIP
    False
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read blocklist {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"blocklist {path} is not valid YAML: {exc}") from exc
    if raw is None:
        return frozenset()
    entries: object = (
        cast("dict[str, object]", raw).get("channels") if isinstance(raw, dict) else None
    )
    if entries is None:
        entries = []
    if not isinstance(raw, dict) or not isinstance(entries, list):
        raise ConfigError(f"blocklist {path} must be a mapping with a `channels` list")
    names: set[str] = set()
    for entry in cast("list[object]", entries):
        fields = cast("dict[str, object]", entry) if isinstance(entry, dict) else {}
        name = fields.get("name") if isinstance(entry, dict) else entry
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"blocklist {path}: entry without a channel name: {entry!r}")
        names.add(_key(name, fields.get("platform")))
    return frozenset(names)


def _key(name: str, platform: object) -> str:
    """Return the channel key of a blocklist entry (Twitch login or ``kick-<slug>``)."""
    text = name.strip().lstrip("#@").lower()
    for prefix in ("https://", "http://", "www."):
        text = text.removeprefix(prefix)
    if text.startswith("kick.com/"):
        return channel_key("kick", text.removeprefix("kick.com/").strip("/"))
    if text.startswith("twitch.tv/"):
        return text.removeprefix("twitch.tv/").strip("/")
    if platform == "kick" and not text.startswith(KICK_PREFIX):
        return channel_key("kick", text)
    return text
