from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from peakreel.chat.base import Emit
from peakreel.config import Config, DiscoverConfig, load_config
from peakreel.db import connect, ensure_channel, init_db, insert_clip
from peakreel.discover import plan_discovery
from peakreel.locks import ChannelLocks
from peakreel.notify import Notifier
from peakreel.pipeline import LivePipeline
from peakreel.publish.queue import queue_clip
from peakreel.record.recorder import ChannelRecorder
from peakreel.review.store import get_clip, view_of
from peakreel.twitch.helix import Credentials, HelixClient, StreamInfo
from tests.conftest import WriteConfig

T0 = 1_795_000_000.0


def s(login: str, viewers: int, language: str = "en") -> StreamInfo:
    return StreamInfo(login, "Grand Theft Auto VI", "", viewers, T0 - 3600, language)


# -- selection rules ----------------------------------------------------------------------


def plan(
    streams: Sequence[StreamInfo],
    *,
    manual: Sequence[str] = (),
    discovered: dict[str, float] | None = None,
    blocked: Sequence[str] = (),
    now: float = T0,
    **settings: object,
) -> tuple[list[str], dict[str, str], dict[str, float]]:
    config = DiscoverConfig.model_validate({"max_channels": 3, "min_viewers": 100, **settings})
    result = plan_discovery(
        streams, settings=config, languages={"en", "es", "pt", "ja"}, manual=manual,
        discovered=discovered or {}, blocked=blocked, now=now,
    )  # fmt: skip
    return [x.login for x in result.add], result.remove, result.seen


def test_picks_the_top_streams_in_account_languages() -> None:
    streams = [s("a", 9000), s("de", 8000, "de"), s("b", 7000, "es"), s("few", 50), s("c", 600),
               s("d", 500)]  # fmt: skip
    add, remove, _ = plan(streams)
    assert add == ["a", "b", "c"]  # German has no account; "few" is under min_viewers
    assert remove == {}


def test_manual_channels_use_slots_first_and_are_never_discovered() -> None:
    add, _, _ = plan([s("a", 9000), s("mine", 8000), s("b", 7000)], manual=["mine", "other"])
    assert add == ["a"]  # 3 slots, 2 manual


def test_blocklist_and_exclude_win() -> None:
    add, _, _ = plan([s("a", 9000), s("b", 8000), s("c", 7000)], blocked=["a"], exclude=["B"])
    assert add == ["c"]
    _, remove, _ = plan([s("a", 9000)], discovered={"a": T0}, blocked=["a"])
    assert remove == {"a": "blocklisted"}


def test_running_channels_are_kept_through_small_rank_changes() -> None:
    streams = [s("new1", 9000), s("new2", 8000), s("new3", 7000), s("old", 6000), s("x", 10)]
    # "old" is now 4th of 3 slots, but within the wider top (3 * 1.5 = 5): kept.
    add, remove, seen = plan(streams, discovered={"old": T0 - 60})
    assert remove == {}
    assert seen["old"] == T0
    assert add == ["new1", "new2"]  # only the free slots are filled


def test_channels_far_out_of_the_top_are_dropped_after_keep_min() -> None:
    streams = [s(f"t{i}", 10000 - i) for i in range(6)] + [s("old", 200)]
    _, remove, seen = plan(streams, discovered={"old": T0 - 5 * 60}, keep_min=20)
    assert remove == {}  # out of the top 5 for only 5 minutes
    assert seen["old"] == T0 - 5 * 60
    _, remove, _ = plan(streams, discovered={"old": T0 - 25 * 60}, keep_min=20)
    assert remove == {"old": "out of the top 5 for 20 min"}


def test_offline_channels_are_dropped_at_once() -> None:
    _, remove, _ = plan([s("a", 9000)], discovered={"gone": T0})
    assert remove == {"gone": "offline or left the category"}


# -- the running pipeline -----------------------------------------------------------------


class JoinableChat:
    """A chat source that records joins and parts."""

    def __init__(self) -> None:
        self.joined: list[str] = []
        self.parted: list[str] = []

    async def run(self, emit: Emit) -> None:
        await asyncio.Event().wait()

    async def join(self, channel: str) -> None:
        self.joined.append(channel)

    async def part(self, channel: str) -> None:
        self.parted.append(channel)


class IdleRecorder(ChannelRecorder):
    def run(self, stop: threading.Event) -> None:
        stop.wait()


class FakeHelix(HelixClient):
    def __init__(self, streams: list[StreamInfo]) -> None:
        super().__init__(Credentials("cid", "secret"))
        self.streams = streams

    def game_id(self, name: str) -> str | None:
        return "516575" if name == "Grand Theft Auto VI" else None

    def top_streams(
        self, game_id: str, languages: Sequence[str] | None = None, limit: int = 100
    ) -> list[StreamInfo]:
        return sorted(self.streams, key=lambda x: x.viewers, reverse=True)[:limit]

    def live_streams(self, logins: Sequence[str]) -> dict[str, StreamInfo]:
        return {x.login: x for x in self.streams if x.login in logins}


@pytest.fixture
def discovery_config(write_config: WriteConfig) -> Config:
    return load_config(
        write_config(
            {
                "channels": [{"name": "mine", "language": "en", "permission": "granted"}],
                "discover": {"enabled": True, "max_channels": 3, "min_viewers": 100},
                "publish": {
                    "accounts": [
                        {"id": "en", "languages": ["en"]},
                        {"id": "ja", "languages": ["ja"]},
                    ]
                },
                "captions": {"enabled": False},
            }
        )
    )


def pipeline_for(
    config: Config, helix: FakeHelix, chat: JoinableChat, locks: ChannelLocks
) -> LivePipeline:
    return LivePipeline(
        config, ["mine"], Path("ffmpeg"), source=chat,  # type: ignore[arg-type]
        recorder_factory=lambda name: IdleRecorder(name, config.record, config.buffer_dir),
        clock=lambda: T0, notifier=Notifier(config.notify, senders=[]), helix=helix, locks=locks,
    )  # fmt: skip


async def test_discovery_adds_and_removes_channels_while_running(discovery_config: Config) -> None:
    config = discovery_config
    helix = FakeHelix([s("a", 9000), s("de", 8000, "de"), s("j", 7000, "ja"), s("b", 600)])
    chat = JoinableChat()
    with ChannelLocks(config.run_dir / "locks", ["mine"]) as locks:
        pipeline = pipeline_for(config, helix, chat, locks)
        await pipeline._discover_once(helix, "516575")  # pyright: ignore[reportPrivateUsage]
        assert sorted(pipeline.states) == ["a", "j", "mine"]  # 3 slots: mine + top 2 eligible
        assert pipeline.states["a"].source == "discovered"
        assert pipeline.states["a"].stream == helix.streams[0]
        assert sorted(chat.joined) == ["a", "j"]
        assert sorted(locks.held) == ["a", "j", "mine"]
        context = pipeline.clip_context("a", T0, T0 + 30)
        assert context.discovered == {"permission": "credit_only"}
        assert pipeline.clip_context("mine", T0, T0 + 30).discovered is None
        status = pipeline._status(running=True)  # pyright: ignore[reportPrivateUsage]
        assert status["channels"]["a"]["source"] == "discovered"  # type: ignore[index]

        # "j" goes offline: it is stopped, parted and unlocked; "b" takes the slot.
        helix.streams = [s("a", 9000), s("b", 600)]
        await pipeline._discover_once(helix, "516575")  # pyright: ignore[reportPrivateUsage]
        assert sorted(pipeline.states) == ["a", "b", "mine"]
        assert chat.parted == ["j"]
        assert sorted(locks.held) == ["a", "b", "mine"]

        assert await pipeline.add_channel("mine") is False  # already running
        config.blocklist_path.write_text("channels: [c]\n", encoding="utf-8")
        assert await pipeline.add_channel("c") is False  # blocklisted
        for state in list(pipeline.states.values()):
            await pipeline.remove_channel(state.name, "test over")
        assert locks.held == ()


def test_discovered_permission_reaches_publishing(discovery_config: Config) -> None:
    config = discovery_config.model_copy(
        update={
            "publish": discovery_config.publish.model_copy(update={"modes": {"youtube": "public"}})
        }
    )
    init_db(config.db_path)
    conn = connect(config.db_path)
    folder = config.outbox_dir / "a"
    folder.mkdir(parents=True)
    (folder / "c.mp4").write_bytes(b"v")
    sidecar = {"discovered": {"permission": "credit_only"}, "stream": {"language": "en"}}
    (folder / "c.json").write_text(json.dumps(sidecar), encoding="utf-8")
    clip_id = insert_clip(
        conn, channel_id=ensure_channel(conn, "a"), spike_id=None, start_ts=T0, end_ts=T0 + 30,
        path=folder / "c.mp4", sidecar_path=folder / "c.json",
    )  # fmt: skip
    conn.commit()
    row = get_clip(conn, clip_id)
    assert row is not None
    notes = queue_clip(config, conn, view_of(row), row, ["youtube"], now=time.time())
    assert notes == []  # credit_only: not downgraded to private
    pub = conn.execute("SELECT mode, account FROM publications").fetchone()
    assert (pub["mode"], pub["account"]) == ("public", "en")  # routed by Twitch's language


_HOLD_LOCK = (
    "import sys, time; from pathlib import Path; from peakreel.locks import ChannelLocks\n"
    "with ChannelLocks(Path(sys.argv[1]), ['a']):\n"
    "    print('ready', flush=True); time.sleep(30)\n"
)


@pytest.fixture
def other_process_owns_a(discovery_config: Config) -> Iterator[None]:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(discovery_config.run_dir / "locks")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        yield
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.usefixtures("other_process_owns_a")
async def test_a_channel_owned_by_another_process_is_skipped(discovery_config: Config) -> None:
    config = discovery_config
    helix = FakeHelix([s("a", 9000), s("b", 600)])
    with ChannelLocks(config.run_dir / "locks", ["mine"]) as locks:
        pipeline = pipeline_for(config, helix, JoinableChat(), locks)
        await pipeline._discover_once(helix, "516575")  # pyright: ignore[reportPrivateUsage]
        assert sorted(pipeline.states) == ["b", "mine"]  # "a" belongs to another process
        for state in list(pipeline.states.values()):
            await pipeline.remove_channel(state.name, "test over")
