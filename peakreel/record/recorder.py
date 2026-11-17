"""Record live Twitch streams as broadcast-timestamped HLS segments.

Why not ``streamlink | ffmpeg -f segment``
------------------------------------------
Piping streamlink into an ffmpeg segmenter produces one continuous byte stream, and
the only clock left is when bytes arrived. streamlink removes Twitch's embedded ads,
so every ad break would silently shift the video timeline against wall-clock time,
and chat alignment would drift by the length of every ad.

Instead, streamlink runs in-process and a small writer subclass receives each HLS
segment *with its metadata*. Each content segment is saved unchanged (about 2 s of
MPEG-TS), and its ``EXT-X-PROGRAM-DATE-TIME`` (PDT) goes into the session index (see
:mod:`peakreel.record.segments`). Ads become explicit, indexed gaps. No ffmpeg runs
while recording.

The writer hook (``TwitchHLSStreamWriter.write``) is streamlink internal API.
The subclasses are built at runtime from the live stream's own classes, because
streamlink's plugin loader gives them a different identity from a normal import. The
streamlink version is locked in ``uv.lock``, and ``tests/test_recorder.py`` exercises
the hook against a fake Twitch playlist, so an incompatible upgrade fails the tests.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from streamlink.exceptions import PluginError, StreamError
from streamlink.session.session import Streamlink

from peakreel.config import RecordConfig
from peakreel.log import get_logger
from peakreel.record.segments import INDEX_NAME, SegmentEntry

__all__ = ["ChannelRecorder", "SegmentSink", "is_twitch_hls", "open_recording"]

log = get_logger(__name__)

_SINK_ATTRIBUTE = "_peakreel_sink"


class SegmentSink:
    """Write segments and their index for one recording session.

    Thread-safe: streamlink calls :meth:`save` and :meth:`skip` from its writer thread.

    Parameters
    ----------
    session_dir : pathlib.Path
        Created if missing.
    clock : callable, optional
        Returns local epoch seconds, recorded as ``arrival``.

    Attributes
    ----------
    saved, skipped : int
        Counters for this session.
    """

    def __init__(self, session_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.session_dir = session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        self._index = (session_dir / INDEX_NAME).open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._clock = clock
        self._warned_no_pdt = False
        self.saved = 0
        self.skipped = 0
        self._init: str | None = None

    def _append(self, entry: SegmentEntry) -> None:
        with self._lock:
            self._index.write(json.dumps(entry.to_record(), separators=(",", ":")) + "\n")

    def _pdt(self, date: datetime | None) -> float | None:
        if date is None and not self._warned_no_pdt:
            self._warned_no_pdt = True
            log.warning("segment without PROGRAM-DATE-TIME; it cannot be placed in time")
        return None if date is None else round(date.timestamp(), 3)

    def _store(self, name: str, data: bytes) -> None:
        temp = self.session_dir / f"{name}.part"
        temp.write_bytes(data)
        temp.replace(self.session_dir / name)

    def save_init(self, seq: int, data: bytes) -> None:
        """Store a fragmented MP4 init segment (``EXT-X-MAP``).

        Media segments saved afterwards reference it, until the next init arrives.

        Parameters
        ----------
        seq : int
            Sequence number of the first media segment that uses it.
        data : bytes
        """
        name = f"init-{seq}.mp4"
        self._store(name, data)
        self._init = name

    def save(
        self,
        seq: int,
        date: datetime | None,
        duration: float,
        data: bytes,
        *,
        fragmented: bool = False,
    ) -> None:
        """Store a content segment, then index it.

        The file is written to a temporary name and renamed, so a reader never sees a
        partial segment, and the index line is written only after the rename.

        Parameters
        ----------
        seq : int
            HLS media sequence number, used as the file name.
        date : datetime or None
            The segment's PDT.
        duration : float
            Segment length in seconds.
        data : bytes
            The segment payload (MPEG-TS, or an fMP4 fragment).
        fragmented : bool, default False
            Whether this is an fMP4 fragment that needs the current init segment.

        Raises
        ------
        OSError
            If the file cannot be written.
        """
        arrival = round(self._clock(), 3)
        init = self._init if fragmented else None
        if fragmented and init is None:
            log.warning("fMP4 segment before any init segment; it will not be playable", seq=seq)
        name = f"{seq}.m4s" if fragmented else f"{seq}.ts"
        self._store(name, data)
        self._append(SegmentEntry(seq, self._pdt(date), duration, arrival, file=name, init=init))
        self.saved += 1

    def skip(self, seq: int, date: datetime | None, duration: float, reason: str) -> None:
        """Index a segment that was not stored (for example an ad)."""
        arrival = round(self._clock(), 3)
        self._append(SegmentEntry(seq, self._pdt(date), duration, arrival, skipped=reason))
        self.skipped += 1

    def close(self) -> None:
        """Close the index file. Safe to call more than once."""
        with self._lock:
            self._index.close()


def _write_segment(self: Any, segment: Any, result: Any, *data: Any) -> None:
    """``write()`` override: hand each segment to the stream's :class:`SegmentSink`.

    Replaces the normal behaviour (writing into the reader's byte buffer) entirely;
    nothing reads that buffer.
    """
    sink = cast("SegmentSink", getattr(self.stream, _SINK_ATTRIBUTE))
    is_map = bool(data and data[0])
    try:
        if is_map:
            # The init segment of a fragmented MP4 stream, queued before the first
            # media segment that needs it (and again whenever the map changes).
            sink.save_init(segment.num, result.content)
        elif self.should_filter_segment(segment):
            result.raw.drain_conn()
            sink.skip(segment.num, segment.date, segment.duration, "ad")
        else:
            sink.save(
                segment.num,
                segment.date,
                segment.duration,
                result.content,
                fragmented=segment.map is not None,
            )
    except Exception:
        # An exception here would kill streamlink's writer thread silently.
        log.exception("failed to store segment", seq=segment.num)


def _capturing_reader(stream_type: type[Any]) -> type[Any]:
    """Subclass ``stream_type``'s own reader and writer with the capture hook.

    streamlink loads plugins through its own importer, so the stream class a live
    session returns is a different class object from
    ``streamlink.plugins.twitch.TwitchHLSStream`` imported here. Subclassing the
    stream's *own* reader and writer keeps every class from the same module.
    """
    reader_base = cast("type[Any]", stream_type.__reader__)
    writer_base = cast("type[Any]", reader_base.__writer__)
    writer = type("PeakreelSegmentWriter", (writer_base,), {"write": _write_segment})
    return type("PeakreelSegmentReader", (reader_base,), {"__writer__": writer})


def is_twitch_hls(stream: object) -> bool:
    """Whether ``stream`` is streamlink's Twitch HLS stream (from any plugin load)."""
    return type(stream).__name__ == "TwitchHLSStream" and hasattr(type(stream), "__reader__")


def open_recording(stream: Any, sink: SegmentSink) -> Any:
    """Start capturing ``stream`` into ``sink``.

    Parameters
    ----------
    stream : TwitchHLSStream
        A stream from ``Streamlink.streams()``, or one constructed directly from a
        playlist URL (as the tests do).
    sink : SegmentSink

    Returns
    -------
    streamlink.stream.hls.HLSStreamReader
        The running reader. Its ``worker`` thread ends when the playlist ends; call
        ``close()`` to stop early.

    Raises
    ------
    TypeError
        If ``stream`` is not a Twitch HLS stream.
    """
    if not is_twitch_hls(stream):
        raise TypeError(f"not a Twitch HLS stream: {type(stream).__name__}")
    setattr(stream, _SINK_ATTRIBUTE, sink)
    stream.__reader__ = _capturing_reader(cast("type[Any]", type(stream)))
    return stream.open()


def _session_name(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


class ChannelRecorder:
    """Keep one channel recorded while it is live.

    Runs in its own thread (see :meth:`run`): finds the stream, records a session
    until the broadcast ends or the connection fails, then looks again.

    Parameters
    ----------
    channel : str
        Twitch login.
    settings : RecordConfig
    buffer_dir : pathlib.Path
        Root of the rolling buffer; this channel writes to ``buffer_dir/channel``.
    session_factory : callable, optional
        Creates a :class:`streamlink.Streamlink` session. Injectable for tests.
    clock : callable, optional
        Returns local epoch seconds.
    """

    def __init__(
        self,
        channel: str,
        settings: RecordConfig,
        buffer_dir: Path,
        *,
        session_factory: Callable[[], Streamlink] = Streamlink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.channel = channel
        self.settings = settings
        self.channel_dir = buffer_dir / channel
        self._session_factory = session_factory
        self._clock = clock

    def _find_stream(self, session: Streamlink) -> Any:
        try:
            # streamlink's `streams()` is untyped; its result maps quality names to streams.
            lookup = cast("Callable[[str], dict[str, object]]", session.streams)  # pyright: ignore[reportUnknownMemberType]
            streams = lookup(f"https://www.twitch.tv/{self.channel}")
        except PluginError as exc:
            log.warning("stream lookup failed", channel=self.channel, error=str(exc))
            return None
        stream = streams.get(self.settings.quality) or streams.get("best")
        if stream is None:
            return None
        if not is_twitch_hls(stream):
            log.error("unexpected stream type", channel=self.channel, type=type(stream).__name__)
            return None
        return stream

    def record_session(self, stream: Any, stop: threading.Event) -> SegmentSink:
        """Record one session until the playlist ends or ``stop`` is set.

        Parameters
        ----------
        stream : TwitchHLSStream
        stop : threading.Event

        Returns
        -------
        SegmentSink
            The closed sink, with its counters.
        """
        sink = SegmentSink(self.channel_dir / _session_name(self._clock()), clock=self._clock)
        log.info("recording session started", channel=self.channel, session=sink.session_dir.name)
        try:
            reader = open_recording(stream, sink)
        except StreamError as exc:
            sink.close()
            log.warning("could not open stream", channel=self.channel, error=str(exc))
            return sink
        try:
            while not stop.is_set() and reader.worker.is_alive():
                stop.wait(1.0)
        finally:
            reader.close()
            sink.close()
            log.info(
                "recording session ended",
                channel=self.channel,
                session=sink.session_dir.name,
                saved=sink.saved,
                skipped_ads=sink.skipped,
                stopped=stop.is_set(),
            )
        return sink

    def run(self, stop: threading.Event) -> None:
        """Record whenever the channel is live, until ``stop`` is set.

        Parameters
        ----------
        stop : threading.Event
            Set it to stop; the current session is closed cleanly within about 1 s.

        Notes
        -----
        Never raises: lookup and stream errors are logged and retried after
        ``offline_retry_s``.
        """
        session = self._session_factory()
        offline_logged = False
        while not stop.is_set():
            stream = self._find_stream(session)
            if stream is None:
                if not offline_logged:
                    log.info("channel offline", channel=self.channel)
                    offline_logged = True
                stop.wait(self.settings.offline_retry_s)
                continue
            offline_logged = False
            try:
                self.record_session(stream, stop)
            except Exception:
                log.exception("recording session crashed", channel=self.channel)
            if not stop.is_set():
                stop.wait(2.0)  # brief pause before looking for the stream again
