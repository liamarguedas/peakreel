"""Append chat events to per-channel, per-day JSONL files.

Layout::

    <root>/<channel>/<YYYY-MM-DD>.jsonl

The day is the UTC date of the event's own timestamp, so a file's contents never
depend on when the writer happened to run. Files are opened in append mode and
line-buffered: every event reaches the operating system as one complete line, and a
crash loses at most the event being written.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO

from peakreel.chat.base import ChatEvent, event_to_record

__all__ = ["ChatLogWriter", "log_path"]


def log_path(root: Path, channel: str, ts: float) -> Path:
    """Return the log file that holds events of ``channel`` at time ``ts``.

    Parameters
    ----------
    root : pathlib.Path
        Chat log directory.
    channel : str
        Channel login.
    ts : float
        UTC epoch seconds.

    Returns
    -------
    pathlib.Path

    Examples
    --------
    >>> log_path(Path("data/chat"), "somestreamer", 1794000000.0).as_posix()
    'data/chat/somestreamer/2026-11-06.jsonl'
    """
    day = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    return root / channel / f"{day}.jsonl"


def _ends_mid_line(path: Path) -> bool:
    """Whether an existing file ends without a newline (a torn write from a crash)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


class ChatLogWriter:
    """Write chat events as JSON lines, one file per channel per UTC day.

    Use it as a context manager so files are flushed and closed on every exit path.

    Parameters
    ----------
    root : pathlib.Path
        Chat log directory. Created on first write.

    Attributes
    ----------
    written : int
        Number of events written by this instance.

    Notes
    -----
    If a previous run was killed in the middle of a line, the next write to that
    file starts on a fresh line, so one torn record never corrupts the one after it.

    Examples
    --------
    >>> from peakreel.chat.base import ChatMessage
    >>> with ChatLogWriter(Path("data/chat")) as writer:  # doctest: +SKIP
    ...     writer.write(ChatMessage(ts=1794000000.0, channel="c", user="u", text="hi"))
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._open: dict[str, tuple[Path, TextIO]] = {}
        self.written = 0

    def __enter__(self) -> Self:
        """Return the writer itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close every open file."""
        self.close()

    def _handle_for(self, channel: str, ts: float) -> TextIO:
        path = log_path(self._root, channel, ts)
        current = self._open.get(channel)
        if current is not None:
            if current[0] == path:
                return current[1]
            current[1].close()  # the UTC day changed
        path.parent.mkdir(parents=True, exist_ok=True)
        torn = _ends_mid_line(path)
        handle = path.open("a", encoding="utf-8", newline="\n", buffering=1)
        if torn:
            handle.write("\n")
        self._open[channel] = (path, handle)
        return handle

    def write(self, event: ChatEvent) -> None:
        """Append one event to its channel's file for the event's UTC day.

        Parameters
        ----------
        event : ChatMessage or ConnectionEvent

        Raises
        ------
        OSError
            If the file cannot be opened or written (for example, disk full).
        """
        line = json.dumps(event_to_record(event), ensure_ascii=False, separators=(",", ":"))
        self._handle_for(event.channel, event.ts).write(line + "\n")
        self.written += 1

    def close(self) -> None:
        """Flush and close every open file. Safe to call more than once."""
        for _, handle in self._open.values():
            handle.close()
        self._open.clear()
