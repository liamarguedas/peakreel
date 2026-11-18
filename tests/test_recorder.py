"""Recorder and buffer tests.

The contract test drives streamlink's real Twitch HLS stream class against a local
playlist, so a streamlink upgrade that changes the writer hook fails here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from streamlink.exceptions import PluginError
from streamlink.plugins.twitch import TwitchHLSStream
from streamlink.session.session import Streamlink

from peakreel.config import RecordConfig, resolve_tool
from peakreel.record.buffer import (
    WindowUnavailable,
    channel_segments,
    cut_window,
    disk_usage,
    get_window,
    prune_channel,
)
from peakreel.record.recorder import ChannelRecorder, SegmentSink, is_twitch_hls, open_recording
from peakreel.record.segments import INDEX_NAME, SegmentEntry, read_index

T0 = 1_794_902_400.0  # 2026-11-17T08:00:00Z


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def playlist(*, ad: bool) -> str:
    """5 content segments, with segments 3-4 replaced by a stitched ad when `ad`."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:100"]
    if ad:
        lines.append(
            f'#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",'
            f'START-DATE="{_iso(T0 + 6)}",DURATION=4.000'
        )
    for i in range(7):
        ts = T0 + 2 * i
        is_ad = ad and i in (3, 4)
        if ad and i in (3, 5):
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso(ts)}")
        lines.append(f"#EXTINF:2.000,{'Amazon' if is_ad else 'live'}")
        lines.append(f"seg{100 + i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def fmp4_playlist() -> str:
    """Fragmented MP4 (CMAF) playlist, as Twitch serves some channels."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        "#EXT-X-TARGETDURATION:2",
        "#EXT-X-MEDIA-SEQUENCE:200",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for i in range(3):
        lines += [
            f"#EXT-X-PROGRAM-DATE-TIME:{_iso(T0 + 2 * i)}",
            "#EXTINF:2.000,live",
            f"seg{200 + i}.m4s",
        ]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@pytest.fixture
def hls_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.endswith("fmp4.m3u8"):
                body = fmp4_playlist().encode()
            elif self.path.endswith(".m3u8"):
                body = playlist(ad="ad" in self.path).encode()
            else:
                body = f"payload-{self.path.strip('/')}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def wait_for_index(session_dir: Path, lines: int, timeout: float = 15.0) -> list[SegmentEntry]:
    deadline = time.monotonic() + timeout
    entries: list[SegmentEntry] = []
    while time.monotonic() < deadline:
        entries = list(read_index(session_dir))
        if len(entries) >= lines:
            return entries
        time.sleep(0.05)
    return entries


@pytest.mark.parametrize("with_ad", [False, True])
def test_streamlink_writer_contract(hls_server: str, tmp_path: Path, with_ad: bool) -> None:
    session = Streamlink()
    stream = TwitchHLSStream(session, f"{hls_server}/{'ad' if with_ad else 'live'}.m3u8")
    sink = SegmentSink(tmp_path / "s1", clock=lambda: T0 + 100)
    reader = open_recording(stream, sink)
    try:
        entries = wait_for_index(tmp_path / "s1", 7)
    finally:
        reader.close()
        sink.close()
    assert [e.seq for e in entries] == list(range(100, 107))
    assert [e.pdt for e in entries] == [T0 + 2 * i for i in range(7)]
    assert all(e.arrival == T0 + 100 for e in entries)
    # streamlink zeroes ad durations (live-edge bookkeeping); gaps are measured from PDTs.
    assert all(e.duration == 2.0 for e in entries if e.skipped is None)
    skipped = [e.seq for e in entries if e.skipped == "ad"]
    assert skipped == ([103, 104] if with_ad else [])
    for entry in entries:
        if entry.file is not None:
            assert (
                tmp_path / "s1" / entry.file
            ).read_bytes() == f"payload-seg{entry.seq}.ts".encode()
    assert not list((tmp_path / "s1").glob("*.part"))


def fake_session(streams: Callable[[Streamlink, str], dict[str, object]]) -> type[Streamlink]:
    """A session whose Twitch plugin answers with ``streams(session, url)``."""

    class FakePlugin:
        built: ClassVar[list[str]] = []

        def __init__(self, session: Streamlink, url: str) -> None:
            self.session = session
            self.url = url
            FakePlugin.built.append(url)

        def streams(self) -> dict[str, object]:
            return streams(self.session, self.url)

    class FakeSession(Streamlink):
        plugin = FakePlugin

        def resolve_url(self, url: str, follow_redirect: bool = True) -> tuple[str, Any, str]:  # type: ignore[override]
            return "twitch", FakePlugin, url

    return FakeSession


def test_channel_recorder_records_then_keeps_looking(hls_server: str, tmp_path: Path) -> None:
    lookups: list[str] = []

    def answer(session: Streamlink, url: str) -> dict[str, object]:
        lookups.append(url)
        if len(lookups) == 1:
            return {}  # offline at first
        return {"best": TwitchHLSStream(session, f"{hls_server}/live.m3u8")}

    FakeSession = fake_session(answer)

    settings = RecordConfig(offline_retry_s=0.05)
    recorder = ChannelRecorder("alpha", settings, tmp_path, session_factory=FakeSession)
    stop = threading.Event()
    thread = threading.Thread(target=recorder.run, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(channel_segments(tmp_path / "alpha")) < 7:
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert lookups[0] == "https://www.twitch.tv/alpha"
    assert len(lookups) >= 2
    assert len(channel_segments(tmp_path / "alpha")) >= 7


def test_offline_checks_reuse_one_plugin(tmp_path: Path) -> None:
    # streamlink keeps every plugin instance alive until exit, holding an OS handle,
    # so building one per offline check leaked a handle every 30 s per channel.
    calls: list[str] = []

    def offline(session: Streamlink, url: str) -> dict[str, object]:
        calls.append(url)
        if len(calls) == 3:
            raise PluginError("Twitch is having a moment")
        return {}

    FakeSession = fake_session(offline)
    recorder = ChannelRecorder(
        "alpha", RecordConfig(offline_retry_s=0.01), tmp_path, session_factory=FakeSession
    )
    stop = threading.Event()
    thread = threading.Thread(target=recorder.run, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(calls) < 20:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    built = FakeSession.plugin.built  # type: ignore[attr-defined]
    assert len(calls) >= 20
    assert built == ["https://www.twitch.tv/alpha"] * 2  # once, and once more after the error


# -- buffer ---------------------------------------------------------------------------


def make_session(
    channel_dir: Path, name: str, start: float, count: int, *, ad: tuple[int, ...] = ()
) -> None:
    session = channel_dir / name
    session.mkdir(parents=True)
    with (session / INDEX_NAME).open("w", encoding="utf-8") as index:
        for i in range(count):
            seq = 1000 + i
            pdt = start + 2 * i
            if i in ad:
                entry = SegmentEntry(seq, pdt, 2.0, pdt + 5, skipped="ad")
            else:
                (session / f"{seq}.ts").write_bytes(b"x" * 100)
                entry = SegmentEntry(seq, pdt, 2.0, pdt + 5, file=f"{seq}.ts")
            index.write(json.dumps(entry.to_record()) + "\n")


def test_get_window_offsets_and_gaps(tmp_path: Path) -> None:
    make_session(tmp_path / "alpha", "s1", T0, 10, ad=(4, 5))  # T0..T0+20, ad at +8..+12
    make_session(tmp_path / "alpha", "s2", T0 + 30, 5)  # T0+30..T0+40 after a reconnect

    window = get_window(tmp_path, "alpha", T0 + 1.5, T0 + 7)
    assert [s.pdt for s in window.segments] == [T0, T0 + 2, T0 + 4, T0 + 6]
    assert window.offset_s == 1.5
    assert window.duration_s == 5.5
    assert window.complete

    window = get_window(tmp_path, "alpha", T0 + 6, T0 + 34)
    assert window.gaps == ((T0 + 8, T0 + 12), (T0 + 20, T0 + 30))
    assert not window.complete

    window = get_window(tmp_path, "alpha", T0 - 5, T0 + 1)
    assert window.gaps == ((T0 - 5, T0),)
    assert window.offset_s == 0.0

    with pytest.raises(WindowUnavailable, match="buffer holds"):
        get_window(tmp_path, "alpha", T0 + 100, T0 + 130)
    with pytest.raises(WindowUnavailable, match="holds nothing"):
        get_window(tmp_path, "nobody", T0, T0 + 1)
    with pytest.raises(ValueError, match="empty window"):
        get_window(tmp_path, "alpha", T0 + 5, T0 + 5)


def test_index_tolerates_torn_line_and_missing_pdt(tmp_path: Path) -> None:
    make_session(tmp_path / "alpha", "s1", T0, 3)
    with (tmp_path / "alpha" / "s1" / INDEX_NAME).open("a", encoding="utf-8") as index:
        index.write(
            json.dumps({"seq": 9, "pdt": None, "duration": 2.0, "arrival": 1.0, "file": "9.ts"})
            + "\n"
        )
        index.write('{"seq": 10, "pdt"')  # recorder killed mid-write
    (tmp_path / "alpha" / "s1" / "9.ts").write_bytes(b"x")
    assert [s.pdt for s in channel_segments(tmp_path / "alpha")] == [T0, T0 + 2, T0 + 4]


def test_prune_keeps_recent_video_and_removes_empty_sessions(tmp_path: Path) -> None:
    channel = tmp_path / "alpha"
    make_session(channel, "s1", T0, 10)  # T0..T0+20
    make_session(channel, "s2", T0 + 100, 10)  # T0+100..T0+120
    (channel / "s3").mkdir()  # an active session that has not stored anything yet
    deleted, freed = prune_channel(channel, keep_s=15)
    assert deleted == 10 + 2  # all of s1, and s2's first two segments (end < T0+105)
    assert freed == 12 * 100
    assert not (channel / "s1").exists()
    assert (channel / "s3").exists()
    assert channel_segments(channel)[0].pdt == T0 + 104
    assert disk_usage(tmp_path)["alpha"] > 0
    assert prune_channel(tmp_path / "nobody", keep_s=15) == (0, 0)


@pytest.mark.skipif(resolve_tool("ffmpeg", Path.cwd()) is None, reason="ffmpeg not installed")
def test_cut_window_is_frame_accurate(tmp_path: Path) -> None:
    ffmpeg = resolve_tool("ffmpeg", Path.cwd())
    ffprobe = resolve_tool("ffprobe", Path.cwd())
    assert ffmpeg is not None
    assert ffprobe is not None
    session = tmp_path / "alpha" / "s1"
    session.mkdir(parents=True)
    # Three 2 s MPEG-TS segments with continuous timestamps, like Twitch's.
    subprocess.run(
        [
            str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264",
            "-g", "60", "-keyint_min", "60", "-c:a", "aac", "-f", "segment",
            "-segment_time", "2", "-segment_format", "mpegts", "%d.ts",
        ],
        cwd=session,
        check=True,
    )  # fmt: skip
    with (session / INDEX_NAME).open("w", encoding="utf-8") as index:
        for i in range(3):
            entry = SegmentEntry(i, T0 + 2 * i, 2.0, T0, file=f"{i}.ts")
            index.write(json.dumps(entry.to_record()) + "\n")
    window = get_window(tmp_path, "alpha", T0 + 1.5, T0 + 4.5)
    out = tmp_path / "cut.mp4"
    cut_window(window, out, ffmpeg)
    probe = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(probe.stdout) - 3.0) < 0.1

    # Window starting 1 s before the buffer: output starts at the first frame and
    # still ends at the requested end (regression: it used to run past the end).
    early = get_window(tmp_path, "alpha", T0 - 1, T0 + 3)
    assert early.gaps == ((T0 - 1, T0),)
    cut_window(early, out, ffmpeg)
    probe = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(probe.stdout) - 3.0) < 0.1
    shutil.rmtree(session)


def test_stream_from_streamlinks_plugin_loader_is_accepted(hls_server: str, tmp_path: Path) -> None:
    """Regression: live sessions load the Twitch plugin through streamlink's own
    importer, so its TwitchHLSStream is a different class object than our import."""
    import importlib.util

    import streamlink.plugins.twitch as twitch

    spec = importlib.util.spec_from_file_location("_separately_loaded_twitch", twitch.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # as streamlink's loader does; dataclasses need it
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    foreign_class = module.TwitchHLSStream
    assert foreign_class is not TwitchHLSStream
    stream = foreign_class(Streamlink(), f"{hls_server}/ad.m3u8")
    assert is_twitch_hls(stream)
    sink = SegmentSink(tmp_path / "s1")
    reader = open_recording(stream, sink)
    try:
        entries = wait_for_index(tmp_path / "s1", 7)
    finally:
        reader.close()
        sink.close()
    assert [e.skipped for e in entries].count("ad") == 2
    with pytest.raises(TypeError, match="not a Twitch HLS stream"):
        open_recording(object(), sink)


def test_fmp4_init_segment_is_kept_and_referenced(hls_server: str, tmp_path: Path) -> None:
    """Regression: fMP4 media segments are unplayable without their EXT-X-MAP init."""
    sink = SegmentSink(tmp_path / "s1")
    reader = open_recording(TwitchHLSStream(Streamlink(), f"{hls_server}/fmp4.m3u8"), sink)
    try:
        entries = wait_for_index(tmp_path / "s1", 3)
    finally:
        reader.close()
        sink.close()
    assert [(e.seq, e.file, e.init) for e in entries] == [
        (200, "200.m4s", "init-200.mp4"),
        (201, "201.m4s", "init-200.mp4"),
        (202, "202.m4s", "init-200.mp4"),
    ]
    assert (tmp_path / "s1" / "init-200.mp4").read_bytes() == b"payload-init.mp4"
    assert (tmp_path / "s1" / "201.m4s").read_bytes() == b"payload-seg201.m4s"
    (ref, *_) = channel_segments(tmp_path)
    assert ref.init == tmp_path / "s1" / "init-200.mp4"


@pytest.mark.skipif(resolve_tool("ffmpeg", Path.cwd()) is None, reason="ffmpeg not installed")
def test_cut_window_handles_fragmented_mp4(tmp_path: Path) -> None:
    ffmpeg = resolve_tool("ffmpeg", Path.cwd())
    ffprobe = resolve_tool("ffprobe", Path.cwd())
    assert ffmpeg is not None
    assert ffprobe is not None
    session = tmp_path / "alpha" / "s1"
    session.mkdir(parents=True)
    subprocess.run(
        [
            str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264",
            "-g", "60", "-keyint_min", "60", "-c:a", "aac", "-f", "hls", "-hls_time", "2",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init-0.mp4",
            "-hls_segment_filename", "%d.m4s", "-hls_list_size", "0", "out.m3u8",
        ],
        cwd=session,
        check=True,
    )  # fmt: skip
    with (session / INDEX_NAME).open("w", encoding="utf-8") as index:
        for i in range(3):
            entry = SegmentEntry(i, T0 + 2 * i, 2.0, T0, file=f"{i}.m4s", init="init-0.mp4")
            index.write(json.dumps(entry.to_record()) + "\n")
    # Media segments alone are not decodable: the init segment carries the track setup.
    window = get_window(tmp_path, "alpha", T0 + 1.5, T0 + 4.5)
    out = tmp_path / "cut.mp4"
    cut_window(window, out, ffmpeg)
    probe = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(probe.stdout) - 3.0) < 0.1
