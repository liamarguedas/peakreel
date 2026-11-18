from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from peakreel.chat.base import ChatEvent, ChatMessage, ConnectionEvent, ConnectionEventName, Emit
from peakreel.chat.gaps import UNCLEAN_STOP, analyze_gaps
from peakreel.chat.ingest import record_chat
from peakreel.chat.replay import find_log_files, read_events, read_log
from peakreel.chat.writer import ChatLogWriter, log_path
from peakreel.cli import app
from peakreel.detect.filters import ChatFilter
from tests.conftest import WriteConfig

DAY = 1794873600.0  # 2026-11-17T00:00:00Z


def msg(ts: float, channel: str = "a", user: str = "u", text: str = "hi") -> ChatMessage:
    return ChatMessage(ts=ts, channel=channel, user=user, text=text)


def conn(
    ts: float, event: ConnectionEventName, channel: str = "a", last_alive: float | None = None
) -> ConnectionEvent:
    reason = None if event == "joined" else f"{event} reason"
    return ConnectionEvent(
        ts=ts, channel=channel, event=event, reason=reason, last_alive_ts=last_alive
    )


def write_all(root: Path, events: list[ChatEvent]) -> None:
    with ChatLogWriter(root) as writer:
        for event in events:
            writer.write(event)


def test_log_path_uses_utc_day(tmp_path: Path) -> None:
    assert log_path(tmp_path, "a", DAY - 0.001) == tmp_path / "a" / "2026-11-16.jsonl"
    assert log_path(tmp_path, "a", DAY) == tmp_path / "a" / "2026-11-17.jsonl"


def test_writer_rolls_over_at_utc_midnight_and_appends(tmp_path: Path) -> None:
    write_all(tmp_path, [msg(DAY - 1), msg(DAY + 1), msg(DAY + 2, channel="b")])
    write_all(tmp_path, [msg(DAY + 3)])
    assert len((tmp_path / "a" / "2026-11-16.jsonl").read_text("utf-8").splitlines()) == 1
    assert len((tmp_path / "a" / "2026-11-17.jsonl").read_text("utf-8").splitlines()) == 2
    assert (tmp_path / "b" / "2026-11-17.jsonl").is_file()
    raw = (tmp_path / "a" / "2026-11-17.jsonl").read_bytes()
    assert b"\r\n" not in raw


def test_writer_recovers_from_torn_line_and_replay_skips_it(tmp_path: Path) -> None:
    write_all(tmp_path, [msg(DAY + 1, text="before")])
    path = tmp_path / "a" / "2026-11-17.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind":"msg","ts":')  # simulated crash mid-write
    write_all(tmp_path, [msg(DAY + 2, text="after")])
    assert [m.text for m in read_log([tmp_path])] == ["before", "after"]


def test_replay_merges_channels_deterministically(tmp_path: Path) -> None:
    events: list[ChatEvent] = [
        conn(DAY, "joined", "b"),
        conn(DAY, "joined", "a"),
        msg(DAY + 1, "b", text="b1"),
        msg(DAY + 1, "a", text="a1"),
        msg(DAY + 0.5, "a", text="a0"),
        msg(DAY + 86400, "a", text="next day"),
    ]
    write_all(tmp_path / "chat", events)
    first = list(read_events([tmp_path / "chat"]))
    second = list(read_events([tmp_path / "chat" / "b", tmp_path / "chat" / "a"]))
    assert first == second
    assert [e.text for e in first if isinstance(e, ChatMessage)] == ["a1", "a0", "b1", "next day"]
    assert list(find_log_files([tmp_path / "chat"])) == ["a", "b"]
    with pytest.raises(FileNotFoundError):
        list(read_events([tmp_path / "missing"]))


def test_read_log_reclassify(tmp_path: Path) -> None:
    write_all(tmp_path, [msg(DAY, user="newbot"), msg(DAY + 1, text="!cmd")])
    assert [m.filtered for m in read_log([tmp_path])] == [None, None]
    chat_filter = ChatFilter(bots=frozenset({"newbot"}))
    assert [m.filtered for m in read_log([tmp_path], reclassify=chat_filter)] == ["bot", "command"]


def test_gaps_separates_quiet_chat_from_downtime(tmp_path: Path) -> None:
    write_all(
        tmp_path,
        [
            conn(DAY, "joined"),
            msg(DAY + 1),
            msg(DAY + 61),  # a quiet minute is not a failure
            conn(DAY + 70, "disconnected", last_alive=DAY + 65),
            conn(DAY + 72, "joined"),  # 7 s down: over the 5 s threshold
            msg(DAY + 73),
            conn(DAY + 80, "disconnected", last_alive=DAY + 79),
            conn(DAY + 81, "joined"),  # 2 s down: tolerated
            conn(DAY + 90, "stopped", last_alive=DAY + 90),
            conn(DAY + 500, "joined"),  # intentional restart
            msg(DAY + 501),
            # killed without a shutdown record, restarted later
            conn(DAY + 600, "joined"),
            conn(DAY + 700, "disconnected", last_alive=DAY + 695),  # log ends while down
        ],
    )
    (report,) = analyze_gaps([tmp_path])
    assert report.messages == 4
    assert report.sessions == 5
    assert report.max_message_gap_s == 60
    assert report.max_message_gap_at == DAY + 1
    durations = [(d.duration_s, d.intentional) for d in report.downtimes]
    assert durations == [(7, False), (2, False), (410, True), (99, False), (None, False)]
    assert report.downtimes[3].reason == UNCLEAN_STOP
    failures = report.failures(threshold_s=5)
    assert [d.duration_s for d in failures] == [7, 99, None]


async def test_record_chat_flags_and_writes_until_cancelled(tmp_path: Path) -> None:
    class FakeSource:
        async def run(self, emit: Emit) -> None:
            emit(conn(DAY, "joined"))
            emit(msg(DAY + 1, user="nightbot"))
            emit(msg(DAY + 2, text="!uptime"))
            emit(msg(DAY + 3, text="real"))
            try:
                await asyncio.Event().wait()
            finally:
                emit(conn(DAY + 4, "stopped", last_alive=DAY + 4))

    with ChatLogWriter(tmp_path) as writer:
        task = asyncio.create_task(
            record_chat(FakeSource(), writer, ChatFilter(bots=frozenset({"nightbot"})))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writer.written == 5
    events = list(read_events([tmp_path]))
    assert [m.filtered for m in events if isinstance(m, ChatMessage)] == ["bot", "command", None]
    assert isinstance(events[-1], ConnectionEvent)
    assert events[-1].event == "stopped"


def test_cli_replay_and_gaps(tmp_path: Path, write_config: WriteConfig) -> None:
    config = write_config({"chat": {"bots": ["nightbot"]}})
    logs = tmp_path / "logs"
    write_all(
        logs,
        [
            conn(DAY, "joined"),
            msg(DAY + 1.25, text="hello"),
            msg(DAY + 2, user="nightbot", text="spam"),
            conn(DAY + 3, "stopped", last_alive=DAY + 3),
        ],
    )
    runner = CliRunner()
    replay = runner.invoke(app, ["-c", str(config), "chat", "replay", str(logs)])
    assert replay.exit_code == 0, replay.output
    assert (
        replay.output
        == "2026-11-17T00:00:01.250Z #a <u> hello\n2026-11-17T00:00:02.000Z #a <nightbot> spam\n"
    )
    again = runner.invoke(app, ["-c", str(config), "chat", "replay", "--reclassify", str(logs)])
    assert again.output == "2026-11-17T00:00:01.250Z #a <u> hello\n"
    gaps = runner.invoke(app, ["-c", str(config), "chat", "gaps", str(logs)])
    assert gaps.exit_code == 0, gaps.output
    assert "[OK  ] #a" in gaps.output
    missing = runner.invoke(app, ["-c", str(config), "chat", "gaps", str(tmp_path / "nope")])
    assert missing.exit_code == 1


def test_cli_record_refuses_blocklisted_channel(write_config: WriteConfig) -> None:
    config = write_config()
    (config.parent / "blocklist.yaml").write_text("channels: [Alpha]\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["-c", str(config), "chat", "record", "-C", "alpha"])
    assert result.exit_code == 1
    assert "blocklisted" in result.output
    result = CliRunner().invoke(app, ["-c", str(config), "chat", "record"])
    assert result.exit_code == 1
    assert "no channels" in result.output


def test_cli_replay_to_redirected_stdout_is_utf8(tmp_path: Path, write_config: WriteConfig) -> None:
    """Regression: a redirected stdout on Windows defaulted to cp1252 and crashed."""
    config = write_config()
    logs = tmp_path / "logs"
    write_all(logs, [msg(DAY + 1, text="日本語 ünï 😀")])
    exe = Path(sys.executable).with_name("peakreel.exe" if os.name == "nt" else "peakreel")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    result = subprocess.run(
        [str(exe), "-c", str(config), "chat", "replay", str(logs)],
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode("utf-8").rstrip("\r\n").endswith("<u> 日本語 ünï 😀")


def test_chat_stop_file_stops_a_running_recorder(tmp_path: Path, write_config: WriteConfig) -> None:
    """A recorder started as `python -m peakreel` stops cleanly via `chat stop`."""
    import time

    config = write_config({"chat": {"url": "ws://127.0.0.1:9"}})  # never connects
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "peakreel", "-c", str(config), "chat", "record", "-C", "alpha"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    lines: list[bytes] = []
    stdout = proc.stdout
    assert stdout is not None
    reader = threading.Thread(target=lambda: lines.extend(iter(stdout.readline, b"")))
    reader.start()
    try:
        # Wait for the recorder to be running: it deletes a stale stop file on startup,
        # so a stop sent earlier (a slow start on a busy machine) would be lost.
        deadline = time.monotonic() + 60
        while not any(b"chat recording" in line for line in lines):
            assert proc.poll() is None and time.monotonic() < deadline, b"".join(lines)
            time.sleep(0.1)
        stop = CliRunner().invoke(app, ["-c", str(config), "chat", "stop"])
        assert stop.exit_code == 0, stop.output
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=5)
    output = b"".join(lines)
    assert proc.returncode == 0, output.decode("utf-8", "replace")
    assert b"stop requested" in output
    assert not (config.parent / "data" / "run" / "chat-record.stop").exists()
