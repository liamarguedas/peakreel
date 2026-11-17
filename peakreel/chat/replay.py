"""Read recorded chat logs back as events.

Replay is deterministic: the same files always produce the same event sequence.
Within a channel, events come out in the order they were recorded. Across channels,
streams are merged by timestamp, with the channel name as the tie-breaker.
"""

from __future__ import annotations

import dataclasses
import heapq
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from peakreel.chat.base import ChatEvent, ChatMessage, RecordError, event_from_record
from peakreel.log import get_logger

if TYPE_CHECKING:
    from peakreel.detect.filters import ChatFilter

__all__ = ["find_log_files", "read_events", "read_file", "read_log"]

log = get_logger(__name__)


def find_log_files(paths: Iterable[Path]) -> dict[str, list[Path]]:
    """Locate chat log files and group them by channel.

    Parameters
    ----------
    paths : iterable of pathlib.Path
        Any mix of ``.jsonl`` files, channel directories, and chat log roots.
        Directories are searched recursively.

    Returns
    -------
    dict of str to list of pathlib.Path
        Channel name (the file's parent directory name) to its files in date order.
        Channels are in alphabetical order.

    Raises
    ------
    FileNotFoundError
        If a given path does not exist.
    """
    found: set[Path] = set()
    for path in paths:
        if path.is_dir():
            found.update(p.resolve() for p in path.rglob("*.jsonl"))
        elif path.is_file():
            found.add(path.resolve())
        else:
            raise FileNotFoundError(f"no such chat log: {path}")
    grouped: dict[str, list[Path]] = {}
    for file in sorted(found, key=lambda p: (p.parent.name, p.name)):
        grouped.setdefault(file.parent.name, []).append(file)
    return grouped


def read_file(path: Path) -> Iterator[ChatEvent]:
    """Yield the events of one log file in recorded order.

    Parameters
    ----------
    path : pathlib.Path
        A ``.jsonl`` chat log.

    Yields
    ------
    ChatMessage or ConnectionEvent

    Notes
    -----
    Blank lines are ignored. A line that is not valid JSON or not a valid record
    (for example, a line torn by a crash) is skipped and logged as a warning with
    its file and line number. It is never silently dropped.
    """
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise RecordError("record is not a JSON object")
                yield event_from_record(record)  # pyright: ignore[reportUnknownArgumentType]
            except (json.JSONDecodeError, RecordError) as exc:
                log.warning(
                    "skipping bad chat log line", file=str(path), line=number, error=str(exc)
                )


def _read_channel(files: list[Path]) -> Iterator[ChatEvent]:
    for file in files:
        yield from read_file(file)


def read_events(paths: Iterable[Path]) -> Iterator[ChatEvent]:
    """Yield every recorded event, messages and connection events alike.

    Parameters
    ----------
    paths : iterable of pathlib.Path
        Files or directories, see :func:`find_log_files`.

    Yields
    ------
    ChatMessage or ConnectionEvent
        Merged across channels by ``(ts, channel)``. Within a channel the recorded
        order is preserved.

    Raises
    ------
    FileNotFoundError
        If a given path does not exist.
    """
    streams = [_read_channel(files) for files in find_log_files(paths).values()]
    yield from heapq.merge(*streams, key=lambda event: (event.ts, event.channel))


def read_log(
    paths: Iterable[Path], *, reclassify: ChatFilter | None = None
) -> Iterator[ChatMessage]:
    """Yield recorded chat messages. This is the input of the offline detector.

    Parameters
    ----------
    paths : iterable of pathlib.Path
        Files or directories, see :func:`find_log_files`.
    reclassify : ChatFilter or None, default None
        When given, the stored ``filtered`` flag of each message is replaced by this
        filter's verdict. Use it to try new bot lists or command prefixes on old
        logs. When ``None``, messages keep the flag recorded at ingest time.

    Yields
    ------
    ChatMessage
        Filtered messages are included; check ``message.filtered``.

    Raises
    ------
    FileNotFoundError
        If a given path does not exist.

    Examples
    --------
    >>> for message in read_log([Path("data/chat")]):  # doctest: +SKIP
    ...     if message.filtered is None:
    ...         print(message.ts, message.channel, message.user, message.text)
    """
    for event in read_events(paths):
        if not isinstance(event, ChatMessage):
            continue
        if reclassify is not None:
            event = dataclasses.replace(event, filtered=reclassify.classify(event))
        yield event
