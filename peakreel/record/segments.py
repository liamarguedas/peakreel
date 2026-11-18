"""On-disk layout of the rolling video buffer.

Layout::

    <buffer>/<channel>/<session>/<seq>.ts
    <buffer>/<channel>/<session>/index.jsonl

A *session* is one continuous recording of a broadcast: it starts when the channel is
found live (or after a reconnect) and ends when the playlist ends or the recorder
stops. Each Twitch HLS segment (about 2 s of MPEG-TS) is stored unchanged, and the
index records its broadcast time.

Index records, one JSON object per line::

    {"seq": 3396, "pdt": 1794904295.275, "duration": 2.0, "arrival": 1794904302.312,
     "file": "3396.ts"}
    {"seq": 3400, "pdt": 1794904303.275, "duration": 2.0, "arrival": 1794904309.1,
     "skipped": "ad"}

Fragmented MP4 (CMAF) streams, which Twitch serves for some channels, also store
their init segment (``EXT-X-MAP``) as ``init-<seq>.mp4``; each media segment
(``<seq>.m4s``) names the init it needs in ``init``.

``pdt`` is the segment's ``EXT-X-PROGRAM-DATE-TIME``: when Twitch says the segment
was broadcast, on Twitch's clock, as UTC epoch seconds. It is ``null`` if Twitch did
not send one, and such segments cannot be placed in time. ``arrival`` is the local
time the segment was downloaded, kept for latency diagnostics.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from peakreel.log import get_logger

__all__ = ["INDEX_NAME", "SegmentEntry", "read_index"]

INDEX_NAME = "index.jsonl"

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SegmentEntry:
    """One line of a session index.

    Attributes
    ----------
    seq : int
        HLS media sequence number.
    pdt : float or None
        Broadcast time of the segment start, UTC epoch seconds (Twitch clock).
    duration : float
        Segment length in seconds.
    arrival : float
        Local download time, UTC epoch seconds.
    file : str or None
        File name inside the session directory, ``None`` when not saved.
    skipped : str or None
        Why the segment was not saved (``"ad"``).
    init : str or None
        For fragmented MP4 streams, the init segment file (``EXT-X-MAP``) that must
        precede this segment's bytes to make them playable. ``None`` for MPEG-TS.
    """

    seq: int
    pdt: float | None
    duration: float
    arrival: float
    file: str | None = None
    skipped: str | None = None
    init: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Return the JSON record, omitting empty optional fields."""
        record: dict[str, Any] = {
            "seq": self.seq,
            "pdt": self.pdt,
            "duration": self.duration,
            "arrival": self.arrival,
        }
        if self.file is not None:
            record["file"] = self.file
        if self.skipped is not None:
            record["skipped"] = self.skipped
        if self.init is not None:
            record["init"] = self.init
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> SegmentEntry:
        """Parse a JSON record.

        Raises
        ------
        KeyError, TypeError, ValueError
            If a field is missing or has the wrong type.
        """
        pdt = record["pdt"]
        return cls(
            seq=int(record["seq"]),
            pdt=None if pdt is None else float(pdt),
            duration=float(record["duration"]),
            arrival=float(record["arrival"]),
            file=cast("str | None", record.get("file")),
            skipped=cast("str | None", record.get("skipped")),
            init=cast("str | None", record.get("init")),
        )


def read_index(session_dir: Path) -> Iterator[SegmentEntry]:
    """Yield the entries of a session index in written order.

    Parameters
    ----------
    session_dir : pathlib.Path

    Yields
    ------
    SegmentEntry

    Notes
    -----
    A missing index yields nothing. A malformed line (for example the torn last line
    of a recorder that was killed) is skipped with a warning.
    """
    path = session_dir / INDEX_NAME
    try:
        handle = path.open(encoding="utf-8")
    except FileNotFoundError:
        return
    with handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield SegmentEntry.from_record(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                log.warning("skipping bad index line", file=str(path), line=number, error=str(exc))
