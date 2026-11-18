from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from peakreel.chat.base import ChatEvent, ConnectionEvent, Emit
from peakreel.clip.clipper import Clipper, clip_window_for
from peakreel.config import ClipConfig, Config, load_config
from peakreel.db import connect, init_db
from peakreel.detect.spikes import Spike
from peakreel.notify import Notifier
from peakreel.pipeline import LivePipeline
from peakreel.record.buffer import Window
from peakreel.record.recorder import ChannelRecorder
from tests.conftest import WriteConfig
from tests.test_recorder import make_session
from tests.test_spikes import bucket_messages

T0 = 1_794_900_000.0


def spike(
    start: float = T0 + 100, *, delay: float | None = 0.5, suppressed: str | None = None
) -> Spike:
    return Spike(
        channel="alpha",
        start_ts=start,
        end_ts=start + 20,
        peak_ts=start,
        peak_count=40,
        messages=80,
        unique_chatters=60,
        baseline_median=6.0,
        baseline_mad=1.0,
        score=17.0,
        top_messages=(("lul", 30), ("no way", 5)),
        receive_delay_s=delay,
        suppressed_by=suppressed,
    )


def fake_cutter(calls: list[Window]) -> object:
    def cut(window: Window, out: Path, _ffmpeg: Path) -> None:
        calls.append(window)
        out.write_bytes(b"mp4")

    return cut


@pytest.fixture
def config(write_config: WriteConfig) -> Config:
    path = write_config(
        {
            "channels": [{"name": "alpha", "credit": "Clip from twitch.tv/alpha (Alpha)"}],
            "clip": {"pre_roll_s": 20, "post_roll_s": 10, "max_wait_s": 0.3},
        }
    )
    config = load_config(path)
    init_db(config.db_path)
    return config


def clipper(config: Config, calls: list[Window], **kwargs: object) -> Clipper:
    kwargs.setdefault("renderer", None)
    return Clipper(config, Path("ffmpeg"), cutter=fake_cutter(calls), poll_s=0.05, **kwargs)  # type: ignore[arg-type]


def rows(config: Config, sql: str) -> list[dict[str, object]]:
    conn = connect(config.db_path)
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def test_clip_window_converts_chat_time_to_broadcast_time() -> None:
    settings = ClipConfig(pre_roll_s=20, post_roll_s=10)
    assert clip_window_for(spike(T0, delay=0.5), settings) == (T0 - 20.5, T0 + 9.5)
    assert clip_window_for(spike(T0, delay=None), settings) == (T0 - 20, T0 + 10)


async def test_clipped_spike_writes_clip_sidecar_and_rows(config: Config) -> None:
    make_session(config.buffer_dir / "alpha", "s1", T0, 100)  # T0 .. T0+200
    calls: list[Window] = []
    result = await clipper(config, calls).handle(spike())
    assert result.status == "clipped"
    assert result.clip_path is not None
    assert result.sidecar_path is not None
    assert result.clip_path.read_bytes() == b"mp4"
    assert result.clip_path.parent == config.outbox_dir / "alpha"
    assert not list(result.clip_path.parent.glob("*.part.mp4"))
    (window,) = calls
    assert (window.start, window.end) == (T0 + 79.5, T0 + 109.5)
    assert window.complete

    sidecar = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["channel"] == "alpha"
    assert sidecar["credit"] == "Clip from twitch.tv/alpha (Alpha)"
    assert sidecar["source"] == "https://www.twitch.tv/alpha"
    assert sidecar["spike"]["score"] == 17.0
    assert sidecar["spike"]["top_messages"][0] == {"text": "lul", "count": 30}
    assert sidecar["window"]["gaps"] == []
    assert sidecar["window"]["start_ts"] == T0 + 79.5

    (spike_row,) = rows(config, "SELECT * FROM spikes")
    assert spike_row["suppressed_by"] is None
    assert spike_row["receive_delay_s"] == 0.5
    assert json.loads(str(spike_row["top_messages"]))[0] == ["lul", 30]
    (clip_row,) = rows(config, "SELECT * FROM clips")
    assert clip_row["state"] == "new"
    assert clip_row["spike_id"] == spike_row["id"]
    assert clip_row["path"] == str(result.clip_path)
    (channel_row,) = rows(config, "SELECT * FROM channels")
    assert channel_row["credit"] == "Clip from twitch.tv/alpha (Alpha)"


async def test_suppressed_spike_is_recorded_without_clip(config: Config) -> None:
    calls: list[Window] = []
    result = await clipper(config, calls).handle(spike(suppressed="raid"))
    assert result.status == "suppressed"
    assert calls == []
    assert rows(config, "SELECT suppressed_by FROM spikes") == [{"suppressed_by": "raid"}]
    assert rows(config, "SELECT * FROM clips") == []


async def test_blocklist_is_checked_before_cutting(config: Config) -> None:
    make_session(config.buffer_dir / "alpha", "s1", T0, 100)
    config.blocklist_path.write_text("channels: [alpha]\n", encoding="utf-8")
    calls: list[Window] = []
    result = await clipper(config, calls).handle(spike())
    assert result.status == "blocked"
    assert calls == []
    assert len(rows(config, "SELECT * FROM spikes")) == 1  # the decision stays traceable


async def test_takedown_during_cut_discards_the_clip(config: Config) -> None:
    make_session(config.buffer_dir / "alpha", "s1", T0, 100)

    def cut_then_block(window: Window, out: Path, _ffmpeg: Path) -> None:
        out.write_bytes(b"mp4")
        config.blocklist_path.write_text("channels: [alpha]\n", encoding="utf-8")

    result = await Clipper(config, Path("ffmpeg"), cutter=cut_then_block, renderer=None).handle(
        spike()
    )
    assert result.status == "blocked"
    assert not list((config.outbox_dir / "alpha").iterdir())
    assert rows(config, "SELECT * FROM clips") == []


async def test_unreadable_blocklist_fails_closed(config: Config) -> None:
    make_session(config.buffer_dir / "alpha", "s1", T0, 100)
    config.blocklist_path.write_text("channels: nope\n", encoding="utf-8")
    calls: list[Window] = []
    assert (await clipper(config, calls).handle(spike())).status == "blocked"
    assert calls == []


async def test_no_video_and_cut_failure_are_reported_not_raised(config: Config) -> None:
    calls: list[Window] = []
    assert (await clipper(config, calls).handle(spike())).status == "no_video"

    make_session(config.buffer_dir / "alpha", "s1", T0, 100)

    def broken(window: Window, out: Path, _ffmpeg: Path) -> None:
        out.write_bytes(b"half")
        raise RuntimeError("ffmpeg exploded")

    result = await Clipper(config, Path("ffmpeg"), cutter=broken, renderer=None).handle(spike())
    assert result.status == "failed"
    assert not list((config.outbox_dir / "alpha").glob("*"))


async def test_waits_for_late_video_then_clips(config: Config, write_config: WriteConfig) -> None:
    config = load_config(
        write_config({"channels": [{"name": "alpha"}], "clip": {"max_wait_s": 10}})
    )
    init_db(config.db_path)
    make_session(config.buffer_dir / "alpha", "s1", T0, 50)  # ends at T0+100: window not covered

    def arrive_later() -> None:
        time.sleep(0.4)
        make_session(config.buffer_dir / "alpha", "s2", T0 + 100, 20)  # T0+100 .. T0+140

    threading.Thread(target=arrive_later).start()
    calls: list[Window] = []
    result = await clipper(config, calls).handle(spike())
    assert result.status == "clipped"
    assert calls[0].complete


async def test_window_still_short_after_max_wait_is_cut_with_gap(config: Config) -> None:
    make_session(config.buffer_dir / "alpha", "s1", T0, 52)  # ends at T0+104, window ends T0+109.5
    calls: list[Window] = []
    result = await clipper(config, calls).handle(spike())
    assert result.status == "clipped"
    assert result.gaps == ((T0 + 104, T0 + 109.5),)
    assert result.sidecar_path is not None
    sidecar = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["window"]["gaps"][0]["seconds"] == 5.5


async def test_live_pipeline_turns_a_chat_burst_into_a_clip(config: Config) -> None:
    """Synthetic chat -> real detector -> real clipper -> clip on disk and in SQLite."""
    make_session(config.buffer_dir / "alpha", "s1", T0, 150)  # T0 .. T0+300 of "video"
    levels = [6, 5, 7, 6, 5, 6, 7, 5, 6, 6] * 2 + [40, 45, 6, 6, 6]
    events: list[ChatEvent] = [ConnectionEvent(ts=T0, channel="alpha", event="joined")]
    for index, count in enumerate(levels):
        events.extend(bucket_messages(index, count, channel="alpha"))
    done = asyncio.Event()

    class FakeChat:
        async def run(self, emit: Emit) -> None:
            for event in events:
                emit(event)
            done.set()
            try:
                await asyncio.Event().wait()
            finally:
                emit(
                    ConnectionEvent(
                        ts=T0 + 260, channel="alpha", event="stopped", last_alive_ts=T0 + 260
                    )
                )

    class IdleRecorder(ChannelRecorder):
        def run(self, stop: threading.Event) -> None:
            stop.wait()

    calls: list[Window] = []
    pipeline = LivePipeline(
        config,
        ["alpha"],
        Path("ffmpeg"),
        source=FakeChat(),
        clipper=clipper(config, calls),
        recorder_factory=lambda name: IdleRecorder(name, config.record, config.buffer_dir),
        clock=lambda: T0 + 400,
        notifier=Notifier(config.notify, senders=[]),
    )
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(done.wait(), 5)
    await asyncio.sleep(1.5)  # one ticker pass closes the burst
    pipeline.stop_requested.set()
    await asyncio.wait_for(runner, 10)

    clipped = [r for r in pipeline.results if r.status == "clipped"]
    assert len(clipped) == 1
    # Burst starts at bucket 20 (T0+200); bucket_messages stamps server_ts 0.25 s earlier.
    assert (calls[0].start, calls[0].end) == (T0 + 200 - 0.25 - 20, T0 + 200 - 0.25 + 10)
    assert len(rows(config, "SELECT * FROM clips")) == 1
    log_files = list(config.chat_log_dir.rglob("*.jsonl"))
    assert log_files  # chat is still logged to JSONL by the live pipeline
    assert '"event":"stopped"' in log_files[0].read_text(encoding="utf-8").splitlines()[-1]


async def test_render_runs_after_publish_and_failure_keeps_clip(config: Config) -> None:
    from peakreel.render.vertical import RenderedClip, RenderError

    make_session(config.buffer_dir / "alpha", "s1", T0, 100)

    def fake_render(cfg: Config, clip: Path, _ffmpeg: Path, **_: object) -> RenderedClip:
        vertical, thumb = clip.with_name(clip.stem + ".vertical.mp4"), clip.with_suffix(".jpg")
        vertical.write_bytes(b"v")
        thumb.write_bytes(b"j")
        return RenderedClip(vertical, thumb, "blur")

    calls: list[Window] = []
    result = await clipper(config, calls, renderer=fake_render).handle(spike())
    assert result.vertical_path is not None
    assert result.vertical_path.read_bytes() == b"v"
    (row,) = rows(config, "SELECT vertical_path, thumbnail_path FROM clips")
    assert row == {
        "vertical_path": str(result.vertical_path),
        "thumbnail_path": str(result.thumbnail_path),
    }

    def broken_render(cfg: Config, clip: Path, _ffmpeg: Path, **_: object) -> RenderedClip:
        raise RenderError("no font")

    result = await clipper(config, calls, renderer=broken_render).handle(spike(T0 + 150))
    assert result.status == "clipped"
    assert result.vertical_path is None
    assert result.clip_path is not None
    assert result.clip_path.exists()
