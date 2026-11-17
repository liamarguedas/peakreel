from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from peakreel.chat.base import ChatEvent, ConnectionEvent, Emit
from peakreel.cli import app as cli_app
from peakreel.config import Config, NotifyConfig, load_config
from peakreel.db import connect, ensure_channel, init_db, insert_clip
from peakreel.locks import ChannelLocks, LockHeld
from peakreel.notify import Notification, Notifier
from peakreel.pipeline import LivePipeline
from peakreel.record.buffer import Window
from peakreel.record.recorder import ChannelRecorder
from peakreel.review.app import create_app
from tests.conftest import WriteConfig
from tests.test_clipper import fake_cutter
from tests.test_recorder import make_session
from tests.test_spikes import bucket_messages

T0 = 1_794_900_000.0

# -- locks ------------------------------------------------------------------------------


def test_channel_locks_are_exclusive_across_processes(tmp_path: Path) -> None:
    code = (
        "import sys; from pathlib import Path; from peakreel.locks import ChannelLocks, LockHeld\n"
        "try:\n    ChannelLocks(Path(sys.argv[1]), ['b']).acquire(); print('acquired')\n"
        "except LockHeld as exc: print('held', exc.owner_pid)\n"
    )
    with ChannelLocks(tmp_path, ["a", "b"]) as locks:
        assert locks.held == ("a", "b")
        out = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
        )
        assert out.stdout.split() == ["held", str(__import__("os").getpid())]
    out = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
    )
    assert out.stdout.strip() == "acquired"


def test_lock_acquisition_is_all_or_nothing(tmp_path: Path) -> None:
    with ChannelLocks(tmp_path, ["b"]):
        other = ChannelLocks(tmp_path, ["a", "b"])
        with pytest.raises(LockHeld, match="#b is already recorded"):
            other.acquire()
        assert other.held == ()
        ChannelLocks(tmp_path, ["a"]).acquire()  # "a" was released again


# -- CLI guards -------------------------------------------------------------------------


def test_run_start_enforces_allowlist(write_config: WriteConfig) -> None:
    path = write_config({"channels": [{"name": "alpha"}, {"name": "beta", "enabled": False}]})
    runner = CliRunner()
    result = runner.invoke(cli_app, ["-c", str(path), "run", "start", "-C", "gamma"])
    assert result.exit_code == 1
    assert "not on the allowlist" in result.output
    result = runner.invoke(cli_app, ["-c", str(path), "run", "start", "-C", "beta"])
    assert "not on the allowlist" in result.output  # disabled counts as not allowed


def test_chat_record_refuses_a_channel_owned_elsewhere(write_config: WriteConfig) -> None:
    path = write_config({"chat": {"url": "ws://127.0.0.1:9"}})
    config = load_config(path)
    with ChannelLocks(config.run_dir / "locks", ["alpha"]):
        result = CliRunner().invoke(cli_app, ["-c", str(path), "chat", "record", "-C", "alpha"])
    assert result.exit_code == 1
    assert "already recorded" in result.output


# -- supervisor ---------------------------------------------------------------------------


def burst_events(channel: str) -> list[ChatEvent]:
    levels = [6, 5, 7, 6, 5, 6, 7, 5, 6, 6] * 2 + [40, 45, 6, 6, 6]
    events: list[ChatEvent] = [ConnectionEvent(ts=T0, channel=channel, event="joined")]
    for index, count in enumerate(levels):
        events.extend(bucket_messages(index, count, channel=channel))
    return events


def supervised(
    config: Config, events: list[ChatEvent], recorder_cls: type[ChannelRecorder], **kwargs: object
) -> tuple[LivePipeline, asyncio.Event, list[Window]]:
    fed = asyncio.Event()

    class FakeChat:
        async def run(self, emit: Emit) -> None:
            for event in sorted(events, key=lambda e: (e.ts, e.channel)):
                emit(event)
            fed.set()
            await asyncio.Event().wait()

    from peakreel.clip.clipper import Clipper

    calls: list[Window] = []
    clipper = Clipper(config, Path("ffmpeg"), cutter=fake_cutter(calls), renderer=None, poll_s=0.05)  # type: ignore[arg-type]
    pipeline = LivePipeline(
        config,
        [c.name for c in config.channels],
        Path("ffmpeg"),
        source=FakeChat(),
        clipper=clipper,
        recorder_factory=lambda name: recorder_cls(name, config.record, config.buffer_dir),
        clock=lambda: T0 + 400,
        notifier=Notifier(config.notify, senders=[]),
        **kwargs,  # type: ignore[arg-type]
    )
    return pipeline, fed, calls


@pytest.fixture
def two_channels(write_config: WriteConfig) -> Config:
    path = write_config(
        {
            "channels": [{"name": "good"}, {"name": "bad"}],
            "clip": {"max_wait_s": 0.3},
            "captions": {"enabled": False},
            "supervisor": {
                "restart_initial_s": 0.05,
                "restart_max_s": 0.1,
                "blocklist_check_s": 0.2,
                "status_interval_s": 0.2,
            },
        }
    )
    config = load_config(path)
    init_db(config.db_path)
    return config


async def test_crashing_recorder_is_restarted_and_other_channel_still_clips(
    two_channels: Config,
) -> None:
    config = two_channels
    make_session(config.buffer_dir / "good", "s1", T0, 150)

    class FlakyRecorder(ChannelRecorder):
        def run(self, stop: threading.Event) -> None:
            if self.channel == "bad":
                raise RuntimeError("streamlink exploded")
            stop.wait()

    pipeline, fed, calls = supervised(config, burst_events("good"), FlakyRecorder)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(fed.wait(), 5)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (
        pipeline.states["bad"].restarts < 2 or not pipeline.results
    ):
        await asyncio.sleep(0.1)
    pipeline.stop_requested.set()
    await asyncio.wait_for(runner, 15)

    assert pipeline.states["bad"].restarts >= 2
    assert pipeline.states["good"].restarts == 0
    assert [r.status for r in pipeline.results] == ["clipped"]
    assert calls[0].channel == "good"
    status = json.loads((config.run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["running"] is False
    assert status["channels"]["bad"]["restarts"] >= 2


async def test_crashing_detector_is_rebuilt_without_affecting_others(
    two_channels: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = two_channels
    make_session(config.buffer_dir / "good", "s1", T0, 150)

    class IdleRecorder(ChannelRecorder):
        def run(self, stop: threading.Event) -> None:
            stop.wait()

    pipeline, fed, _ = supervised(config, burst_events("good") + burst_events("bad"), IdleRecorder)
    original = pipeline.states["bad"].detector

    def explode(event: ChatEvent) -> list[object]:
        raise ValueError("bad state")

    monkeypatch.setattr(original, "feed", explode)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(fed.wait(), 5)
    await asyncio.sleep(1.5)
    pipeline.stop_requested.set()
    await asyncio.wait_for(runner, 15)

    assert pipeline.states["bad"].detector_restarts >= 1
    assert pipeline.states["bad"].detector is not original
    assert any(r.status == "clipped" for r in pipeline.results)


async def test_takedown_while_running_stops_channel_and_deletes_buffer(
    two_channels: Config,
) -> None:
    config = two_channels
    make_session(config.buffer_dir / "bad", "s1", T0, 10)
    stopped: list[str] = []

    class IdleRecorder(ChannelRecorder):
        def run(self, stop: threading.Event) -> None:
            stop.wait()
            stopped.append(self.channel)

    pipeline, fed, _ = supervised(config, [], IdleRecorder)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(fed.wait(), 5)
    config.blocklist_path.write_text("channels: [bad]\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pipeline.states["bad"].blocked:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    assert stopped == ["bad"]
    assert not (config.buffer_dir / "bad").exists()
    assert not pipeline._chat_allowed("bad")  # pyright: ignore[reportPrivateUsage]
    status = json.loads((config.run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["channels"]["bad"]["state"] == "blocked"
    assert status["channels"]["good"]["state"] == "running"
    pipeline.stop_requested.set()
    await asyncio.wait_for(runner, 15)


# -- review app ------------------------------------------------------------------------------


@pytest.fixture
def review(write_config: WriteConfig) -> tuple[Config, TestClient, int]:
    config = load_config(
        write_config({"channels": [{"name": "alpha", "credit": "Alpha on Twitch"}]})
    )
    init_db(config.db_path)
    clip_dir = config.outbox_dir / "alpha"
    clip_dir.mkdir(parents=True)
    horizontal = clip_dir / "c1.mp4"
    horizontal.write_bytes(b"h" * 5000)
    (clip_dir / "c1.vertical.mp4").write_bytes(b"v" * 5000)
    (clip_dir / "c1.jpg").write_bytes(b"jpg")
    (clip_dir / "c1.ass").write_text("[Script Info]\n", encoding="utf-8")
    (clip_dir / "c1.json").write_text(
        json.dumps({"captions": {"status": "ok", "language": "en"}}), encoding="utf-8"
    )
    conn = connect(config.db_path)
    channel_id = ensure_channel(conn, "alpha", "Alpha on Twitch")
    clip_id = insert_clip(
        conn, channel_id=channel_id, spike_id=None, start_ts=T0, end_ts=T0 + 30,
        path=horizontal, sidecar_path=clip_dir / "c1.json",
    )  # fmt: skip
    conn.execute(
        "UPDATE clips SET vertical_path = ?, thumbnail_path = ? WHERE id = ?",
        (str(clip_dir / "c1.vertical.mp4"), str(clip_dir / "c1.jpg"), clip_id),
    )
    conn.commit()
    conn.close()
    return config, TestClient(create_app(config), base_url="http://127.0.0.1"), clip_id


def test_review_list_approve_export_and_reject(review: tuple[Config, TestClient, int]) -> None:
    config, client, clip_id = review
    (clip,) = client.get("/api/clips?state=new").json()
    assert clip["channel"] == "alpha"
    assert clip["has_vertical"] and clip["has_thumbnail"]
    assert clip["captions"]["status"] == "ok"

    body = {"state": "approved", "title": "Nice one", "description": "wow"}
    approved = client.post(f"/api/clips/{clip_id}", json=body).json()
    assert approved["state"] == "approved"
    assert approved["exported"]
    (export,) = list(config.export_dir.glob("*/*"))
    assert sorted(p.name for p in export.iterdir()) == [
        "caption.txt",
        "captions.ass",
        "clip.mp4",
        "metadata.json",
        "thumbnail.jpg",
    ]
    assert (export / "clip.mp4").read_bytes() == b"v" * 5000  # the vertical render is exported
    assert (export / "caption.txt").read_text(
        encoding="utf-8"
    ) == "Nice one\n\nwow\n\nAlpha on Twitch · twitch.tv/alpha\n"
    metadata = json.loads((export / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["title"] == "Nice one"
    assert metadata["source"] == "https://www.twitch.tv/alpha"
    assert client.get("/api/clips?state=new").json() == []

    rejected = client.post(f"/api/clips/{clip_id}", json={"state": "rejected"}).json()
    assert rejected["state"] == "rejected"
    assert rejected["title"] == "Nice one"  # omitted fields are unchanged
    assert not export.exists()


def test_review_protections(review: tuple[Config, TestClient, int]) -> None:
    config, client, clip_id = review
    assert client.get("/api/clips", headers={"host": "evil.example"}).status_code == 400
    text_post = client.post(
        f"/api/clips/{clip_id}",
        content='{"state": "approved"}',
        headers={"content-type": "text/plain"},
    )
    assert text_post.status_code == 422
    assert client.post(f"/api/clips/{clip_id}", json={"state": "blocked"}).status_code == 422
    assert client.post(f"/api/clips/{clip_id}", json={"stat": "approved"}).status_code == 422
    conn = connect(config.db_path)
    conn.execute("UPDATE clips SET state = 'blocked' WHERE id = ?", (clip_id,))
    conn.commit()
    conn.close()
    blocked = client.post(f"/api/clips/{clip_id}", json={"state": "approved"})
    assert blocked.status_code == 409
    assert "takedown" in blocked.json()["detail"]
    assert client.post("/api/clips/999", json={"state": "approved"}).status_code == 409


def test_review_media_supports_range_and_stays_in_data_dir(
    review: tuple[Config, TestClient, int], tmp_path: Path
) -> None:
    config, client, clip_id = review
    partial = client.get(f"/media/{clip_id}/vertical", headers={"range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert client.get(f"/media/{clip_id}/thumbnail").content == b"jpg"
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"secret")
    conn = connect(config.db_path)
    conn.execute("UPDATE clips SET path = ? WHERE id = ?", (str(outside), clip_id))
    conn.commit()
    conn.close()
    assert client.get(f"/media/{clip_id}/horizontal").status_code == 404
    assert client.get("/").status_code == 200
    assert client.get("/api/status").json() == {"running": False}


# -- notifier ---------------------------------------------------------------------------------


def test_notifier_failures_are_logged_not_raised() -> None:
    sent: list[Notification] = []

    def broken(_: Notification) -> None:
        raise OSError("no network")

    notifier = Notifier(NotifyConfig(), senders=[broken, sent.append])
    assert notifier.send(Notification("t", "b")) == 1
    assert len(sent) == 1
    assert not Notifier(NotifyConfig(desktop=False)).enabled
    assert Notifier(NotifyConfig(desktop=False, ntfy_topic="x")).enabled
