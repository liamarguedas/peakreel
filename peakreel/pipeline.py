"""The live pipeline: chat, detection, video and clipping for a set of channels.

One process, one event loop:

- **Chat**: a single :class:`~peakreel.chat.twitch.TwitchChat` connection records
  every channel to JSONL (exactly as ``peakreel chat record`` does) and feeds each
  event to that channel's :class:`~peakreel.detect.spikes.SpikeDetector`.
- **Ticker**: once a second every detector is advanced to the current time, so
  events finish even when chat goes quiet.
- **Video**: one :class:`~peakreel.record.recorder.ChannelRecorder` thread per
  channel fills the rolling buffer; the buffer is pruned every
  ``record.prune_interval_s``.
- **Clips**: each finished spike becomes a :class:`~peakreel.clip.clipper.Clipper`
  task that waits for the video, cuts and publishes the clip.

Shutdown order matters: chat stops first (its ``stopped`` records are written),
detectors are flushed, in-flight clips are allowed to finish while video is still
being recorded, and only then are the recorders stopped.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from peakreel.chat.base import ChatEvent, ChatSource
from peakreel.chat.ingest import record_chat
from peakreel.chat.twitch import TwitchChat
from peakreel.chat.writer import ChatLogWriter
from peakreel.clip.clipper import Clipper, ClipResult
from peakreel.config import Config
from peakreel.db import init_db
from peakreel.detect.filters import ChatFilter
from peakreel.detect.spikes import Spike, SpikeDetector
from peakreel.log import get_logger
from peakreel.record.buffer import disk_usage, prune_channel
from peakreel.record.recorder import ChannelRecorder

__all__ = ["LivePipeline"]

log = get_logger(__name__)


class LivePipeline:
    """Run chat ingest, detection, recording and clipping until stopped.

    Parameters
    ----------
    config : Config
    channels : sequence of str
        Channel logins, already checked against the blocklist by the caller.
    ffmpeg : pathlib.Path
    recorder_factory : callable, optional
        ``recorder_factory(channel) -> ChannelRecorder``; injectable for tests.
    clock : callable, optional
        Returns local epoch seconds.
    source : ChatSource, optional
        Chat source; defaults to an anonymous Twitch connection. Injectable for tests.
    clipper : Clipper, optional
        Defaults to a :class:`Clipper` for ``config``. Injectable for tests.

    Attributes
    ----------
    stop_requested : asyncio.Event
        Set it (or cancel :meth:`run`) to begin an orderly shutdown.
    results : list of ClipResult
        Outcomes of every handled spike, for reporting and tests.
    """

    def __init__(
        self,
        config: Config,
        channels: Sequence[str],
        ffmpeg: Path,
        *,
        recorder_factory: Callable[[str], ChannelRecorder] | None = None,
        clock: Callable[[], float] = time.time,
        source: ChatSource | None = None,
        clipper: Clipper | None = None,
    ) -> None:
        self.config = config
        self.channels = tuple(dict.fromkeys(channels))
        self.stop_requested = asyncio.Event()
        self.results: list[ClipResult] = []
        self._clock = clock
        params = config.detect.params()
        self._detectors = {name: SpikeDetector(name, params) for name in self.channels}
        self._clipper = clipper or Clipper(config, ffmpeg, clock=clock)
        self._source: ChatSource = source or TwitchChat(self.channels, config.chat)
        self._recorder_factory = recorder_factory or self._default_recorder
        self._clip_tasks: set[asyncio.Task[None]] = set()

    def _default_recorder(self, name: str) -> ChannelRecorder:
        return ChannelRecorder(name, self.config.record, self.config.buffer_dir)

    # -- spikes -> clips ---------------------------------------------------------

    def _dispatch(self, spikes: list[Spike]) -> None:
        for spike in spikes:
            log.info(
                "spike",
                channel=spike.channel,
                score=spike.score,
                peak=spike.peak_count,
                suppressed_by=spike.suppressed_by,
                top=[t for t, _ in spike.top_messages[:3]],
            )
            task = asyncio.get_running_loop().create_task(self._handle(spike))
            self._clip_tasks.add(task)
            task.add_done_callback(self._clip_tasks.discard)

    async def _handle(self, spike: Spike) -> None:
        try:
            self.results.append(await self._clipper.handle(spike))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("clip handling crashed", channel=spike.channel)

    def _on_chat_event(self, event: ChatEvent) -> None:
        detector = self._detectors.get(event.channel)
        if detector is not None:
            self._dispatch(detector.feed(event))

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = self._clock()
            for detector in self._detectors.values():
                self._dispatch(detector.advance(now))

    async def _prune(self) -> None:
        keep_s = self.config.record.buffer_minutes * 60

        def prune_all() -> int:
            return sum(prune_channel(self.config.buffer_dir / c, keep_s)[1] for c in self.channels)

        while True:
            freed = await asyncio.to_thread(prune_all)
            usage = await asyncio.to_thread(disk_usage, self.config.buffer_dir)
            log.info(
                "buffer pruned",
                freed_mb=round(freed / 1e6, 1),
                usage_mb={k: round(v / 1e6, 1) for k, v in usage.items()},
                clips_in_flight=len(self._clip_tasks),
            )
            await asyncio.sleep(self.config.record.prune_interval_s)

    # -- lifecycle ---------------------------------------------------------------

    async def run(self) -> None:
        """Run until :attr:`stop_requested` is set or the task is cancelled.

        Raises
        ------
        OSError
            If chat logs cannot be written (recording stops rather than lose data).
        """
        await asyncio.to_thread(init_db, self.config.db_path)
        stop_video = threading.Event()
        threads = [
            threading.Thread(
                target=self._recorder_factory(name).run,
                args=(stop_video,),
                name=f"record-{name}",
                daemon=True,
            )
            for name in self.channels
        ]
        for thread in threads:
            thread.start()
        log.info("live pipeline started", channels=list(self.channels))

        with ChatLogWriter(self.config.chat_log_dir) as writer:
            chat = asyncio.create_task(
                record_chat(
                    self._source,
                    writer,
                    ChatFilter.from_config(self.config.chat),
                    on_event=self._on_chat_event,
                ),
                name="chat",
            )
            helpers = [
                asyncio.create_task(self._tick(), name="ticker"),
                asyncio.create_task(self._prune(), name="pruner"),
            ]
            interrupted = False
            try:
                waiter = asyncio.create_task(self.stop_requested.wait())
                done, _ = await asyncio.wait({chat, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if chat in done:
                    chat.result()  # chat only ends by error: surface it
                waiter.cancel()
            except asyncio.CancelledError:
                interrupted = True
            finally:
                await self._shutdown(chat, helpers, threads, stop_video)
            if interrupted:
                raise asyncio.CancelledError

    async def _shutdown(
        self,
        chat: asyncio.Task[None],
        helpers: list[asyncio.Task[None]],
        threads: list[threading.Thread],
        stop_video: threading.Event,
    ) -> None:
        log.info("stopping: chat")
        chat.cancel()
        await asyncio.gather(chat, return_exceptions=True)
        for detector in self._detectors.values():
            self._dispatch(detector.flush())
        if self._clip_tasks:
            wait_s = self.config.clip.max_wait_s + 60
            log.info(
                "stopping: finishing clips in flight",
                count=len(self._clip_tasks),
                max_wait_s=wait_s,
            )
            _, pending = await asyncio.wait(set(self._clip_tasks), timeout=wait_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in helpers:
            task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        log.info("stopping: video")
        stop_video.set()
        for thread in threads:
            await asyncio.to_thread(thread.join, 15)
        log.info(
            "live pipeline stopped",
            clips=sum(1 for r in self.results if r.status == "clipped"),
        )
