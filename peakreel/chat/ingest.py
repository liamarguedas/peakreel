"""Glue between a live chat source, the filters and the log writer."""

from __future__ import annotations

import asyncio
import dataclasses
import statistics
from collections import Counter

from peakreel.chat.base import ChatEvent, ChatMessage, ChatSource, Emit
from peakreel.chat.writer import ChatLogWriter
from peakreel.detect.filters import ChatFilter
from peakreel.log import get_logger

__all__ = ["record_chat"]

log = get_logger(__name__)


async def record_chat(
    source: ChatSource,
    writer: ChatLogWriter,
    chat_filter: ChatFilter,
    *,
    stats_interval_s: float = 60.0,
    on_event: Emit | None = None,
) -> None:
    """Record a live chat source to JSONL until cancelled.

    Every message is classified by ``chat_filter`` and written with the verdict in
    its ``filtered`` field. Connection events are written unchanged. A summary of
    message counts per channel is logged every ``stats_interval_s``, together with
    the median receive delay (``ts - server_ts``). A delay far from a fraction of a
    second means the local clock is skewed (see ``peakreel doctor``).

    Parameters
    ----------
    source : ChatSource
        Live source, for example :class:`peakreel.chat.twitch.TwitchChat`.
    writer : ChatLogWriter
        Destination. The caller owns it and closes it.
    chat_filter : ChatFilter
        Flags bots and commands. Flagged messages are still written.
    stats_interval_s : float, default 60.0
        Seconds between throughput log lines.
    on_event : callable, optional
        Called with every event after it is written, with its filter verdict
        applied. The live clipper feeds its spike detectors from here.

    Raises
    ------
    OSError
        If writing fails (for example, disk full). Recording stops rather than
        silently losing data.

    Notes
    -----
    Runs until cancelled. On cancellation the source emits its ``stopped`` events,
    which are written before this coroutine exits, so a clean shutdown is always
    visible in the log.
    """
    counts: Counter[str] = Counter()
    flagged: Counter[str] = Counter()
    delays: list[float] = []

    def emit(event: ChatEvent) -> None:
        if isinstance(event, ChatMessage):
            reason = chat_filter.classify(event)
            if reason is not None:
                event = dataclasses.replace(event, filtered=reason)
                flagged[event.channel] += 1
            counts[event.channel] += 1
            if event.server_ts is not None:
                delays.append(event.ts - event.server_ts)
        writer.write(event)
        if on_event is not None:
            on_event(event)

    async def report() -> None:
        while True:
            await asyncio.sleep(stats_interval_s)
            log.info(
                "chat throughput",
                interval_s=stats_interval_s,
                messages=dict(sorted(counts.items())),
                filtered=dict(sorted(flagged.items())),
                total_written=writer.written,
                median_receive_delay_s=round(statistics.median(delays), 3) if delays else None,
            )
            counts.clear()
            flagged.clear()
            delays.clear()

    reporter = asyncio.create_task(report(), name="chat-throughput")
    try:
        await source.run(emit)
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
