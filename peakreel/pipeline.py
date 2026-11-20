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
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from peakreel.blocklist import load_blocklist
from peakreel.captions.whisper import Captioner
from peakreel.chat.base import ChatEvent, ChatMessage, ChatSource, Emit
from peakreel.chat.ingest import record_chat
from peakreel.chat.multi import MultiChat
from peakreel.chat.twitch import TwitchChat
from peakreel.chat.writer import ChatLogWriter
from peakreel.cleanup import free_gb, run_cleanup
from peakreel.clip.clipper import ClipContext, Clipper, ClipResult
from peakreel.config import Config, ConfigError
from peakreel.db import connect, init_db
from peakreel.detect.filters import ChatFilter, excitement
from peakreel.detect.spikes import Spike, SpikeDetector
from peakreel.discover import plan_discovery
from peakreel.health import ProcessHealth, sample_machine
from peakreel.health import sample as sample_health
from peakreel.kick.api import KickApi, KickError
from peakreel.kick.api import load_credentials as load_kick_credentials
from peakreel.kick.chat import KickChat
from peakreel.locks import ChannelLocks, LockHeld
from peakreel.log import get_logger
from peakreel.notify import Notification, Notifier
from peakreel.platforms import split_key
from peakreel.publish.queue import full_languages
from peakreel.record.buffer import channel_segments, disk_usage, prune_channel
from peakreel.record.recorder import ChannelRecorder
from peakreel.render.vertical import RenderError, font_family, resolve_font
from peakreel.review.store import block_channels
from peakreel.safety.checks import ALERT_NOTICES
from peakreel.twitch.helix import HelixClient, HelixError, StreamInfo, load_credentials

__all__ = ["ChannelState", "LivePipeline"]

log = get_logger(__name__)
_DISCOVERY_DEPTH = 500  # streams fetched per platform: deep enough for every language's top


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
    alerts : collections.deque of ChatMessage
        Recent chat notices and cheers (which come with on-screen alerts), for the
        clips' alerts check.
    stream : StreamInfo or None
        Live stream details from Twitch; ``None`` when offline or not polled.
    source : {"manual", "discovered"}
        Configured by hand, or added by category discovery.
    discovered_seen : float
        Discovery: when the channel was last within the wider top of its category.
    clip_times : collections.deque of float
        Start times of this channel's clips in the last hour (clip budget).
    budget_skipped : int
        Spikes not clipped because the channel was over its clip budget.
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
    alerts: deque[ChatMessage] = field(default_factory=lambda: deque[ChatMessage](maxlen=500))
    stream: StreamInfo | None = None
    source: str = "manual"
    discovered_seen: float = 0.0
    clip_times: deque[float] = field(default_factory=deque[float])
    budget_skipped: int = 0


def _is_alert(message: ChatMessage) -> bool:
    if message.type == "usernotice":
        return message.tags.get("msg-id", "") in ALERT_NOTICES
    return bool(message.tags.get("bits"))


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
    kick : KickApi, "auto" or None, default "auto"
        Kick's API, for Kick channels' chat, live status and discovery; ``"auto"``
        uses the app stored by ``peakreel kick setup``.
    locks : ChannelLocks, optional
        The per-channel ownership locks held for the run; channels added by
        discovery take their lock here, and removed ones release it.
    helix : HelixClient, "auto" or None, default "auto"
        Polls live state, game and title. ``"auto"`` uses the credentials stored by
        ``peakreel twitch login`` when there are any; ``None`` disables polling.

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
        helix: HelixClient | Literal["auto"] | None = "auto",
        locks: ChannelLocks | None = None,
        kick: KickApi | Literal["auto"] | None = "auto",
    ) -> None:
        self.config = config
        self._locks = locks
        self.channels = tuple(dict.fromkeys(channels))
        self.stop_requested = asyncio.Event()
        self.results: list[ClipResult] = []
        self._clock = clock
        self._params = config.detect.params()
        self.states = {
            name: ChannelState(name, SpikeDetector(name, self._params)) for name in self.channels
        }
        self._captioner = captioner if captioner is not None else _make_captioner(config, ffmpeg)
        self._clipper = clipper or Clipper(
            config, ffmpeg, clock=clock, captioner=self._captioner, context=self.clip_context
        )
        self._notifier = notifier if notifier is not None else Notifier(config.notify)
        if kick == "auto":
            kick_credentials = load_kick_credentials()
            kick = KickApi(kick_credentials) if kick_credentials is not None else None
        self._kick: KickApi | None = kick
        self._category_ids: dict[str, str | int] = {}
        self._full: set[str] = set()  # languages at their Instagram cap (discovery skips them)
        base_source: ChatSource = source or self._default_source()
        self._base_source = base_source
        self._source = _FilteredSource(base_source, self._chat_allowed)
        self._recorder_factory = recorder_factory or self._default_recorder
        self._clip_tasks: set[asyncio.Task[None]] = set()
        self._started = clock()
        self._status_since = self._started
        # Writes run in pool threads (startup, every interval, shutdown) and share one
        # temp file: one at a time, or two can interleave into it.
        self._status_lock = threading.Lock()
        if helix == "auto":
            credentials = load_credentials()
            helix = HelixClient(credentials) if credentials is not None else None
        self._helix: HelixClient | None = helix
        self._stopping = False
        self._warm_up: asyncio.Task[None] | None = None

    def _default_source(self) -> ChatSource:
        """Twitch chat, plus Kick chat (webhooks) when Kick is in use and set up."""
        twitch = TwitchChat(
            [c for c in self.channels if split_key(c)[0] == "twitch"], self.config.chat
        )
        kick_channels = [c for c in self.channels if split_key(c)[0] == "kick"]
        wants_kick = bool(kick_channels) or (
            self.config.discover.enabled and "kick" in self.config.discover.platforms
        )
        if not wants_kick:
            return twitch
        if self._kick is None:
            log.error("Kick channels need `peakreel kick setup`; Kick chat is off",
                      channels=kick_channels)  # fmt: skip
            return twitch
        kick = KickChat(kick_channels, self._kick, self.config.kick, clock=self._clock)
        return MultiChat({"twitch": twitch, "kick": kick})

    def _default_recorder(self, name: str) -> ChannelRecorder:
        return ChannelRecorder(name, self.config.record, self.config.buffer_dir)

    def _chat_allowed(self, channel: str) -> bool:
        state = self.states.get(channel)
        return state is None or not state.blocked

    # -- spikes -> clips ---------------------------------------------------------

    def _excited(self, spike: Spike) -> Spike:
        """Return ``spike``, marked ``suppressed_by="unexcited"`` if its chat did not react.

        The same rule auto review rejects by (``review.auto.reject_below_excitement``),
        applied before cutting: an unexcited moment never costs a cut, Whisper, a
        render or disk. Off with ``review.auto.mode: off``; a spike without chat
        messages is kept (no evidence).
        """
        settings = self.config.review.auto
        if spike.suppressed_by is not None or settings.mode == "off" or not spike.top_messages:
            return spike
        if (
            excitement(spike.top_messages, settings.excitement_words)
            < settings.reject_below_excitement
        ):
            return replace(spike, suppressed_by="unexcited")
        return spike

    def _within_budget(self, spike: Spike) -> Spike:
        """Return ``spike``, marked ``suppressed_by="budget"`` if its channel is over budget."""
        settings = self.config.clip
        state = self.states.get(spike.channel)
        if spike.suppressed_by is not None or settings.max_per_hour is None or state is None:
            return spike
        recent = state.clip_times
        while recent and spike.start_ts - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= settings.max_per_hour and spike.score < settings.keep_score:
            state.budget_skipped += 1
            return replace(spike, suppressed_by="budget")
        recent.append(spike.start_ts)
        return spike

    def _dispatch(self, spikes: list[Spike]) -> None:
        for raw in spikes:
            spike = self._within_budget(self._excited(raw))
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
            if self._notifier.enabled and result.duplicate_of is None:
                top = spike.top_messages[0][0] if spike.top_messages else ""
                flags = f" · {len(result.flags)} flag(s)" if result.flags else ""
                note = Notification(
                    title=f"New clip: {spike.channel}",
                    body=f"score {spike.score:g}{flags} · {top}"[:200],
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
            if _is_alert(event):
                state.alerts.append(event)
        self._feed(state, event)

    def clip_context(self, channel: str, start: float, end: float) -> ClipContext:
        """Return what is known about ``channel`` between ``start`` and ``end`` (local clock).

        Parameters
        ----------
        channel : str
        start, end : float
            Chat-clock range of the clip.

        Returns
        -------
        ClipContext
            Alert-bearing chat in the range, and the current stream details.
        """
        state = self.states.get(channel)
        if state is None:
            return ClipContext()
        chat = tuple(m for m in state.alerts if start <= m.ts <= end)
        stream = state.stream.to_json() if state.stream is not None else None
        discovered = (
            {"permission": self.config.discover.permission}
            if state.source == "discovered"
            else None
        )
        return ClipContext(chat=chat, stream=stream, discovered=discovered)

    # -- channels at runtime ---------------------------------------------------------------

    async def add_channel(self, name: str, *, source: str = "discovered") -> bool:
        """Start recording ``name`` while running: lock, recorder, chat.

        Returns ``False`` (and changes nothing) if it is already running, on the
        blocklist, or recorded by another process.
        """
        if name in self.states:
            return False
        try:
            if name in await asyncio.to_thread(load_blocklist, self.config.blocklist_path):
                return False
        except ConfigError:
            return False  # fail closed: never add a channel while takedowns are unreadable
        if self._locks is not None:
            try:
                self._locks.add(name)
            except LockHeld as exc:
                log.warning("channel not added", channel=name, error=str(exc))
                return False
        state = ChannelState(name, SpikeDetector(name, self._params), source=source)
        self.states[name] = state
        self._start_recorder(state)
        join = getattr(self._base_source, "join", None)
        if join is not None:
            await join(name)
        log.info("channel added", channel=name, source=source)
        return True

    async def remove_channel(self, name: str, reason: str) -> None:
        """Stop recording ``name`` while running; its buffer ages out normally."""
        state = self.states.get(name)
        if state is None:
            return
        state.stop.set()
        if state.thread is not None:
            await asyncio.to_thread(state.thread.join, 15)
        part = getattr(self._base_source, "part", None)
        if part is not None:
            await part(name)
        self.states.pop(name, None)
        if self._locks is not None:
            self._locks.release([name])
        log.info("channel removed", channel=name, reason=reason)

    def _full_languages(self) -> set[str]:
        conn = connect(self.config.db_path)
        try:
            return full_languages(self.config, conn, now=self._clock())
        finally:
            conn.close()

    def _discovery_languages(self, streams: Sequence[StreamInfo]) -> set[str]:
        configured = self.config.discover.languages
        if configured is not None:
            return set(configured)
        accounts = self.config.publish.accounts
        if any(not a.languages for a in accounts):  # a catch-all account takes any language
            return {s.language for s in streams}
        return {code for a in accounts for code in a.languages}

    async def _discovery_streams(self) -> list[StreamInfo]:
        """Live streams of the discovery category on every enabled platform."""
        settings = self.config.discover
        streams: list[StreamInfo] = []
        if "twitch" in settings.platforms and self._helix is not None:
            helix = self._helix
            if "twitch" not in self._category_ids:
                game = await asyncio.to_thread(helix.game_id, settings.category)
                if game is None:
                    log.error("discovery: no Twitch category with that name",
                              category=settings.category)  # fmt: skip
                else:
                    self._category_ids["twitch"] = game
            game_id = self._category_ids.get("twitch")
            if game_id is not None:
                streams += await asyncio.to_thread(
                    helix.top_streams, str(game_id), settings.languages, _DISCOVERY_DEPTH
                )
        if "kick" in settings.platforms and self._kick is not None:
            kick = self._kick
            if "kick" not in self._category_ids:
                category = await asyncio.to_thread(kick.category_id, settings.kick_category)
                if category is None:
                    log.error("discovery: no Kick category with that name",
                              category=settings.kick_category)  # fmt: skip
                else:
                    self._category_ids["kick"] = category
            category_id = self._category_ids.get("kick")
            if category_id is not None:
                streams += await asyncio.to_thread(
                    kick.top_streams, int(category_id), settings.languages, _DISCOVERY_DEPTH
                )
        return streams

    async def _discover(self) -> None:
        """Keep the top live streamers of the discovery category recorded."""
        while True:
            try:
                await self._discover_once(await self._discovery_streams())
            except (HelixError, KickError) as exc:
                log.warning("discovery unavailable", error=str(exc))
            except Exception:
                log.exception("discovery pass failed")
            await asyncio.sleep(self.config.discover.refresh_min * 60)

    async def _discover_once(self, streams: Sequence[StreamInfo]) -> None:
        """Apply one discovery decision for ``streams`` (any platforms)."""
        settings = self.config.discover
        try:
            blocked = await asyncio.to_thread(load_blocklist, self.config.blocklist_path)
        except ConfigError:
            log.error("discovery paused: blocklist unreadable")
            return
        manual = [s.name for s in self.states.values() if s.source == "manual"]
        discovered = {
            s.name: s.discovered_seen for s in self.states.values() if s.source == "discovered"
        }
        languages = self._discovery_languages(streams)
        # A language whose Instagram accounts are all at their cap gets no streamers: its
        # clips would only wait and be dropped. Its slots go to the others, and come back
        # when it has room: the others' running channels over their share then stop.
        full = await asyncio.to_thread(self._full_languages)
        if full != self._full:
            log.info("discovery: languages at their Instagram cap", full=sorted(full))
            self._full = full
        if full & languages:
            languages = languages - full
            # The full language's slots are shared out among the others.
            share = -(-settings.max_channels // max(1, len(languages)))
            settings = settings.model_copy(update={"max_per_group": share})
        plan = plan_discovery(
            streams, settings=settings, languages=languages,
            manual=manual, discovered=discovered, blocked=blocked, now=self._clock(),
        )  # fmt: skip
        language_of = {s.login: s.language for s in streams}
        for name in discovered:
            if language_of.get(name) in full and name not in plan.remove:
                plan.remove[name] = f"{language_of[name]} Instagram accounts at their daily cap"
                plan.seen.pop(name, None)
        for name, reason in plan.remove.items():
            await self.remove_channel(name, reason)
        for stream in plan.add:
            if await self.add_channel(stream.login):
                self.states[stream.login].stream = stream
        for name, seen in plan.seen.items():
            if name in self.states:
                self.states[name].discovered_seen = seen
        if plan.add or plan.remove:
            log.info(
                "discovery",
                category=settings.category,
                added=[s.login for s in plan.add],
                removed=plan.remove,
                running=sorted(self.states),
            )

    def _live_streams(self) -> dict[str, StreamInfo]:
        """Live state of every channel, from each platform's API (blocking)."""
        keys = list(self.states)
        live: dict[str, StreamInfo] = {}
        twitch = [k for k in keys if split_key(k)[0] == "twitch"]
        kick = [split_key(k)[1] for k in keys if split_key(k)[0] == "kick"]
        if twitch and self._helix is not None:
            live.update(self._helix.live_streams(twitch))
        if kick and self._kick is not None:
            for channel in self._kick.channels(kick).values():
                info = channel.stream()
                if info is not None:
                    live[info.login] = info
        return live

    async def _poll_streams(self) -> None:
        """Track live state, game and title of every channel through the platforms' APIs."""
        while True:
            try:
                live = await asyncio.to_thread(self._live_streams)
            except (HelixError, KickError) as exc:
                log.warning("stream status unavailable", error=str(exc))
            except Exception:
                # Never let polling die silently: an odd API answer must not stop it.
                log.exception("stream status poll failed")
            else:
                for state in list(self.states.values()):
                    info = live.get(state.name)
                    if (info is None) != (state.stream is None):
                        if info is None:
                            log.info("channel went offline", channel=state.name)
                        else:
                            log.info(
                                "channel is live",
                                channel=state.name,
                                game=info.game,
                                title=info.title,
                                viewers=info.viewers,
                            )
                    state.stream = info
            await asyncio.sleep(self.config.golive.poll_s)

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
        await asyncio.to_thread(self._block_clips, state.name)

    def _block_clips(self, channel: str) -> None:
        conn = connect(self.config.db_path)
        try:
            block_channels(self.config, conn, [channel])
        finally:
            conn.close()

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
            for state in list(self.states.values()):
                if state.name in blocked and not state.blocked:
                    await self._take_down(state)

    # -- status ------------------------------------------------------------------------

    def _warnings(self, health: ProcessHealth, free: float) -> list[str]:
        limits = self.config.health
        warnings: list[str] = []
        if free < self.config.retention.min_free_gb:
            warnings.append(f"disk low: {free:.1f} GB free")
        if health.memory_mb is not None and health.memory_mb > limits.max_memory_mb:
            warnings.append(f"memory high: {health.memory_mb:.0f} MB")
        if health.handles is not None and health.handles > limits.max_handles:
            warnings.append(f"handles high: {health.handles} (leak?)")
        backlog = self._captioner.backlog if self._captioner else 0
        if backlog > limits.max_captions_backlog:
            warnings.append(f"captions behind: {backlog} clips waiting")
        if len(self._clip_tasks) > limits.max_clips_in_flight:
            warnings.append(f"clips piling up: {len(self._clip_tasks)} in flight")
        for state in self.states.values():
            if not state.blocked and state.next_restart_at:
                warnings.append(f"{state.name}: recorder restarting ({state.restarts} restarts)")
        return warnings

    def _status(self, *, running: bool) -> dict[str, object]:
        now = self._clock()
        health = sample_health()
        free = free_gb(self.config.data_dir)
        # Chat is counted since the previous write, which is shorter than the interval
        # for the final write at shutdown.
        elapsed = max(now - self._status_since, 1.0)
        self._status_since = now
        channels: dict[str, object] = {}
        for state in list(self.states.values()):  # discovery may change it meanwhile
            segments = channel_segments(self.config.buffer_dir / state.name)
            video_age = round(now - segments[-1].end, 1) if segments else None
            if state.blocked:
                status = "blocked"
            elif not running:
                status = "stopped"
            elif state.thread is not None and state.thread.is_alive():
                status = "running"
            else:
                status = "restarting"
            channels[state.name] = {
                "state": status,
                "video_age_s": video_age,
                "chat_per_min": round(state.chat_events * 60 / elapsed),
                "restarts": state.restarts,
                "detector_restarts": state.detector_restarts,
                "clips": state.clips,
                "live": None if self._helix is None else state.stream is not None,
                "game": state.stream.game if state.stream is not None else None,
                "source": state.source,
                "budget_skipped": state.budget_skipped,
            }
            state.chat_events = 0
        return {
            "running": running,
            "pid": os.getpid(),
            "started_utc": _iso(self._started),
            "updated_utc": _iso(now),
            "clips_in_flight": len(self._clip_tasks),
            "captions_backlog": self._captioner.backlog if self._captioner else 0,
            "health": {**health.to_json(), "free_disk_gb": round(free, 1)},
            "machine": sample_machine().to_json(),
            "warnings": self._warnings(health, free) if running else [],
            "channels": channels,
        }

    def _write_status(self, *, running: bool) -> None:
        path = self.config.run_dir / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with self._status_lock:
            temp.write_text(json.dumps(self._status(running=running), indent=1), encoding="utf-8")
            for attempt in range(5):
                try:
                    temp.replace(path)
                    break
                except PermissionError:  # Windows: the review page is reading it right now
                    if attempt == 4:
                        raise
                    time.sleep(0.05)

    async def _report_status(self) -> None:
        while True:
            await asyncio.sleep(self.config.supervisor.status_interval_s)
            try:
                await asyncio.to_thread(self._write_status, running=True)
            except Exception:
                # One failed write must not stop the reports: the page would show the
                # pipeline as stopped while it runs.
                log.exception("status not written")

    async def _cleanup(self) -> None:
        """Apply the retention rules now and every ``retention.interval_min``."""

        def once() -> None:
            conn = connect(self.config.db_path)
            try:
                run_cleanup(self.config, conn)
            finally:
                conn.close()

        while True:
            try:
                await asyncio.to_thread(once)
            except Exception:
                log.exception("cleanup failed; retrying at the next interval")
            await asyncio.sleep(self.config.retention.interval_min * 60)

    async def _prune(self) -> None:
        keep_s = self.config.record.buffer_minutes * 60

        def prune_all() -> int:
            # Every buffer folder, not only running channels: a channel dropped by
            # discovery still has video to age out.
            root = self.config.buffer_dir
            if not root.is_dir():
                return 0
            blocked = {s.name for s in list(self.states.values()) if s.blocked}
            return sum(
                prune_channel(folder, keep_s)[1]
                for folder in root.iterdir()
                if folder.is_dir() and folder.name not in blocked
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
                asyncio.create_task(self._cleanup(), name="cleanup"),
                asyncio.create_task(self._clipper.resume(), name="resume-clips"),
            ]
            if self._helix is not None or self._kick is not None:
                helpers.append(asyncio.create_task(self._poll_streams(), name="streams"))
            else:
                log.info("go-live polling off: no Twitch credentials (`peakreel twitch login`)")
            if self.config.discover.enabled:
                clients = {"twitch": self._helix, "kick": self._kick}
                missing = [p for p in self.config.discover.platforms if clients[p] is None]
                if missing:
                    log.error("discovery skips platforms without credentials", platforms=missing)
                helpers.append(asyncio.create_task(self._discover(), name="discover"))
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

    async def _shutdown(self, chat: asyncio.Task[None], helpers: list[asyncio.Task[Any]]) -> None:
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
