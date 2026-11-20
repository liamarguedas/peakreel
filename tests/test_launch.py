from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from peakreel import cli
from peakreel import health as health_mod
from peakreel.cleanup import run_cleanup
from peakreel.config import Config, load_config
from peakreel.db import connect, ensure_channel, init_db, insert_clip
from peakreel.health import sample, sample_machine
from peakreel.log import configure_logging
from peakreel.notify import Notifier
from peakreel.pipeline import LivePipeline
from peakreel.review.store import get_clip, list_clips
from tests.conftest import WriteConfig

NOW = 1_795_000_000.0
DAY = 86400.0


@pytest.fixture
def setup(write_config: WriteConfig) -> tuple[Config, sqlite3.Connection]:
    config = load_config(
        write_config({"retention": {"rejected_days": 2, "new_days": 7, "min_free_gb": 0}})
    )
    init_db(config.db_path)
    return config, connect(config.db_path)


def add_clip(
    config: Config, conn: sqlite3.Connection, name: str, state: str, age_days: float
) -> int:
    folder = config.outbox_dir / "alpha"
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in (".mp4", ".vertical.mp4", ".jpg", ".ass", ".fp"):
        (folder / f"{name}{suffix}").write_bytes(b"x" * 1000)
    (folder / f"{name}.json").write_bytes(b"x" * 1000)  # damaged sidecar: must not break lists
    clip_id = insert_clip(
        conn, channel_id=ensure_channel(conn, "alpha"), spike_id=None, start_ts=NOW,
        end_ts=NOW + 30, path=folder / f"{name}.mp4", sidecar_path=folder / f"{name}.json",
    )  # fmt: skip
    conn.execute(
        "UPDATE clips SET state = ?, created_at = ?, vertical_path = ?, thumbnail_path = ? "
        "WHERE id = ?",
        (state, NOW - age_days * DAY, str(folder / f"{name}.vertical.mp4"),
         str(folder / f"{name}.jpg"), clip_id),
    )  # fmt: skip
    conn.commit()
    return clip_id


def files(config: Config, name: str) -> list[str]:
    return sorted(p.name for p in (config.outbox_dir / "alpha").glob(f"{name}.*"))


def test_retention_purges_by_state_and_age(setup: tuple[Config, sqlite3.Connection]) -> None:
    config, conn = setup
    approved = add_clip(config, conn, "approved", "approved", 300)
    rejected = add_clip(config, conn, "rejected", "rejected", 3)
    blocked = add_clip(config, conn, "blocked", "blocked", 3)
    fresh_rejected = add_clip(config, conn, "fresh", "rejected", 1)
    old_new = add_clip(config, conn, "oldnew", "new", 8)
    young_new = add_clip(config, conn, "young", "new", 6)

    dry = run_cleanup(config, conn, now=NOW, dry_run=True, disk_free=lambda: 500.0)
    assert sorted(dry.purged) == sorted([rejected, blocked, old_new])
    assert files(config, "rejected") == [
        "rejected.ass", "rejected.fp", "rejected.jpg", "rejected.json", "rejected.mp4",
        "rejected.vertical.mp4",
    ]  # fmt: skip

    report = run_cleanup(config, conn, now=NOW, disk_free=lambda: 500.0)
    assert sorted(report.purged) == sorted([rejected, blocked, old_new])
    assert report.freed_mb == pytest.approx(3 * 5 * 1000 / 1e6)
    for name in ("rejected", "blocked", "oldnew"):
        assert files(config, name) == [f"{name}.json"]  # the sidecar stays
    for name in ("approved", "fresh", "young"):
        assert len(files(config, name)) == 6
    for clip_id in (approved, fresh_rejected, young_new):
        assert get_clip(conn, clip_id)["purged_at"] is None  # type: ignore[index]
    assert get_clip(conn, rejected)["purged_at"] == NOW  # type: ignore[index]

    assert [c.id for c in list_clips(conn, "new")] == [young_new]  # purged clips leave "new"
    (old,) = [c for c in list_clips(conn) if c.id == old_new]
    assert old.purged
    assert not old.has_vertical
    assert run_cleanup(config, conn, now=NOW, disk_free=lambda: 500.0).purged == []  # idempotent


def test_posted_clips_are_purged_a_day_after_their_last_post(
    setup: tuple[Config, sqlite3.Connection],
) -> None:
    config, conn = setup

    def post(clip_id: int, platform: str, state: str, age_days: float) -> None:
        conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, "
            "attempts, created_at, updated_at) VALUES (?, 'en', ?, 'public', ?, '{}', 0, ?, ?)",
            (clip_id, platform, state, NOW, NOW - age_days * DAY),
        )
        conn.commit()

    done = add_clip(config, conn, "done", "approved", 3)
    post(done, "youtube", "published", 2)
    post(done, "facebook", "failed", 2)  # out of retries: final too
    waiting = add_clip(config, conn, "waiting", "approved", 3)
    post(waiting, "youtube", "published", 2)
    post(waiting, "instagram", "approved", 2)  # still to post: needs the file
    recent = add_clip(config, conn, "recent", "approved", 3)
    post(recent, "youtube", "published", 0.5)  # posted half a day ago
    failed = add_clip(config, conn, "failed", "approved", 3)
    post(failed, "youtube", "failed", 1)  # never on a platform: kept a while to post again
    expired = add_clip(config, conn, "expired", "approved", 4)
    post(expired, "instagram", "cancelled", 3)  # dropped as stale, past rejected_days
    post(expired, "youtube", "failed", 2.5)

    posts_before = conn.execute("SELECT count(*) FROM publications").fetchone()[0]
    report = run_cleanup(config, conn, now=NOW, disk_free=lambda: 500.0)
    assert report.purged == [expired, done]
    # Only video files go: every post record stays (the Uploads tab's history).
    assert conn.execute("SELECT count(*) FROM publications").fetchone()[0] == posts_before
    assert files(config, "done") == ["done.json"]  # media gone, the record stays
    for name in ("waiting", "recent", "failed"):
        assert len(files(config, name)) == 6


def test_retention_removes_old_packages_and_chat(setup: tuple[Config, sqlite3.Connection]) -> None:
    config, conn = setup
    old_package = config.publish_dir / "en" / "tiktok" / "2026-10-01" / "c1"
    new_package = config.publish_dir / "en" / "tiktok" / "2026-11-17" / "c2"
    for package in (old_package, new_package):
        package.mkdir(parents=True)
        (package / "video.mp4").write_bytes(b"v" * 2000)
    os.utime(old_package, (NOW - 20 * DAY, NOW - 20 * DAY))
    os.utime(new_package, (NOW - 1 * DAY, NOW - 1 * DAY))
    chat = config.chat_log_dir / "alpha"
    chat.mkdir(parents=True)
    (chat / "2026-07-01.jsonl").write_text("{}\n", encoding="utf-8")  # > 90 days before NOW
    (chat / "2026-11-15.jsonl").write_text("{}\n", encoding="utf-8")

    report = run_cleanup(config, conn, now=NOW, disk_free=lambda: 500.0)
    assert (report.packages, report.chat_files) == (1, 1)
    assert not old_package.exists()
    assert new_package.exists()
    assert sorted(p.name for p in chat.iterdir()) == ["2026-11-15.jsonl"]


def test_low_disk_purges_oldest_unapproved_first(write_config: WriteConfig) -> None:
    config = load_config(write_config({"retention": {"min_free_gb": 10}}))
    init_db(config.db_path)
    conn = connect(config.db_path)
    approved = add_clip(config, conn, "approved", "approved", 3)
    oldest = add_clip(config, conn, "oldest", "new", 3)
    older = add_clip(config, conn, "older", "new", 2)
    newest = add_clip(config, conn, "newest", "new", 1)
    in_flight = add_clip(config, conn, "inflight", "new", 0.01)  # 15 minutes old

    free = iter([9.999997, 10.5])  # needs 3000 bytes back, then reports room
    report = run_cleanup(config, conn, now=NOW, disk_free=lambda: next(free))
    assert report.emergency == [oldest]  # 5 kB freed covers the 3 kB needed
    assert not report.low_disk
    free = iter([0.0, 0.0])
    report = run_cleanup(config, conn, now=NOW, disk_free=lambda: next(free))
    assert report.emergency == [older, newest]  # never the approved or the in-flight clip
    assert report.low_disk
    for clip_id in (approved, in_flight):
        assert get_clip(conn, clip_id)["purged_at"] is None  # type: ignore[index]


def test_machine_sample_rates_and_a_missing_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_mod, "_last_received", None)
    monkeypatch.setattr(health_mod, "_last_cpu", None)

    def no_tool(_name: str) -> None:  # no NVIDIA GPU
        return None

    monkeypatch.setattr(health_mod.shutil, "which", no_tool)
    first = sample_machine()
    assert (first.download_mbps, first.cpu_pct) == (None, None)  # rates need a previous sample
    assert (first.gpu_pct, first.vram_used_gb) == (None, None)  # unknown, not 0
    assert first.ram_total_gb and 0 < (first.ram_used_gb or 0) <= first.ram_total_gb
    sum(i * i for i in range(200_000))  # some CPU time between the samples
    # From another thread, as the pipeline writes the status (psutil.cpu_percent read 0.0).
    with ThreadPoolExecutor(1) as pool:
        second = pool.submit(sample_machine).result()
    assert second.download_mbps is not None and second.download_mbps >= 0
    assert second.cpu_pct is not None and 0 < second.cpu_pct <= 100


def test_process_health_sample() -> None:
    health = sample()
    assert health.threads >= 1
    assert health.memory_mb is not None
    assert health.memory_mb > 1
    if sys.platform == "win32":
        assert health.handles is not None
        assert health.handles > 10


def test_status_reports_health_and_warnings(write_config: WriteConfig) -> None:
    config = load_config(
        write_config(
            {
                "channels": [{"name": "alpha"}],
                "retention": {"min_free_gb": 1_000_000},
                "health": {"max_memory_mb": 1, "max_handles": 1},
                "captions": {"enabled": False},
            }
        )
    )
    pipeline = LivePipeline(
        config, ["alpha"], Path("ffmpeg"), source=None,  # type: ignore[arg-type]
        notifier=Notifier(config.notify, senders=[]), helix=None,
    )  # fmt: skip
    pipeline.states["alpha"].next_restart_at = 1.0
    status: dict[str, Any] = pipeline._status(running=True)  # pyright: ignore[reportPrivateUsage]
    health = status["health"]
    assert health["memory_mb"] > 1
    assert health["free_disk_gb"] > 0
    warnings = " | ".join(status["warnings"])
    assert "disk low" in warnings
    assert "memory high" in warnings
    assert "alpha: recorder restarting" in warnings
    if sys.platform == "win32":
        assert "handles high" in warnings
    stopped: dict[str, Any] = pipeline._status(running=False)  # pyright: ignore[reportPrivateUsage]
    assert stopped["warnings"] == []


def test_uncaught_thread_exceptions_reach_the_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    log_file = tmp_path / "log.jsonl"
    configure_logging(file=log_file, console=False)

    def boom() -> None:
        raise RuntimeError("recorder exploded")

    thread = threading.Thread(target=boom, name="recorder-alpha")
    thread.start()
    thread.join()
    configure_logging()  # release the file
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    (crash,) = [r for r in records if r["event"] == "uncaught exception in thread"]
    assert crash["level"] == "critical"
    assert crash["thread"] == "recorder-alpha"
    assert "recorder exploded" in crash["exception"]


class FakeChild:
    def __init__(self, exit_code: int | None) -> None:
        self.pid = 4242
        self.exit_code = exit_code

    def wait(self, timeout: float) -> int:
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("peakreel", timeout)
        return self.exit_code


def test_detach_restarts_the_command_in_the_background(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config({})
    launched: list[dict[str, Any]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> FakeChild:
        launched.append({"args": args, **kwargs})
        return FakeChild(None)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    argv = ["peakreel", "-c", str(path), "review", "--port", "8799", "--detach"]
    monkeypatch.setattr(sys, "argv", argv)
    result = CliRunner().invoke(cli.app, argv[1:])
    assert result.exit_code == 0, result.output
    assert "started in the background (pid 4242)" in result.output
    (call,) = launched
    assert call["args"][1:] == ["-m", "peakreel", "-c", str(path), "review", "--port", "8799"]
    assert call["env"][cli._BACKGROUND_ENV] == "1"  # pyright: ignore[reportPrivateUsage]
    assert call["stdout"] is subprocess.DEVNULL

    def failing_popen(args: list[str], **kwargs: Any) -> FakeChild:
        return FakeChild(1)

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    result = CliRunner().invoke(cli.app, argv[1:])
    assert result.exit_code == 1
    assert "exited at start (code 1)" in result.output


def test_cleanup_command(setup: tuple[Config, sqlite3.Connection]) -> None:
    config, conn = setup
    add_clip(config, conn, "rejected", "rejected", time.time() / DAY)  # created at epoch 0
    conn.close()
    result = CliRunner().invoke(
        cli.app, ["-c", str(config.root / "config.yaml"), "cleanup", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would delete: 1 clip(s)" in result.output
    assert len(files(config, "rejected")) == 6


def test_clip_budget_per_channel(write_config: WriteConfig) -> None:
    from dataclasses import replace as dc_replace

    from peakreel.detect.spikes import Spike

    config = load_config(
        write_config(
            {
                "channels": [{"name": "alpha"}, {"name": "beta"}],
                "clip": {"max_per_hour": 2, "keep_score": 10},
                "captions": {"enabled": False},
            }
        )
    )
    pipeline = LivePipeline(
        config, ["alpha", "beta"], Path("ffmpeg"), source=None,  # type: ignore[arg-type]
        notifier=Notifier(config.notify, senders=[]), helix=None, kick=None,
    )  # fmt: skip
    base = Spike(
        channel="alpha", start_ts=NOW, end_ts=NOW + 10, peak_ts=NOW, peak_count=40, messages=60,
        unique_chatters=50, baseline_median=5.0, baseline_mad=1.0, score=5.0,
        top_messages=(), receive_delay_s=None,
    )  # fmt: skip

    def check(offset: float, score: float = 5.0, channel: str = "alpha") -> str | None:
        spike = dc_replace(base, start_ts=NOW + offset, score=score, channel=channel)
        return pipeline._within_budget(spike).suppressed_by  # pyright: ignore[reportPrivateUsage]

    assert check(0) is None
    assert check(60) is None
    assert check(120) == "budget"  # third weak spike within the hour
    assert check(180, score=12) is None  # a huge moment is kept anyway
    assert check(200, channel="beta") is None  # budgets are per channel
    assert check(3700) is None  # the first clip left the rolling hour
    assert pipeline.states["alpha"].budget_skipped == 1
    suppressed = dc_replace(base, suppressed_by="raid")
    assert pipeline._within_budget(suppressed).suppressed_by == "raid"  # pyright: ignore[reportPrivateUsage]


def test_unexcited_spikes_are_dropped_before_cutting(config: Config) -> None:
    from dataclasses import replace

    from peakreel.detect.spikes import Spike

    pipeline = LivePipeline(
        config, [], Path("ffmpeg"), notifier=Notifier(config.notify, senders=[]), helix=None
    )
    t0 = 1_795_000_000.0
    spike = Spike(
        channel="alpha", start_ts=t0, end_ts=t0 + 10, peak_ts=t0, peak_count=80, messages=80,
        unique_chatters=60, baseline_median=10, baseline_mad=2, score=12.0,
        top_messages=(("yes", 17), ("no", 9)), receive_delay_s=0.2,
    )  # fmt: skip
    assert pipeline._excited(spike).suppressed_by == "unexcited"  # pyright: ignore[reportPrivateUsage]
    excited = replace(spike, top_messages=(("KEKW", 20), ("LOOOL", 10)))
    assert pipeline._excited(excited).suppressed_by is None  # pyright: ignore[reportPrivateUsage]
    no_chat = replace(spike, top_messages=())
    assert pipeline._excited(no_chat).suppressed_by is None  # pyright: ignore[reportPrivateUsage]
