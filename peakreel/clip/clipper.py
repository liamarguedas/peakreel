"""Cut a clip for each detected spike.

For every spike the clipper:

1. Records the spike in SQLite, suppressed or not, so every decision is traceable.
2. Skips suppressed spikes and blocklisted channels (no clip).
3. Converts the spike start from the local chat clock to the broadcast clock with
   the spike's measured receive delay, and builds the window
   ``[moment - pre_roll_s, moment + post_roll_s]``.
4. Waits until the video buffer covers the window (segments arrive 3 to 7 s after
   broadcast), up to ``max_wait_s``; then cuts what is there and records any gap.
5. Cuts with ffmpeg into ``<outbox>/<channel>/``, checks the blocklist again (a
   takedown may have arrived during the cut), then publishes the clip and a JSON
   sidecar atomically and records the clip in SQLite.
6. Captions it (M6) on the captioner's worker thread, then renders the vertical
   version and thumbnail (M5) next to it. A captioning failure renders without
   captions; a render failure keeps the horizontal clip.

The blocklist file is re-read for every spike, so adding a streamer to it takes
effect without a restart.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from peakreel.blocklist import load_blocklist
from peakreel.captions.whisper import Captioner
from peakreel.config import ClipConfig, Config, ConfigError
from peakreel.db import connect, ensure_channel, insert_clip, insert_spike
from peakreel.detect.spikes import Spike
from peakreel.log import get_logger
from peakreel.record.buffer import (
    Window,
    WindowUnavailable,
    channel_segments,
    cut_window,
    get_window,
)
from peakreel.render.vertical import RenderedClip, render_clip

__all__ = ["ClipResult", "Clipper", "clip_window_for"]

log = get_logger(__name__)

Cutter = Callable[[Window, Path, Path], None]
Renderer = Callable[..., RenderedClip]


def clip_window_for(spike: Spike, settings: ClipConfig) -> tuple[float, float]:
    """Return the broadcast-clock range to clip for ``spike``.

    Parameters
    ----------
    spike : Spike
        Its times are on the local chat clock.
    settings : ClipConfig

    Returns
    -------
    tuple of (float, float)
        ``(start, end)`` on the broadcast clock (Twitch's), UTC epoch seconds.

    Notes
    -----
    ``receive_delay_s`` is the median of ``local receive time - Twitch timestamp`` over
    the spike's messages, so subtracting it moves the spike onto Twitch's clock, the
    clock of the video segments. Without it (no server timestamps), the local clock is
    used as is; ``peakreel doctor`` keeps it within 2 s of Twitch's.
    """
    moment = spike.start_ts - (spike.receive_delay_s or 0.0)
    return moment - settings.pre_roll_s, moment + settings.post_roll_s


@dataclass(frozen=True, slots=True)
class ClipResult:
    """What happened to one spike.

    Attributes
    ----------
    spike_id : int
        Row id in ``spikes``.
    status : str
        ``"clipped"``, ``"suppressed"``, ``"blocked"``, ``"no_video"`` or ``"failed"``.
    clip_path, sidecar_path : pathlib.Path or None
        Set when ``status == "clipped"``.
    gaps : tuple of (float, float)
        Missing video inside the clip window.
    vertical_path, thumbnail_path : pathlib.Path or None
        The vertical render and its thumbnail, when rendering succeeded.
    """

    spike_id: int
    status: str
    clip_path: Path | None = None
    sidecar_path: Path | None = None
    gaps: tuple[tuple[float, float], ...] = ()
    vertical_path: Path | None = None
    thumbnail_path: Path | None = None


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y%m%dT%H%M%SZ")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Clipper:
    """Turn spikes into clips. One instance serves every channel.

    Parameters
    ----------
    config : Config
        Supplies clip settings, paths, the database and the blocklist.
    ffmpeg : pathlib.Path
        The ffmpeg executable.
    cutter : callable, optional
        ``cutter(window, out_path, ffmpeg)``. Defaults to
        :func:`peakreel.record.buffer.cut_window`; injectable for tests.
    clock : callable, optional
        Returns local epoch seconds.
    poll_s : float, default 1.0
        How often the buffer is checked while waiting for the window to arrive.
    renderer : callable or None, optional
        ``renderer(config, clip_path, ffmpeg)`` producing the vertical version,
        run after the clip is published. Defaults to
        :func:`peakreel.render.vertical.render_clip`; ``None`` disables it. A
        render failure keeps the horizontal clip and is noted in its sidecar.
    captioner : Captioner or None, optional
        Transcribes the clip on its own worker thread before rendering; ``None``
        renders without captions. A captioning failure renders uncaptioned.

    Notes
    -----
    :meth:`handle` is a coroutine. Database access and ffmpeg run in worker threads,
    and ``clip.concurrent_cuts`` limits simultaneous encodes, so the event loop
    (chat ingest, detection) never blocks.
    """

    def __init__(
        self,
        config: Config,
        ffmpeg: Path,
        *,
        cutter: Cutter = cut_window,
        clock: Callable[[], float] = time.time,
        poll_s: float = 1.0,
        renderer: Renderer | None = render_clip,
        captioner: Captioner | None = None,
    ) -> None:
        self._config = config
        self._settings = config.clip
        self._ffmpeg = ffmpeg
        self._cutter = cutter
        self._clock = clock
        self._poll_s = poll_s
        self._renderer = renderer
        self._captioner = captioner
        self._cuts = asyncio.Semaphore(config.clip.concurrent_cuts)

    # -- helpers --------------------------------------------------------------

    def _blocked(self, channel: str) -> bool:
        try:
            return channel in load_blocklist(self._config.blocklist_path)
        except ConfigError:
            # Fail closed: an unreadable takedown list must not let clips through.
            log.exception("blocklist unreadable; treating every channel as blocked")
            return True

    def _db(self) -> sqlite3.Connection:
        return connect(self._config.db_path)

    def _record_spike(self, spike: Spike) -> int:
        conn = self._db()
        try:
            channel = self._config.channel(spike.channel)
            channel_id = ensure_channel(
                conn, spike.channel, channel.credit_text if channel else None
            )
            spike_id = insert_spike(conn, channel_id, spike)
            conn.commit()
            return spike_id
        finally:
            conn.close()

    def _record_clip(
        self, spike: Spike, spike_id: int, window: Window, clip: Path, sidecar: Path
    ) -> int:
        conn = self._db()
        try:
            channel_id = ensure_channel(conn, spike.channel)
            clip_id = insert_clip(
                conn,
                channel_id=channel_id,
                spike_id=spike_id,
                start_ts=window.start,
                end_ts=window.end,
                path=clip,
                sidecar_path=sidecar,
            )
            conn.commit()
            return clip_id
        finally:
            conn.close()

    def _record_render(self, clip_id: int, vertical: Path, thumbnail: Path) -> None:
        conn = self._db()
        try:
            conn.execute(
                "UPDATE clips SET vertical_path = ?, thumbnail_path = ? WHERE id = ?",
                (str(vertical), str(thumbnail), clip_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _newest_video(self, channel: str) -> float | None:
        segments = channel_segments(self._config.buffer_dir / channel)
        return segments[-1].end if segments else None

    async def _wait_for_video(self, channel: str, end: float) -> None:
        deadline = self._clock() + self._settings.max_wait_s
        while self._clock() < deadline:
            newest = await asyncio.to_thread(self._newest_video, channel)
            if newest is not None and newest >= end:
                return
            await asyncio.sleep(self._poll_s)
        log.warning("buffer did not cover the clip window in time", channel=channel)

    def _sidecar(self, spike: Spike, spike_id: int, window: Window, clip: Path) -> dict[str, Any]:
        channel = self._config.channel(spike.channel)
        return {
            "clip": clip.name,
            "spike_id": spike_id,
            "channel": spike.channel,
            "source": f"https://www.twitch.tv/{spike.channel}",
            "credit": channel.credit_text if channel else f"Clip from twitch.tv/{spike.channel}",
            "window": {
                "start_utc": _iso(window.start),
                "end_utc": _iso(window.end),
                "start_ts": window.start,
                "end_ts": window.end,
                "clock": "broadcast (Twitch PROGRAM-DATE-TIME)",
                "gaps": [
                    {"start_ts": a, "end_ts": b, "seconds": round(b - a, 3)} for a, b in window.gaps
                ],
            },
            "spike": {
                **{k: v for k, v in asdict(spike).items() if k != "top_messages"},
                "start_utc": _iso(spike.start_ts),
                "clock": "local chat receive time",
                "top_messages": [{"text": t, "count": c} for t, c in spike.top_messages],
            },
            "created_utc": _iso(self._clock()),
        }

    # -- main entry point ------------------------------------------------------

    async def handle(self, spike: Spike) -> ClipResult:
        """Record ``spike`` and, when appropriate, cut and publish its clip.

        Parameters
        ----------
        spike : Spike

        Returns
        -------
        ClipResult

        Notes
        -----
        Never raises for operational failures (missing video, ffmpeg errors): they
        are logged and reported in the result, so one bad clip cannot stop the
        pipeline. Cancellation propagates normally.
        """
        spike_id = await asyncio.to_thread(self._record_spike, spike)
        if spike.suppressed_by is not None:
            log.info(
                "spike suppressed",
                channel=spike.channel,
                reason=spike.suppressed_by,
                spike_id=spike_id,
            )
            return ClipResult(spike_id, "suppressed")
        if self._blocked(spike.channel):
            log.warning("channel on blocklist: no clip", channel=spike.channel, spike_id=spike_id)
            return ClipResult(spike_id, "blocked")

        start, end = clip_window_for(spike, self._settings)
        await self._wait_for_video(spike.channel, end)
        try:
            window = await asyncio.to_thread(
                get_window, self._config.buffer_dir, spike.channel, start, end
            )
        except WindowUnavailable as exc:
            log.warning(
                "no video for spike", channel=spike.channel, spike_id=spike_id, error=str(exc)
            )
            return ClipResult(spike_id, "no_video")

        out_dir = self._config.outbox_dir / spike.channel
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{_stamp(window.start)}-{spike.channel}-{spike_id}"
        clip = out_dir / f"{name}.mp4"
        sidecar = out_dir / f"{name}.json"
        temp = out_dir / f"{name}.part.mp4"
        try:
            async with self._cuts:
                await asyncio.to_thread(self._cutter, window, temp, self._ffmpeg)
        except Exception:
            log.exception("clip cut failed", channel=spike.channel, spike_id=spike_id)
            temp.unlink(missing_ok=True)
            return ClipResult(spike_id, "failed", gaps=window.gaps)

        if self._blocked(spike.channel):  # a takedown may have arrived during the cut
            temp.unlink(missing_ok=True)
            log.warning("channel blocklisted during cut: clip discarded", channel=spike.channel)
            return ClipResult(spike_id, "blocked", gaps=window.gaps)

        payload = json.dumps(
            self._sidecar(spike, spike_id, window, clip), ensure_ascii=False, indent=2
        )
        sidecar.write_text(payload + "\n", encoding="utf-8")
        temp.replace(clip)
        clip_id = await asyncio.to_thread(self._record_clip, spike, spike_id, window, clip, sidecar)
        log.info(
            "clip ready",
            channel=spike.channel,
            clip=str(clip),
            clip_id=clip_id,
            score=spike.score,
            gaps_s=round(sum(b - a for a, b in window.gaps), 1),
        )
        vertical = thumbnail = None
        if self._renderer is not None:
            renderer = self._renderer
            captions = await self._captioner.caption(clip) if self._captioner else None
            try:
                async with self._cuts:
                    rendered = await asyncio.to_thread(
                        renderer, self._config, clip, self._ffmpeg, captions=captions
                    )
            except Exception:
                log.exception("vertical render failed; horizontal clip kept", clip=clip.name)
            else:
                vertical, thumbnail = rendered.vertical, rendered.thumbnail
                await asyncio.to_thread(self._record_render, clip_id, vertical, thumbnail)
        return ClipResult(spike_id, "clipped", clip, sidecar, window.gaps, vertical, thumbnail)
