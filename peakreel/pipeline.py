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
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from peakreel.blocklist import load_blocklist
from peakreel.captions.whisper import Captioner
from peakreel.chat.base import ChatEvent, ChatMessage, ChatSource, Emit
from peakreel.chat.ingest import record_chat
from peakreel.chat.twitch import TwitchChat
from peakreel.chat.writer import ChatLogWriter
from peakreel.clip.clipper import Clipper, ClipResult
from peakreel.config import Config, ConfigError
from peakreel.db import init_db
from peakreel.detect.filters import ChatFilter
from peakreel.detect.spikes import Spike, SpikeDetector
from peakreel.log import get_logger
from peakreel.notify import Notification, Notifier
from peakreel.record.buffer import channel_segments, disk_usage, prune_channel
from peakreel.record.recorder import ChannelRecorder
from peakreel.render.vertical import RenderError, font_family, resolve_font

__all__ = ["ChannelState", "LivePipeline"]

log = get_logger(__name__)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_captioner(config: Config, ffmpeg: Path) -> Captioner | None:
    """Build the captioner from config, or ``None`` when captions are off or impossible."""
    if not config.captions.enabled:
        return None
    try:
        font = resolve_font(config.render.font_file)
        family = font_family(font, config.render.font_name)
    except RenderError as exc:
        log.warning("captions disabled: no usable font", error=str(exc))
        return None
    return Captioner(
        config.captions,
        config.output,
        config.resolve(config.tools.whisper_model_dir),
        ffmpeg,
        family,
    )


@dataclass
class ChannelState:
    """Supervision state of one channel.

    Attributes
    ----------
    name : str
    detector : SpikeDetector
    stop : threading.Event
        Stops this channel's recorder only.
    thread : threading.Thread or None
        The recorder thread.
    restarts : int
        Recorder restarts after a crash.
    detector_restarts : int
        Detector rebuilds after an exception.
    next_restart_at : float
        Monotonic time before which a crashed recorder is not restarted (backoff).
    blocked : bool
        Taken down while running: no recording, no chat, buffer deleted.
    chat_events : int
        Chat messages since the last status write.
    clips : int
        Clips produced.
    """

    name: str
    detector: SpikeDetector
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    restarts: int = 0
    detector_restarts: int = 0
    next_restart_at: float = 0.0
    blocked: bool = False
    chat_events: int = 0
    clips: int = 0


class _FilteredSource:
    """Drop chat events of channels that are no longer allowed (taken down)."""

    def __init__(self, source: ChatSource, allowed: Callable[[str], bool]) -> None:
        self._source = source
        self._allowed = allowed

    async def run(self, emit: Emit) -> None:
        def filtered(event: ChatEvent) -> None:
            if self._allowed(event.channel):
                emit(event)

        await self._source.run(filtered)


class LivePipeline:
    """Supervise chat ingest, detection, recording and clipping for many channels.

    Every channel is isolated from the others:

    - A detector that raises is logged and rebuilt; other channels never notice.
    - A recorder thread that dies is restarted by a watchdog with exponential backoff
      (``supervisor.restart_initial_s`` doubling up to ``restart_max_s``).
    - The takedown blocklist is re-read every ``supervisor.blocklist_check_s``. A
      channel that appears on it is stopped at once: its recorder is stopped, its
      chat is no longer logged, and its buffered video is deleted.
    - ``data/run/status.json`` is rewritten every ``supervisor.status_interval_s``
      with each channel's health, for the review tool and for monitoring.

    Parameters
    ----------
    config : Config
    channels : sequence of str
        Channel logins, already checked against the allowlist and blocklist.
    ffmpeg : pathlib.Path
    recorder_factory : callable, optional
        ``recorder_factory(channel) -> ChannelRecorder``; injectable for tests.
    clock : callable, optional
        Returns local epoch seconds.
    source : ChatSource, optional
        Chat source; defaults to an anonymous Twitch connection. Injectable for tests.
    clipper : Clipper, optional
        Defaults to a :class:`Clipper` for ``config``. Injectable for tests.
    captioner : Captioner, optional
        Defaults to one built from ``captions:`` when captions are enabled.
    notifier : Notifier, optional
        Defaults to one built from ``notify:``.

    Attributes
    ----------
    stop_requested : asyncio.Event
        Set it (or cancel :meth:`run`) to begin an orderly shutdown.
    results : list of ClipResult
        Outcomes of every handled spike, for reporting and tests.
    states : dict of str to ChannelState
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
        captioner: Captioner | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.channels = tuple(dict.fromkeys(channels))
        self.stop_requested = asyncio.Event()
        self.results: list[ClipResult] = []
        self._clock = clock
        self._params = config.detect.params()
        self.states = {
            name: ChannelState(name, SpikeDetector(name, self._params)) for name in self.channels
        }
        self._captioner = captioner if captioner is not None else _make_captioner(config, ffmpeg)
        self._clipper = clipper or Clipper(config, ffmpeg, clock=clock, captioner=self._captioner)
        self._notifier = notifier if notifier is not None else Notifier(config.notify)
        base_source: ChatSource = source or TwitchChat(self.channels, config.chat)
        self._source = _FilteredSource(base_source, self._chat_allowed)
        self._recorder_factory = recorder_factory or self._default_recorder
        self._clip_tasks: set[asyncio.Task[None]] = set()
        self._started = clock()
        self._stopping = False
        self._warm_up: asyncio.Task[None] | None = None

    def _default_recorder(self, name: str) -> ChannelRecorder:
        return ChannelRecorder(name, self.config.record, self.config.buffer_dir)

    def _chat_allowed(self, channel: str) -> bool:
        state = self.states.get(channel)
        return state is None or not state.blocked

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
            result = await self._clipper.handle(spike)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("clip handling crashed", channel=spike.channel)
            return
        self.results.append(result)
        if result.status == "clipped":
            state = self.states.get(spike.channel)
            if state is not None:
                state.clips += 1
            if self._notifier.enabled:
                top = spike.top_messages[0][0] if spike.top_messages else ""
                note = Notification(
                    title=f"New clip: {spike.channel}",
                    body=f"score {spike.score:g} · {top}"[:200],
                    url=f"http://{self.config.review.host}:{self.config.review.port}/",
                )
                await asyncio.to_thread(self._notifier.send, note)

    def _feed(self, state: ChannelState, event: ChatEvent | None, now: float | None = None) -> None:
        """Feed or advance one detector; an exception rebuilds that detector only."""
        try:
            spikes = (
                state.detector.feed(event)
                if event is not None
                else state.detector.advance(now or 0.0)
            )
        except Exception:
            state.detector_restarts += 1
            log.exception(
                "detector crashed; rebuilt", channel=state.name, restarts=state.detector_restarts
            )
            state.detector = SpikeDetector(state.name, self._params)
            return
        self._dispatch(spikes)

    def _on_chat_event(self, event: ChatEvent) -> None:
        state = self.states.get(event.channel)
        if state is None or state.blocked:
            return
        if isinstance(event, ChatMessage):
            state.chat_events += 1
        self._feed(state, event)

    async def _warm_up_captions(self) -> None:
        """Load the model now, so problems show at start, not on the first clip."""
        captioner = self._captioner
        if captioner is None:
            return
        try:
            await captioner.warm_up()
        except Exception as exc:
            log.warning("captions unavailable; clips will render without", error=str(exc))

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = self._clock()
            for state in self.states.values():
                if not state.blocked:
                    self._feed(state, None, now)

    # -- recorders -----------------------------------------------------------------

    def _recorder_main(self, name: str, stop: threading.Event) -> None:
        try:
            self._recorder_factory(name).run(stop)
        except Exception:
            log.exception("recorder crashed", channel=name)

    def _start_recorder(self, state: ChannelState) -> None:
        state.stop = threading.Event()
        state.thread = threading.Thread(
            target=self._recorder_main,
            args=(state.name, state.stop),
            name=f"record-{state.name}",
            daemon=True,
        )
        state.thread.start()

    async def _watchdog(self) -> None:
        settings = self.config.supervisor
        while True:
            await asyncio.sleep(2.0)
            for state in self.states.values():
                thread = state.thread
                if state.blocked or self._stopping or thread is None or thread.is_alive():
                    continue
                now = time.monotonic()
                if state.next_restart_at == 0.0:
                    delay = min(
                        settings.restart_max_s, settings.restart_initial_s * 2**state.restarts
                    )
                    state.next_restart_at = now + delay
                    log.error(
                        "recorder stopped unexpectedly", channel=state.name, restart_in_s=delay
                    )
                    continue
                if now >= state.next_restart_at:
                    state.restarts += 1
                    state.next_restart_at = 0.0
                    log.warning("recorder restarted", channel=state.name, restarts=state.restarts)
                    self._start_recorder(state)

    # -- takedowns -------------------------------------------------------------------

    async def _take_down(self, state: ChannelState) -> None:
        state.blocked = True
        state.stop.set()
        if state.thread is not None:
            await asyncio.to_thread(state.thread.join, 15)
        await asyncio.to_thread(shutil.rmtree, self.config.buffer_dir / state.name, True)
        log.warning("takedown: channel stopped and its buffered video deleted", channel=state.name)

    async def _watch_blocklist(self) -> None:
        while True:
            await asyncio.sleep(self.config.supervisor.blocklist_check_s)
            try:
                blocked = await asyncio.to_thread(load_blocklist, self.config.blocklist_path)
            except ConfigError as exc:
                # Clips fail closed in the clipper; recording keeps its current state
                # rather than stopping every channel over a typo in the file.
                log.error("blocklist unreadable; keeping current channel states", error=str(exc))
                continue
            for state in self.states.values():
                if state.name in blocked and not state.blocked:
                    await self._take_down(state)

    # -- status ------------------------------------------------------------------------

    def _status(self, *, running: bool) -> dict[str, object]:
        interval = self.config.supervisor.status_interval_s
        channels: dict[str, object] = {}
        for state in self.states.values():
            segments = channel_segments(self.config.buffer_dir / state.name)
            video_age = round(self._clock() - segments[-1].end, 1) if segments else None
            if state.blocked:
                status = "blocked"
            elif state.thread is not None and state.thread.is_alive():
                status = "running"
            else:
                status = "restarting"
            channels[state.name] = {
                "state": status,
                "video_age_s": video_age,
                "chat_per_min": round(state.chat_events * 60 / interval),
                "restarts": state.restarts,
                "detector_restarts": state.detector_restarts,
                "clips": state.clips,
            }
            state.chat_events = 0
        return {
            "running": running,
            "pid": os.getpid(),
            "started_utc": _iso(self._started),
            "updated_utc": _iso(self._clock()),
            "clips_in_flight": len(self._clip_tasks),
            "captions_backlog": self._captioner.backlog if self._captioner else 0,
            "channels": channels,
        }

    def _write_status(self, *, running: bool) -> None:
        path = self.config.run_dir / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._status(running=running), indent=1), encoding="utf-8")
        temp.replace(path)

    async def _report_status(self) -> None:
        while True:
            await asyncio.sleep(self.config.supervisor.status_interval_s)
            await asyncio.to_thread(self._write_status, running=True)

    async def _prune(self) -> None:
        keep_s = self.config.record.buffer_minutes * 60

        def prune_all() -> int:
            return sum(
                prune_channel(self.config.buffer_dir / s.name, keep_s)[1]
                for s in self.states.values()
                if not s.blocked
            )

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

    # -- lifecycle -----------------------------------------------------------------------

    async def run(self) -> None:
        """Run until :attr:`stop_requested` is set or the task is cancelled.

        Raises
        ------
        OSError
            If chat logs cannot be written (recording stops rather than lose data).
        """
        await asyncio.to_thread(init_db, self.config.db_path)
        for state in self.states.values():
            self._start_recorder(state)
        log.info("live pipeline started", channels=list(self.channels))
        await asyncio.to_thread(self._write_status, running=True)
        if self._captioner is not None:
            self._warm_up = asyncio.create_task(self._warm_up_captions(), name="captions-warm-up")

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
                asyncio.create_task(self._watchdog(), name="watchdog"),
                asyncio.create_task(self._watch_blocklist(), name="blocklist"),
                asyncio.create_task(self._report_status(), name="status"),
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
                await self._shutdown(chat, helpers)
            if interrupted:
                raise asyncio.CancelledError

    async def _shutdown(self, chat: asyncio.Task[None], helpers: list[asyncio.Task[None]]) -> None:
        self._stopping = True
        log.info("stopping: chat")
        chat.cancel()
        await asyncio.gather(chat, return_exceptions=True)
        for state in self.states.values():
            if not state.blocked:
                try:
                    self._dispatch(state.detector.flush())
                except Exception:
                    log.exception("detector flush failed", channel=state.name)
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
        if self._captioner is not None:
            await asyncio.to_thread(self._captioner.close)
        log.info("stopping: video")
        for state in self.states.values():
            state.stop.set()
        for state in self.states.values():
            if state.thread is not None:
                await asyncio.to_thread(state.thread.join, 15)
        await asyncio.to_thread(self._write_status, running=False)
        log.info(
            "live pipeline stopped", clips=sum(1 for r in self.results if r.status == "clipped")
        )
