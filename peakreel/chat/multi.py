"""One chat source over several platforms (Twitch, Kick)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from peakreel.chat.base import ChatSource, Emit
from peakreel.platforms import StreamPlatform, split_key

__all__ = ["MultiChat"]


class MultiChat:
    """Run one chat source per platform; join and part go to the channel's platform.

    Parameters
    ----------
    sources : mapping of platform to ChatSource
        Each must accept channel keys of its platform (``kick-<slug>`` for Kick).
    """

    def __init__(self, sources: Mapping[StreamPlatform, ChatSource]) -> None:
        self._sources = dict(sources)

    @property
    def channels(self) -> tuple[str, ...]:
        """Every channel of every platform."""
        names: list[str] = []
        for source in self._sources.values():
            names += getattr(source, "channels", ())
        return tuple(names)

    async def run(self, emit: Emit) -> None:
        """Run every source until cancelled; a failing source stops all of them."""
        async with asyncio.TaskGroup() as group:
            for platform, source in self._sources.items():
                group.create_task(source.run(emit), name=f"chat-{platform}")

    async def join(self, channel: str) -> None:
        """Join ``channel`` on its platform."""
        source = self._sources.get(split_key(channel)[0])
        join = getattr(source, "join", None)
        if join is not None:
            await join(channel)

    async def part(self, channel: str) -> None:
        """Part ``channel`` on its platform."""
        source = self._sources.get(split_key(channel)[0])
        part = getattr(source, "part", None)
        if part is not None:
            await part(channel)
