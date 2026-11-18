"""TwitchChat against a local fake of the Twitch IRC websocket gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from websockets.asyncio.server import ServerConnection, serve

from peakreel.chat.base import ChatEvent, ChatMessage, ConnectionEvent
from peakreel.chat.twitch import TwitchChat
from peakreel.config import ChatConfig


@dataclass
class FakeTwitch:
    """Minimal Twitch IRC gateway: welcomes, confirms JOINs, relays scripted lines."""

    url: str = ""
    connections: list[ServerConnection] = field(default_factory=list[ServerConnection])
    received: list[str] = field(default_factory=list[str])
    joined: asyncio.Queue[ServerConnection] = field(default_factory=asyncio.Queue[ServerConnection])

    async def handler(self, ws: ServerConnection) -> None:
        self.connections.append(ws)
        nick = ""
        async for frame in ws:
            assert isinstance(frame, str)
            for line in frame.split("\r\n"):
                if not line:
                    continue
                self.received.append(line)
                if line.startswith("NICK "):
                    nick = line[5:]
                    await ws.send(f":tmi.twitch.tv 001 {nick} :Welcome, GLHF!\r\n")
                elif line.startswith("JOIN "):
                    for channel in line[5:].split(","):
                        await ws.send(f":{nick}!{nick}@{nick}.tmi.twitch.tv JOIN {channel}\r\n")
                    self.joined.put_nowait(ws)

    async def privmsg(self, ws: ServerConnection, channel: str, user: str, text: str) -> None:
        await ws.send(
            f"@tmi-sent-ts=1000 :{user}!{user}@{user}.tmi.twitch.tv PRIVMSG #{channel} :{text}\r\n"
        )


@pytest.fixture
async def fake() -> AsyncIterator[FakeTwitch]:
    server_state = FakeTwitch()
    async with serve(server_state.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        server_state.url = f"ws://127.0.0.1:{port}"
        yield server_state


def fast_settings(url: str) -> ChatConfig:
    return ChatConfig(
        url=url,
        keepalive_interval_s=0.1,
        keepalive_timeout_s=0.2,
        connect_timeout_s=1.0,
        reconnect_initial_delay_s=0.01,
        reconnect_max_delay_s=0.05,
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[ChatEvent] = []
        self._changed = asyncio.Event()

    def __call__(self, event: ChatEvent) -> None:
        self.events.append(event)
        self._changed.set()

    def conn(self, name: str) -> list[ConnectionEvent]:
        return [e for e in self.events if isinstance(e, ConnectionEvent) and e.event == name]

    def messages(self) -> list[ChatMessage]:
        return [e for e in self.events if isinstance(e, ChatMessage)]

    async def wait_for(self, predicate: Callable[[], bool], within: float = 5.0) -> None:
        async with asyncio.timeout(within):
            while not predicate():
                self._changed.clear()
                await self._changed.wait()


async def start(chat: TwitchChat, recorder: Recorder) -> asyncio.Task[None]:
    return asyncio.create_task(chat.run(recorder))


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_joins_receives_and_stops_cleanly(fake: FakeTwitch) -> None:
    recorder = Recorder()
    chat = TwitchChat(["#Alpha", "beta", "alpha"], fast_settings(fake.url))
    assert chat.channels == ("alpha", "beta")
    task = await start(chat, recorder)
    ws = await asyncio.wait_for(fake.joined.get(), 5)
    await recorder.wait_for(lambda: len(recorder.conn("joined")) == 2)
    assert chat.joined == {"alpha", "beta"}

    await fake.privmsg(ws, "alpha", "Viewer", "hello")
    await ws.send("PING :tmi.twitch.tv\r\n")
    await recorder.wait_for(lambda: len(recorder.messages()) == 1)
    (message,) = recorder.messages()
    assert (message.channel, message.user, message.text, message.server_ts) == (
        "alpha",
        "viewer",
        "hello",
        1.0,
    )

    await stop(task)
    stopped = recorder.conn("stopped")
    assert sorted(e.channel for e in stopped) == ["alpha", "beta"]
    assert all(e.last_alive_ts is not None for e in stopped)
    assert recorder.conn("disconnected") == []
    assert fake.received[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands"
    assert fake.received[1].startswith("NICK justinfan")
    assert "JOIN #alpha,#beta" in fake.received
    assert "PONG :tmi.twitch.tv" in fake.received


async def test_reconnects_after_connection_drop(fake: FakeTwitch) -> None:
    recorder = Recorder()
    task = await start(TwitchChat(["alpha"], fast_settings(fake.url)), recorder)
    first = await asyncio.wait_for(fake.joined.get(), 5)
    first.transport.abort()
    second = await asyncio.wait_for(fake.joined.get(), 5)
    assert second is not first
    await recorder.wait_for(lambda: len(recorder.conn("joined")) == 2)
    (disconnected,) = recorder.conn("disconnected")
    assert disconnected.reason is not None
    assert "connection lost" in disconnected.reason or "closed" in disconnected.reason
    await fake.privmsg(second, "alpha", "v", "back")
    await recorder.wait_for(lambda: len(recorder.messages()) == 1)
    await stop(task)


async def test_detects_stall_by_missing_pong(fake: FakeTwitch) -> None:
    recorder = Recorder()
    task = await start(TwitchChat(["alpha"], fast_settings(fake.url)), recorder)
    first = await asyncio.wait_for(fake.joined.get(), 5)
    await recorder.wait_for(lambda: len(recorder.conn("joined")) == 1)
    # Stop reading: the socket stays open but pings are never answered, exactly like
    # a network that silently dropped.
    first.transport.pause_reading()
    await recorder.wait_for(lambda: len(recorder.conn("disconnected")) == 1)
    (disconnected,) = recorder.conn("disconnected")
    assert disconnected.reason == "stalled: no pong within 0.2s"
    assert disconnected.last_alive_ts is not None
    # Detected within interval + timeout (plus scheduling slack) of the last proof of life.
    assert disconnected.ts - disconnected.last_alive_ts < 0.3 + 0.5
    await asyncio.wait_for(fake.joined.get(), 5)
    await recorder.wait_for(lambda: len(recorder.conn("joined")) == 2)
    first.transport.abort()
    await stop(task)


async def test_server_reconnect_command(fake: FakeTwitch) -> None:
    recorder = Recorder()
    task = await start(TwitchChat(["alpha"], fast_settings(fake.url)), recorder)
    first = await asyncio.wait_for(fake.joined.get(), 5)
    await recorder.wait_for(lambda: len(recorder.conn("joined")) == 1)
    await first.send(":tmi.twitch.tv RECONNECT\r\n")
    await asyncio.wait_for(fake.joined.get(), 5)
    await recorder.wait_for(lambda: len(recorder.conn("joined")) == 2)
    assert recorder.conn("disconnected")[0].reason == "server requested reconnect"
    await stop(task)


async def test_keeps_retrying_while_server_is_down() -> None:
    recorder = Recorder()
    chat = TwitchChat(["alpha"], fast_settings("ws://127.0.0.1:9"))  # nothing listens here
    task = await start(chat, recorder)
    await asyncio.sleep(0.5)
    assert not task.done()
    assert recorder.events == []  # nothing joined, so nothing to report as lost
    await stop(task)


def test_may_start_without_channels() -> None:
    # Category discovery can start with no channel configured by hand.
    assert TwitchChat(["#", " "], ChatConfig()).channels == ()


async def test_join_and_part_while_running(fake: FakeTwitch) -> None:
    recorder = Recorder()
    chat = TwitchChat(["alpha"], fast_settings(fake.url))
    task = await start(chat, recorder)
    await asyncio.wait_for(fake.joined.get(), 5)
    await recorder.wait_for(lambda: chat.joined == frozenset({"alpha"}))

    await chat.join("#Gamma")
    await chat.join("gamma")  # already there: nothing sent twice
    await recorder.wait_for(lambda: chat.joined == frozenset({"alpha", "gamma"}))
    assert chat.channels == ("alpha", "gamma")
    assert fake.received.count("JOIN #gamma") == 1

    await chat.part("alpha")
    async with asyncio.timeout(5):
        while "PART #alpha" not in fake.received:
            await asyncio.sleep(0.01)
    assert chat.channels == ("gamma",)
    assert chat.joined == frozenset({"gamma"})
    assert [e.channel for e in recorder.conn("stopped")] == ["alpha"]

    # After a reconnect only the current channels are joined again.
    await fake.connections[0].close()
    await recorder.wait_for(
        lambda: len(fake.connections) == 2 and chat.joined == frozenset({"gamma"})
    )
    rejoins = fake.received[fake.received.index("PART #alpha") + 1 :]
    assert "JOIN #gamma" in rejoins
    assert "JOIN #alpha" not in rejoins
    await stop(task)
