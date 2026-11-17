"""Read, prune and cut the rolling video buffer.

All times here are on the broadcast clock (each segment's PDT, Twitch's clock),
as UTC epoch seconds. Chat timestamps are on the local clock, which ``peakreel
doctor`` keeps within 2 s of Twitch's; callers that start from chat time can subtract
a spike's measured receive delay to convert exactly.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from peakreel.log import get_logger
from peakreel.record.segments import read_index

__all__ = [
    "GAP_TOLERANCE_S",
    "SegmentRef",
    "Window",
    "WindowUnavailable",
    "channel_segments",
    "cut_window",
    "disk_usage",
    "get_window",
    "prune_channel",
]

log = get_logger(__name__)

GAP_TOLERANCE_S = 0.25
"""Space between consecutive segments tolerated before it counts as a gap."""


class WindowUnavailable(LookupError):
    """Raised when the buffer holds no video for a requested window."""


@dataclass(frozen=True, slots=True)
class SegmentRef:
    """A stored segment.

    Attributes
    ----------
    path : pathlib.Path
    pdt : float
        Broadcast time of its first frame.
    duration : float
    """

    path: Path
    pdt: float
    duration: float

    @property
    def end(self) -> float:
        """Broadcast time just after its last frame."""
        return self.pdt + self.duration


@dataclass(frozen=True, slots=True)
class Window:
    """The stored video covering a requested time range.

    Attributes
    ----------
    channel : str
    start, end : float
        The requested range, broadcast clock.
    segments : tuple of SegmentRef
        Every stored segment overlapping the range, in time order.
    offset_s : float
        Where ``start`` falls inside the first segment.
    gaps : tuple of (float, float)
        Parts of the range with no stored video: before the first segment, between
        segments (ad breaks, reconnects) and after the last. Empty means complete.
    """

    channel: str
    start: float
    end: float
    segments: tuple[SegmentRef, ...]
    offset_s: float
    gaps: tuple[tuple[float, float], ...] = field(default_factory=tuple[tuple[float, float], ...])

    @property
    def duration_s(self) -> float:
        """Requested length in seconds."""
        return self.end - self.start

    @property
    def complete(self) -> bool:
        """Whether the whole range is covered without gaps."""
        return not self.gaps


def _session_dirs(channel_dir: Path) -> list[Path]:
    if not channel_dir.is_dir():
        return []
    return sorted(p for p in channel_dir.iterdir() if p.is_dir())


def channel_segments(channel_dir: Path) -> list[SegmentRef]:
    """List every stored, time-placed segment of a channel.

    Parameters
    ----------
    channel_dir : pathlib.Path
        ``<buffer>/<channel>``.

    Returns
    -------
    list of SegmentRef
        Sorted by broadcast time. Segments without a PDT, skipped segments and
        index entries whose file was pruned are left out.
    """
    refs: list[SegmentRef] = []
    for session in _session_dirs(channel_dir):
        for entry in read_index(session):
            if entry.file is None or entry.pdt is None:
                continue
            path = session / entry.file
            if path.exists():
                refs.append(SegmentRef(path, entry.pdt, entry.duration))
    refs.sort(key=lambda r: r.pdt)
    return refs


def get_window(buffer_dir: Path, channel: str, start: float, end: float) -> Window:
    """Return the stored video for ``[start, end)``.

    Parameters
    ----------
    buffer_dir : pathlib.Path
        Buffer root.
    channel : str
    start, end : float
        Broadcast-clock range, UTC epoch seconds.

    Returns
    -------
    Window
        Check :attr:`Window.gaps`: a window that is only partly stored is returned
        with its gaps listed, never silently shortened.

    Raises
    ------
    ValueError
        If ``end <= start``.
    WindowUnavailable
        If no stored segment overlaps the range. The message gives the range the
        buffer does hold.
    """
    if end <= start:
        raise ValueError(f"empty window: start {start} >= end {end}")
    segments = channel_segments(buffer_dir / channel)
    chosen = [s for s in segments if s.pdt < end and s.end > start]
    if not chosen:
        held = f"{segments[0].pdt:.3f}-{segments[-1].end:.3f}" if segments else "nothing"
        raise WindowUnavailable(
            f"{channel}: no video for {start:.3f}-{end:.3f} (buffer holds {held})"
        )
    gaps: list[tuple[float, float]] = []
    if chosen[0].pdt > start + GAP_TOLERANCE_S:
        gaps.append((start, chosen[0].pdt))
    for previous, current in itertools.pairwise(chosen):
        if current.pdt - previous.end > GAP_TOLERANCE_S:
            gaps.append((previous.end, current.pdt))
    if chosen[-1].end < end - GAP_TOLERANCE_S:
        gaps.append((chosen[-1].end, end))
    return Window(
        channel=channel,
        start=start,
        end=end,
        segments=tuple(chosen),
        offset_s=max(0.0, start - chosen[0].pdt),
        gaps=tuple(gaps),
    )


def prune_channel(channel_dir: Path, keep_s: float) -> tuple[int, int]:
    """Delete segments older than ``keep_s`` before the channel's newest video.

    Parameters
    ----------
    channel_dir : pathlib.Path
    keep_s : float
        Seconds of video to keep, measured back from the newest segment's end.

    Returns
    -------
    tuple of (int, int)
        Files deleted and bytes freed.

    Notes
    -----
    Age is measured against the newest stored video, not the wall clock, so the last
    minutes of a broadcast stay available after the channel goes offline. Session
    directories left without segments are removed, except the newest one, which may
    be an active session that has not stored its first segment yet.
    """
    segments = channel_segments(channel_dir)
    if not segments:
        return 0, 0
    cutoff = max(s.end for s in segments) - keep_s
    deleted = freed = 0
    for segment in segments:
        if segment.end < cutoff:
            try:
                size = segment.path.stat().st_size
                segment.path.unlink()
            except FileNotFoundError:
                continue
            deleted += 1
            freed += size
    for session in _session_dirs(channel_dir)[:-1]:
        if not any(session.glob("*.ts")):
            for leftover in session.iterdir():
                leftover.unlink()
            session.rmdir()
    return deleted, freed


def disk_usage(buffer_dir: Path) -> dict[str, int]:
    """Bytes stored per channel.

    Parameters
    ----------
    buffer_dir : pathlib.Path

    Returns
    -------
    dict of str to int
    """
    usage: dict[str, int] = {}
    if not buffer_dir.is_dir():
        return usage
    for channel_dir in sorted(p for p in buffer_dir.iterdir() if p.is_dir()):
        usage[channel_dir.name] = sum(
            f.stat().st_size for f in channel_dir.rglob("*") if f.is_file()
        )
    return usage


def cut_window(window: Window, out: Path, ffmpeg: Path) -> None:
    """Render ``window`` to a standalone MP4 (frame-accurate, re-encoded).

    A verification helper for M3; M4 renders the real clips.

    Parameters
    ----------
    window : Window
    out : pathlib.Path
        Output file, overwritten.
    ffmpeg : pathlib.Path
        The ffmpeg executable.

    Raises
    ------
    subprocess.CalledProcessError
        If ffmpeg fails; ``stderr`` holds its message.

    Notes
    -----
    The concat demuxer joins segments back to back, so if the window has gaps the
    output is shorter than requested and time after a gap is shifted. Check
    :attr:`Window.gaps` first.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="peakreel-cut-") as temp:
        listing = Path(temp) / "segments.txt"
        listing.write_text(
            "".join(f"file '{s.path.resolve().as_posix()}'\n" for s in window.segments),
            encoding="utf-8",
        )
        args = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            listing.name,
            "-ss",
            f"{window.offset_s:.3f}",
            "-t",
            f"{window.duration_s:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(out.resolve()),
        ]
        # cwd is the temp dir so the concat list is a plain relative name (Windows
        # drive letters break ffmpeg's filter and list syntax in places).
        subprocess.run(args, cwd=temp, check=True, capture_output=True, text=True, env=os.environ)
