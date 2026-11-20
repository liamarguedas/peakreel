from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from peakreel.chat.base import ChatEvent, ChatMessage, ConnectionEvent, Emit
from peakreel.cli import app as cli_app
from peakreel.clip.clipper import ClipResult
from peakreel.config import Config, NotifyConfig, load_config
from peakreel.db import connect, ensure_channel, init_db, insert_clip
from peakreel.detect.spikes import Spike
from peakreel.locks import ChannelLocks, LockHeld
from peakreel.notify import Notification, Notifier
from peakreel.pipeline import LivePipeline
from peakreel.record.buffer import Window
from peakreel.record.recorder import ChannelRecorder
from peakreel.review.app import create_app
from peakreel.review.store import PROCESSING_GRACE_S, suggest_title
from peakreel.safety.checks import Flag
from peakreel.twitch.helix import Credentials, HelixClient, StreamInfo
from tests.conftest import MemoryKeyring, WriteConfig
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
        # The burst is an excited chat (LOL): unexcited spikes are dropped before cutting.
        text = "LOL" if count > 20 else "hi"
        events.extend(bucket_messages(index, count, channel=channel, text=text))
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
    kwargs.setdefault("helix", None)
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
    # After shutdown every channel reports stopped, not "restarting".
    assert {c["state"] for c in status["channels"].values()} == {"stopped"}


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
    conn = connect(config.db_path)
    clip_path = config.outbox_dir / "bad" / "earlier.mp4"
    earlier = insert_clip(
        conn, channel_id=ensure_channel(conn, "bad", "bad"), spike_id=None,
        start_ts=T0, end_ts=T0 + 30, path=clip_path, sidecar_path=clip_path,
    )  # fmt: skip
    conn.commit()
    conn.close()
    config.blocklist_path.write_text("channels: [bad]\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pipeline.states["bad"].blocked:
        await asyncio.sleep(0.05)
    # The next status write shows it; wait for it rather than a fixed time (the write
    # also samples the machine, which takes a moment).
    path = config.run_dir / "status.json"
    status: dict[str, Any] = {}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if status.get("channels", {}).get("bad", {}).get("state") == "blocked":
            break
        await asyncio.sleep(0.05)
    assert stopped == ["bad"]
    assert not (config.buffer_dir / "bad").exists()
    assert not pipeline._chat_allowed("bad")  # pyright: ignore[reportPrivateUsage]
    assert status["channels"]["bad"]["state"] == "blocked"
    assert status["channels"]["good"]["state"] == "running"
    conn = connect(config.db_path)
    (state,) = conn.execute("SELECT state FROM clips WHERE id = ?", (earlier,)).fetchone()
    conn.close()
    assert state == "blocked"  # clips cut before the takedown can no longer be published
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
        "UPDATE clips SET vertical_path = ?, thumbnail_path = ?, flags = '[]' WHERE id = ?",
        (str(clip_dir / "c1.vertical.mp4"), str(clip_dir / "c1.jpg"), clip_id),
    )
    conn.commit()
    conn.close()
    return config, TestClient(create_app(config), base_url="http://127.0.0.1"), clip_id


def test_review_list_approve_and_reject(review: tuple[Config, TestClient, int]) -> None:
    config, client, clip_id = review
    (clip,) = client.get("/api/clips?state=new").json()
    assert clip["channel"] == "alpha"
    assert clip["has_vertical"] and clip["has_thumbnail"]
    assert clip["captions"]["status"] == "ok"

    body = {"state": "approved", "title": "Nice one", "description": "wow"}
    approved = client.post(f"/api/clips/{clip_id}", json=body).json()
    assert approved["state"] == "approved"
    assert (approved["title"], approved["description"]) == ("Nice one", "wow")
    assert not config.export_dir.exists()  # no copy for posting by hand any more
    assert client.get("/api/clips?state=new").json() == []

    rejected = client.post(f"/api/clips/{clip_id}", json={"state": "rejected"}).json()
    assert rejected["state"] == "rejected"
    assert rejected["title"] == "Nice one"  # omitted fields are unchanged


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


def test_takedown_blocks_existing_clips(
    review: tuple[Config, TestClient, int],
) -> None:
    config, client, clip_id = review
    assert client.post(f"/api/clips/{clip_id}", json={"state": "approved"}).status_code == 200
    config.blocklist_path.write_text("channels: [alpha]\n", encoding="utf-8")

    # Approving again after the takedown is refused and blocks the clip.
    refused = client.post(f"/api/clips/{clip_id}", json={"state": "approved"})
    assert refused.status_code == 409
    assert "takedown" in refused.json()["detail"]
    (clip,) = client.get("/api/clips").json()
    assert clip["state"] == "blocked"


def test_listing_applies_takedowns_without_a_pipeline(
    review: tuple[Config, TestClient, int],
) -> None:
    config, client, clip_id = review
    config.blocklist_path.write_text("channels: [alpha]\n", encoding="utf-8")
    assert client.get("/api/clips?state=new").json() == []
    (clip,) = client.get("/api/clips?state=blocked").json()
    assert clip["id"] == clip_id


def test_unreadable_blocklist_refuses_approval(review: tuple[Config, TestClient, int]) -> None:
    config, client, clip_id = review
    config.blocklist_path.write_text("channels: [unclosed\n", encoding="utf-8")
    refused = client.post(f"/api/clips/{clip_id}", json={"state": "approved"})
    assert refused.status_code == 409
    assert "blocklist unreadable" in refused.json()["detail"]
    assert not config.export_dir.exists() or not any(config.export_dir.iterdir())
    # Rejecting needs no blocklist: it can only make publishing less likely.
    assert client.post(f"/api/clips/{clip_id}", json={"state": "rejected"}).status_code == 200


@pytest.mark.parametrize(
    ("top", "expected"),
    [
        ([["https://twitter.com/avashaka", 9], ["GG", 4]], "GG"),
        ([["follow twitter.com/x", 3], ["@someone lol", 3], ["nice", 2]], "nice"),
        ([["www.spam.example", 3]], "💀"),
        ([], "💀"),  # no name: it is on the video
        ([["正解", 17]], "正解"),
    ],
)
def test_suggested_title_skips_links_and_mentions(top: list[list[object]], expected: str) -> None:
    assert suggest_title("gaules", top) == expected


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


# -- go-live polling, clip context, notifications (M7a) ---------------------------------


class FakeHelix(HelixClient):
    def __init__(self, answers: list[dict[str, StreamInfo]]) -> None:
        super().__init__(Credentials("cid", "secret"))
        self.answers = answers
        self.calls: list[list[str]] = []

    def live_streams(self, logins: Sequence[str]) -> dict[str, StreamInfo]:
        self.calls.append(sorted(logins))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


async def test_stream_polling_and_clip_context(two_channels: Config) -> None:
    config = two_channels.model_copy(
        update={"golive": two_channels.golive.model_copy(update={"poll_s": 0.05})}
    )
    gta = StreamInfo("good", "Grand Theft Auto VI", "launch", 900, T0 - 3600, "pt")
    helix = FakeHelix([{"good": gta}, {"good": gta}, {}])
    alert = ChatMessage(T0 + 50, "good", "fan", "", "usernotice", tags={"msg-id": "subgift"})
    cheer = ChatMessage(T0 + 70, "good", "fan2", "Cheer100", tags={"bits": "100"})
    plain = ChatMessage(T0 + 60, "good", "viewer", "lol")

    class IdleRecorder(ChannelRecorder):
        def run(self, stop: threading.Event) -> None:
            stop.wait()

    pipeline, fed, _ = supervised(config, [alert, plain, cheer], IdleRecorder, helix=helix)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(fed.wait(), 5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and pipeline.states["good"].stream is None:
        await asyncio.sleep(0.02)
    context = pipeline.clip_context("good", T0 + 40, T0 + 65)
    assert context.chat == (alert,)  # only alert-bearing chat, only inside the window
    assert context.stream is not None
    assert context.stream["game"] == "Grand Theft Auto VI"
    assert pipeline.clip_context("bad", T0, T0 + 100).chat == ()
    assert pipeline.clip_context("unknown", T0, T0 + 1).stream is None
    pipeline._write_status(running=True)  # pyright: ignore[reportPrivateUsage]
    status = json.loads((config.run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["channels"]["good"]["live"] is True
    assert status["channels"]["good"]["game"] == "Grand Theft Auto VI"
    assert status["channels"]["bad"]["live"] is False

    while time.monotonic() < deadline and pipeline.states["good"].stream is not None:
        await asyncio.sleep(0.02)
    assert pipeline.states["good"].stream is None  # went offline
    assert helix.calls[0] == ["bad", "good"]
    pipeline.stop_requested.set()
    await asyncio.wait_for(runner, 15)


async def test_duplicates_are_not_notified(two_channels: Config) -> None:
    sent: list[Notification] = []

    class ScriptedClipper:
        def __init__(self) -> None:
            self.results = [
                ClipResult(1, "clipped", clip_id=1, flags=(Flag("music", "m"),)),
                ClipResult(2, "clipped", clip_id=2, duplicate_of=1),
            ]

        async def handle(self, spike: Spike) -> ClipResult:
            return self.results.pop(0)

        async def resume(self) -> int:
            return 0

    pipeline = LivePipeline(
        two_channels, ["good"], Path("ffmpeg"), source=None,
        clipper=ScriptedClipper(),  # type: ignore[arg-type]
        notifier=Notifier(two_channels.notify, senders=[sent.append]), helix=None,
    )  # fmt: skip
    spike = Spike(
        channel="good", start_ts=T0, end_ts=T0 + 20, peak_ts=T0, peak_count=40, messages=80,
        unique_chatters=60, baseline_median=6.0, baseline_mad=1.0, score=9.0,
        top_messages=(("gg", 5),), receive_delay_s=0.5,
    )  # fmt: skip
    await pipeline._handle(spike)  # pyright: ignore[reportPrivateUsage]
    await pipeline._handle(spike)  # pyright: ignore[reportPrivateUsage]
    assert len(sent) == 1
    assert "1 flag(s)" in sent[0].body
    assert pipeline.states["good"].clips == 2


def test_review_lists_duplicates_separately_and_shows_flags(
    review: tuple[Config, TestClient, int],
) -> None:
    config, client, clip_id = review
    flags = [{"kind": "music", "message": "music likely (beat score 0.70)"}]
    conn = connect(config.db_path)
    channel_id = ensure_channel(conn, "alpha")
    other = insert_clip(
        conn, channel_id=channel_id, spike_id=None, start_ts=T0 + 1, end_ts=T0 + 31,
        path=config.outbox_dir / "alpha" / "c2.mp4",
        sidecar_path=config.outbox_dir / "alpha" / "c2.json",
    )  # fmt: skip
    conn.execute(
        "UPDATE clips SET flags = ?, duplicate_of = NULL WHERE id = ?", (json.dumps(flags), clip_id)
    )
    conn.execute(
        "UPDATE clips SET duplicate_of = ?, match_strength = 21.5 WHERE id = ?", (clip_id, other)
    )
    conn.commit()
    conn.close()

    (kept,) = client.get("/api/clips?state=new").json()
    assert kept["id"] == clip_id
    assert kept["flags"] == flags
    (duplicate,) = client.get("/api/clips?state=duplicate").json()
    assert (duplicate["id"], duplicate["duplicate_of"], duplicate["match_strength"]) == (
        other, clip_id, 21.5,
    )  # fmt: skip
    assert len(client.get("/api/clips").json()) == 2

    # Flags never block approval.
    approved = client.post(f"/api/clips/{clip_id}", json={"state": "approved"}).json()
    assert (approved["state"], approved["flags"]) == ("approved", flags)


async def test_stream_polling_survives_unexpected_errors(two_channels: Config) -> None:
    config = two_channels.model_copy(
        update={"golive": two_channels.golive.model_copy(update={"poll_s": 0.05})}
    )
    gta = StreamInfo("good", "Grand Theft Auto VI", "launch", 900, T0 - 3600, "pt")

    class FlakyHelix(FakeHelix):
        def live_streams(self, logins: Sequence[str]) -> dict[str, StreamInfo]:
            if not self.calls:
                self.calls.append([])
                raise KeyError("started_at")  # an unexpected payload
            return super().live_streams(logins)

    class IdleRecorder(ChannelRecorder):
        def run(self, stop: threading.Event) -> None:
            stop.wait()

    helix = FlakyHelix([{"good": gta}])
    pipeline, fed, _ = supervised(config, [], IdleRecorder, helix=helix)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(fed.wait(), 5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and pipeline.states["good"].stream is None:
        await asyncio.sleep(0.02)
    assert pipeline.states["good"].stream == gta
    pipeline.stop_requested.set()
    await asyncio.wait_for(runner, 15)


def test_published_tab_lists_clips_on_a_platform(review: tuple[Config, TestClient, int]) -> None:
    config, client, clip_id = review
    conn = connect(config.db_path)
    rows = [
        ("en", "youtube", "private", "published", "https://youtu.be/abc"),
        ("en", "facebook", "private", "private_only", None),
        ("en", "tiktok", "export", "exported", None),
    ]
    for account, platform, mode, state, url in rows:
        conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, url, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)",
            (clip_id, account, platform, mode, state, url, T0, T0),
        )
    conn.commit()
    conn.close()

    (clip,) = client.get("/api/clips?state=published").json()
    assert clip["id"] == clip_id
    assert {p["platform"]: p["url"] for p in clip["publications"]}["youtube"] == (
        "https://youtu.be/abc"
    )

    # Exported packages are not on a platform yet, so they do not count.
    conn = connect(config.db_path)
    conn.execute("DELETE FROM publications WHERE state != 'exported'")
    conn.commit()
    conn.close()
    assert client.get("/api/clips?state=published").json() == []


# -- review login (phone approval) -------------------------------------------------------


def _login_app(
    write_config: WriteConfig, review_settings: dict[str, object]
) -> tuple[Config, TestClient]:
    config = load_config(write_config({"review": review_settings}))
    return config, TestClient(create_app(config), base_url="http://127.0.0.1")


def test_reachable_review_page_needs_a_password(
    write_config: WriteConfig, memory_keyring: MemoryKeyring
) -> None:
    from peakreel.config import ConfigError

    config = load_config(write_config({"review": {"public_hosts": ["pc.tail1.ts.net"]}}))
    with pytest.raises(ConfigError, match="review password"):
        create_app(config)
    assert not load_config(write_config()).review.login_required()


def test_review_login_flow(write_config: WriteConfig, memory_keyring: MemoryKeyring) -> None:
    from peakreel.review.auth import set_password

    set_password("correct horse battery")
    _config, client = _login_app(write_config, {"public_hosts": ["pc.tail1.ts.net"]})

    # Without a session: pages redirect to the login, the API and media answer 401.
    page = client.get("/", follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (303, "/login")
    assert client.get("/api/clips").status_code == 401
    assert client.get("/media/1/vertical").status_code == 401
    assert client.get("/login").status_code == 200

    assert client.post("/api/login", json={"password": "wrong password!!"}).status_code == 401
    ok = client.post("/api/login", json={"password": "correct horse battery"})
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/clips").status_code == 200
    assert client.get("/api/settings").json()["login"] is True

    # A tampered cookie is refused; logging out ends the session.
    good = client.cookies.get("peakreel_session")
    assert good
    client.cookies.clear()
    client.cookies.set("peakreel_session", good[:-2] + "xx")
    assert client.get("/api/clips").status_code == 401
    client.cookies.clear()
    client.cookies.set("peakreel_session", good)
    assert client.get("/api/clips").status_code == 200
    client.cookies.clear()
    client.post("/api/login", json={"password": "correct horse battery"})
    out = client.post("/api/logout", json={})
    assert "max-age=0" in out.headers["set-cookie"].lower()
    assert client.get("/api/clips").status_code == 401

    # The public host is accepted; any other host is not.
    other = TestClient(client.app, base_url="http://pc.tail1.ts.net")
    assert other.get("/login").status_code == 200
    assert TestClient(client.app, base_url="http://evil.example").get("/login").status_code == 400


def test_review_login_throttles_guessing(
    write_config: WriteConfig, memory_keyring: MemoryKeyring
) -> None:
    from peakreel.review.auth import set_password

    set_password("correct horse battery")
    _config, client = _login_app(write_config, {"host": "192.168.1.20"})
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope nope nope"}).status_code == 401
    locked = client.post("/api/login", json={"password": "correct horse battery"})
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


def test_new_password_ends_sessions(
    write_config: WriteConfig, memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    import peakreel.review.app as review_app
    from peakreel.review.auth import set_password

    set_password("correct horse battery")
    monkeypatch.setattr(review_app, "_CREDENTIALS_TTL_S", 0.0)
    _config, client = _login_app(write_config, {"public_hosts": ["pc.tail1.ts.net"]})
    client.post("/api/login", json={"password": "correct horse battery"})
    assert client.get("/api/clips").status_code == 200
    set_password("another long password")
    assert client.get("/api/clips").status_code == 401


def test_cli_review_password(memory_keyring: MemoryKeyring, write_config: WriteConfig) -> None:
    from peakreel.review.auth import load_credentials

    path = write_config()
    runner = CliRunner()
    short = runner.invoke(cli_app, ["-c", str(path), "review", "password"], input="short\nshort\n")
    assert short.exit_code == 1 and load_credentials() is None
    good = "correct horse battery\n" * 2
    result = runner.invoke(cli_app, ["-c", str(path), "review", "password"], input=good)
    assert result.exit_code == 0, result.output
    creds = load_credentials()
    assert creds is not None and creds.check_password("correct horse battery")
    runner.invoke(cli_app, ["-c", str(path), "review", "password", "--clear"])
    assert load_credentials() is None


def test_clip_is_reviewable_only_once_processed(review: tuple[Config, TestClient, int]) -> None:
    # Seen live: a clip approved 15 s after its row appeared, before its render, flags
    # and discovered permission were recorded, went out private instead of public.
    config, client, clip_id = review
    conn = connect(config.db_path)
    conn.execute(
        "UPDATE clips SET flags = NULL, created_at = ? WHERE id = ?", (time.time(), clip_id)
    )
    conn.commit()
    assert client.get("/api/clips?state=new").json() == []
    refused = client.post(f"/api/clips/{clip_id}", json={"state": "approved"})
    assert refused.status_code == 409 and "still being processed" in refused.json()["detail"]

    # A clip whose processing never finished (crashed run) is shown after the grace.
    old = (
        time.time() - PROCESSING_GRACE_S - 60
    )  # past the grace (exactly at it flaked: same clock tick)
    conn.execute("UPDATE clips SET created_at = ? WHERE id = ?", (old, clip_id))
    conn.commit()
    conn.close()
    assert [c["id"] for c in client.get("/api/clips?state=new").json()] == [clip_id]


def test_activity_feed_shows_pipeline_and_publishing_events(
    review: tuple[Config, TestClient, int],
) -> None:
    config, client, _clip_id = review
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"event": "chat throughput", "timestamp": "2026-11-18T23:50:00Z"},  # noise: left out
        {"event": "clip ready", "clip_id": 7, "channel": "speed", "score": 9.4,
         "timestamp": "2026-11-18T23:51:00Z"},
        {"event": "publication", "clip_id": 7, "account": "en", "platform": "youtube",
         "state": "failed", "detail": "quota", "timestamp": "2026-11-18T23:52:00Z"},
    ]  # fmt: skip
    config.log_file.write_text(
        "first line may be cut\n" + "\n".join(json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )
    feed = client.get("/api/activity").json()
    assert [(a["kind"], a["text"], a["tag"]) for a in feed] == [
        ("problem", "Clip #7 not posted, gave up: quota", "YouTube en"),
        ("detect", "Clip #7 cut, chat score 9.4", "speed"),
    ]


def test_activity_keeps_rare_events_among_many_skips() -> None:
    from peakreel.review.activity import activity

    skip = '{"event": "spike suppressed", "channel": "a", "reason": "budget"}'
    same = ('{"event": "same moment found", "clip_id": 9, "channel": "b", "other": 4, '
            '"other_channel": "a", "strength": 58.7}')
    feed = activity([same] + [skip] * 500, limit=100)
    kinds = [a["kind"] for a in feed]
    assert kinds.count("skip") == 100 and kinds.count("duplicate") == 1
    assert feed[-1]["text"] == "Clip #9 is the same moment as clip #4 from a"
    assert feed[-1]["tag"] == "b"


def test_to_review_holds_clips_until_auto_review_decides(
    review: tuple[Config, TestClient, int],
) -> None:
    from peakreel.review.store import list_clips

    config, _client, clip_id = review
    conn = connect(config.db_path)
    hold = 3600.0

    def listed(state: str) -> list[int]:
        return [v.id for v in list_clips(conn, state, ai_hold_s=hold)]

    def auto(record: dict[str, str] | None, age_s: float = 0.0) -> None:
        conn.execute(
            "UPDATE clips SET auto = ?, created_at = ? WHERE id = ?",
            (record and json.dumps(record), time.time() - age_s, clip_id),
        )
        conn.commit()

    auto(None)  # not decided yet: the AI is checking it
    assert (listed("new"), listed("checking")) == ([], [clip_id])
    auto({"mode": "shadow", "decision": "review"})  # shadow decisions are redone in on mode
    assert listed("new") == []
    auto({"mode": "on", "decision": "review"})  # left for a person
    assert (listed("new"), listed("checking")) == ([clip_id], [])
    auto(None, age_s=hold + 60)  # auto review stalled: shown anyway, never hidden for good
    assert (listed("new"), listed("checking")) == ([clip_id], [])
    assert [v.id for v in list_clips(conn, "new")] == [clip_id]  # no hold outside on mode
    conn.close()


def test_recording_switch(review: tuple[Config, TestClient, int]) -> None:
    config, client, _ = review
    status_path = config.run_dir / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = datetime.now(UTC).isoformat()
    status_path.write_text(json.dumps({"running": True, "updated_utc": fresh}), encoding="utf-8")
    assert client.get("/api/status").json()["running"] is True
    # Off: the pipeline's stop file; on while it still runs is refused.
    assert client.post("/api/pipeline", json={"on": False}).status_code == 200
    assert (config.run_dir / "pipeline.stop").exists()
    assert client.get("/api/status").json()["stopping"] is True
    assert client.post("/api/pipeline", json={"on": True}).status_code == 409
    # A killed pipeline leaves running=true behind: stale means not running.
    stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    status_path.write_text(json.dumps({"running": True, "updated_utc": stale}), encoding="utf-8")
    assert client.get("/api/status").json()["running"] is False
