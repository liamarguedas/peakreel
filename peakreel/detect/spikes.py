"""Chat spike detector.

The detector turns one channel's chat into scored events ("spikes"). It is a
streaming state machine, so the same code runs offline over recorded logs (M2) and
live inside the clipper (M4).

Algorithm
---------
1. Messages are counted in fixed buckets of ``bucket_s`` seconds, aligned to
   multiples of ``bucket_s`` in epoch time. Only unfiltered ``privmsg`` messages
   count; bot and command messages, and platform notices, do not.
2. Each closed bucket is compared with a rolling baseline: the median and median
   absolute deviation (MAD) of the previous ``baseline_buckets`` valid buckets.
3. A bucket is *hot* when ``count > max(floor, median + k * MAD)`` and it has at
   least ``min_unique`` distinct chatters, once the baseline holds at least
   ``warmup_buckets`` buckets.
4. Hot buckets separated by at most ``merge_gap`` cold buckets form one event. An
   event ends when it goes cold or reaches ``max_event_s``, and is followed by a
   ``cooldown_s`` period in which nothing triggers.
5. Each event gets a score, ``(peak - median) / (MAD + 1)``, and the statistics that
   explain it.

Missing data is unknown, not zero
---------------------------------
A bucket that overlaps a period in which the recorder was not connected (taken from
the connection events recorded by M1) is *invalid*: it never enters the baseline and
never triggers. Treating it as zero messages would fake a quiet period and make the
next normal minute look like a spike. A downtime longer than
``reset_after_downtime_s`` clears the baseline, since chat levels may have changed.

Suppression
-----------
Events caused by things other than an on-stream moment are kept but marked with
``suppressed_by``:

- ``"raid"``: a raid arrived within ``raid_suppress_s`` before or during the event.
- ``"sub_train"``: at least ``sub_train_min`` sub, resub or gift notices arrived in
  the ``sub_train_window_s`` before or during the event.
- ``"spam"``: fewer than ``min_unique_ratio`` distinct chatters per message, which
  is what a few accounts pasting the same text look like.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from peakreel.chat.base import ChatEvent, ChatMessage
from peakreel.config import DetectParams

__all__ = [
    "SUB_NOTICE_IDS",
    "DetectionResult",
    "Spike",
    "SpikeDetector",
    "detect_spikes",
    "normalize_text",
]

SUB_NOTICE_IDS: frozenset[str] = frozenset(
    {
        "sub",
        "resub",
        "subgift",
        "submysterygift",
        "anonsubgift",
        "anonsubmysterygift",
        "giftpaidupgrade",
        "anongiftpaidupgrade",
        "primepaidupgrade",
    }
)
"""``msg-id`` values of ``USERNOTICE`` messages that count towards a sub train."""

_MAX_TEXT_CHARS = 80


def normalize_text(text: str) -> str:
    """Normalise a message for frequency counting.

    Lower-cases, collapses whitespace, collapses immediate token repeats (so
    ``"LUL LUL LUL"`` and ``"lul"`` count as the same reaction) and truncates to 80
    characters.

    Parameters
    ----------
    text : str

    Returns
    -------
    str

    Examples
    --------
    >>> normalize_text("  KEKW KEKW   kekw  no way ")
    'kekw no way'
    """
    tokens: list[str] = []
    for token in text.lower().split():
        if not tokens or tokens[-1] != token:
            tokens.append(token)
    return " ".join(tokens)[:_MAX_TEXT_CHARS]


@dataclass(frozen=True, slots=True)
class Spike:
    """A detected chat event with the statistics that explain it.

    Attributes
    ----------
    channel : str
    start_ts, end_ts : float
        Start of the first and end of the last hot bucket, UTC epoch seconds
        (local receive clock, like the chat log).
    peak_ts : float
        Start of the bucket with the most messages.
    peak_count : int
        Messages in that bucket.
    messages : int
        Counted messages over the whole event.
    unique_chatters : int
        Distinct chatters over the whole event.
    baseline_median, baseline_mad : float
        Baseline when the event started.
    score : float
        ``(peak_count - baseline_median) / (baseline_mad + 1)``.
    top_messages : tuple of (str, int)
        Most frequent normalised messages and their counts, most frequent first.
    receive_delay_s : float or None
        Median of ``ts - server_ts`` over the event's messages. Subtracting it from a
        timestamp converts the local clock to Twitch's clock.
    suppressed_by : str or None
        ``None``, ``"raid"``, ``"sub_train"`` or ``"spam"``.
    """

    channel: str
    start_ts: float
    end_ts: float
    peak_ts: float
    peak_count: int
    messages: int
    unique_chatters: int
    baseline_median: float
    baseline_mad: float
    score: float
    top_messages: tuple[tuple[str, int], ...]
    receive_delay_s: float | None
    suppressed_by: str | None = None

    @property
    def event_id(self) -> str:
        """Stable identifier, ``<channel>-<start epoch seconds>``."""
        return f"{self.channel}-{int(self.start_ts)}"

    @property
    def duration_s(self) -> float:
        """Event length in seconds."""
        return self.end_ts - self.start_ts


@dataclass(slots=True)
class _Bucket:
    index: int
    count: int = 0
    users: set[str] = field(default_factory=set[str])
    texts: Counter[str] = field(default_factory=Counter[str])
    delays: list[float] = field(default_factory=list[float])


@dataclass(slots=True)
class _OpenEvent:
    median: float
    mad: float
    buckets: list[_Bucket] = field(default_factory=list[_Bucket])


class SpikeDetector:
    """Streaming spike detector for one channel.

    Parameters
    ----------
    channel : str
        Channel login. Events for other channels are rejected.
    params : DetectParams

    Attributes
    ----------
    valid_seconds : float
        Total length of the valid (fully connected) buckets seen so far.
    active_seconds : float
        Total length of valid buckets with at least one counted message. An offline
        channel is connected but silent, so this is the fair denominator for
        "events per hour".

    Notes
    -----
    Feed events in recorded order. Timestamps that go backwards (a clock correction)
    are counted in the current bucket rather than reopening an old one.

    Examples
    --------
    >>> from peakreel.config import DetectParams
    >>> detector = SpikeDetector("somestreamer", DetectParams())
    >>> spikes = []
    >>> for event in events:  # doctest: +SKIP
    ...     spikes += detector.feed(event)
    >>> spikes += detector.flush()  # doctest: +SKIP
    """

    def __init__(self, channel: str, params: DetectParams) -> None:
        self.channel = channel
        self.params = params
        self.valid_seconds = 0.0
        self.active_seconds = 0.0
        self._bucket: _Bucket | None = None
        self._history: deque[int] = deque(maxlen=params.baseline_buckets)
        # Start of the current downtime. Before the first join nothing is known.
        self._down_since: float | None = -math.inf
        self._downtimes: list[tuple[float, float]] = []
        self._event: _OpenEvent | None = None
        self._gap: list[_Bucket] = []
        self._cooldown_until = -math.inf
        self._raids: deque[float] = deque()
        self._subs: deque[float] = deque()
        self._ready: list[Spike] = []

    def feed(self, event: ChatEvent) -> list[Spike]:
        """Process one event and return any spikes that finished.

        Parameters
        ----------
        event : ChatMessage or ConnectionEvent
            Must belong to this detector's channel.

        Returns
        -------
        list of Spike
            Spikes completed by time advancing to ``event.ts``. Usually empty.

        Raises
        ------
        ValueError
            If the event is for another channel.
        """
        if event.channel != self.channel:
            raise ValueError(f"event for {event.channel!r} fed to detector for {self.channel!r}")
        self._advance_to(event.ts)
        if isinstance(event, ChatMessage):
            self._on_message(event)
        elif event.event == "joined":
            self._on_joined(event.ts)
        elif self._down_since is None:
            alive = event.last_alive_ts if event.last_alive_ts is not None else event.ts
            self._down_since = min(alive, event.ts)
        return self._take_ready()

    def advance(self, ts: float) -> list[Spike]:
        """Close every bucket that ends at or before ``ts``.

        Live callers use this on a timer so events finish even when chat goes quiet.

        Parameters
        ----------
        ts : float
            Current time, UTC epoch seconds.

        Returns
        -------
        list of Spike
        """
        self._advance_to(ts)
        return self._take_ready()

    def flush(self) -> list[Spike]:
        """Finish an event still open at the end of the input.

        The current partial bucket is not judged, since its count is incomplete.

        Returns
        -------
        list of Spike
        """
        if self._event is not None:
            self._finalize()
        return self._take_ready()

    # -- internals -------------------------------------------------------------

    def _take_ready(self) -> list[Spike]:
        ready, self._ready = self._ready, []
        return ready

    def _on_joined(self, ts: float) -> None:
        if self._down_since is None:
            return
        start = self._down_since
        self._downtimes.append((start, ts))
        if ts - start > self.params.reset_after_downtime_s:
            self._history.clear()
        self._down_since = None

    def _on_message(self, message: ChatMessage) -> None:
        if self._down_since == -math.inf:
            # A log that starts mid-session: a message proves we are connected now.
            self._on_joined(message.ts)
        if message.type == "usernotice":
            kind = message.tags.get("msg-id", "")
            if kind == "raid":
                self._raids.append(message.ts)
            elif kind in SUB_NOTICE_IDS:
                self._subs.append(message.ts)
            return
        if message.filtered is not None:
            return
        bucket = self._bucket
        if bucket is None:
            return
        bucket.count += 1
        bucket.users.add(message.user)
        bucket.texts[normalize_text(message.text)] += 1
        if message.server_ts is not None:
            bucket.delays.append(message.ts - message.server_ts)

    def _advance_to(self, ts: float) -> None:
        index = math.floor(ts / self.params.bucket_s)
        if self._bucket is None:
            self._bucket = _Bucket(index)
            return
        while self._bucket.index < index:
            self._close(self._bucket)
            self._bucket = _Bucket(self._bucket.index + 1)

    def _is_invalid(self, bucket: _Bucket) -> bool:
        start = bucket.index * self.params.bucket_s
        end = start + self.params.bucket_s
        if self._down_since is not None and self._down_since < end:
            return True
        self._downtimes = [(s, e) for s, e in self._downtimes if e > start]
        return any(s < end and e > start for s, e in self._downtimes)

    def _close(self, bucket: _Bucket) -> None:
        params = self.params
        if self._is_invalid(bucket):
            if self._event is not None:
                self._finalize()
            return
        self.valid_seconds += params.bucket_s
        if bucket.count:
            self.active_seconds += params.bucket_s
        start = bucket.index * params.bucket_s

        if self._event is not None:
            median, mad = self._event.median, self._event.mad
            can_trigger = True
        elif len(self._history) >= params.warmup_buckets and start >= self._cooldown_until:
            median = statistics.median(self._history)
            mad = statistics.median(abs(x - median) for x in self._history)
            can_trigger = True
        else:
            median = mad = 0.0
            can_trigger = False

        hot = (
            can_trigger
            and bucket.count > max(params.floor, median + params.k * mad)
            and len(bucket.users) >= params.min_unique
        )

        if self._event is None:
            if hot:
                self._event = _OpenEvent(median=median, mad=mad, buckets=[bucket])
        elif hot:
            self._event.buckets.extend(self._gap)
            self._event.buckets.append(bucket)
            self._gap.clear()
        else:
            self._gap.append(bucket)
            if len(self._gap) > params.merge_gap:
                self._finalize()

        event = self._event
        if event is not None and self._span(event) >= params.max_event_s:
            self._finalize()
        self._history.append(bucket.count)

    def _span(self, event: _OpenEvent) -> float:
        return (event.buckets[-1].index - event.buckets[0].index + 1) * self.params.bucket_s

    def _finalize(self) -> None:
        event = self._event
        if event is None:
            return
        params = self.params
        buckets = event.buckets
        start = buckets[0].index * params.bucket_s
        end = (buckets[-1].index + 1) * params.bucket_s
        peak = max(buckets, key=lambda b: (b.count, -b.index))
        users: set[str] = set()
        texts: Counter[str] = Counter()
        delays: list[float] = []
        for b in buckets:
            users |= b.users
            texts.update(b.texts)
            delays.extend(b.delays)
        messages = sum(b.count for b in buckets)
        top = sorted(texts.items(), key=lambda item: (-item[1], item[0]))[: params.top_messages]

        suppressed: str | None = None
        if any(start - params.raid_suppress_s <= r <= end for r in self._raids):
            suppressed = "raid"
        elif (
            sum(1 for s in self._subs if start - params.sub_train_window_s <= s <= end)
            >= params.sub_train_min
        ):
            suppressed = "sub_train"
        elif messages and len(users) / messages < params.min_unique_ratio:
            suppressed = "spam"

        self._ready.append(
            Spike(
                channel=self.channel,
                start_ts=start,
                end_ts=end,
                peak_ts=peak.index * params.bucket_s,
                peak_count=peak.count,
                messages=messages,
                unique_chatters=len(users),
                baseline_median=event.median,
                baseline_mad=event.mad,
                score=round((peak.count - event.median) / (event.mad + 1), 2),
                top_messages=tuple(top),
                receive_delay_s=round(statistics.median(delays), 3) if delays else None,
                suppressed_by=suppressed,
            )
        )
        self._event = None
        self._gap.clear()
        self._cooldown_until = end + params.cooldown_s
        horizon = end - max(params.raid_suppress_s, params.sub_train_window_s)
        while self._raids and self._raids[0] < horizon:
            self._raids.popleft()
        while self._subs and self._subs[0] < horizon:
            self._subs.popleft()


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Output of :func:`detect_spikes`.

    Attributes
    ----------
    spikes : tuple of Spike
        Every spike, suppressed ones included, ordered by ``(start_ts, channel)``.
    valid_seconds : dict of str to float
        Usable (fully connected) seconds per channel.
    active_seconds : dict of str to float
        Usable seconds with chat activity per channel.
    """

    spikes: tuple[Spike, ...]
    valid_seconds: dict[str, float]
    active_seconds: dict[str, float]

    @property
    def valid_hours(self) -> float:
        """Usable hours summed over all channels."""
        return sum(self.valid_seconds.values()) / 3600

    @property
    def active_hours(self) -> float:
        """Active hours summed over all channels."""
        return sum(self.active_seconds.values()) / 3600


def detect_spikes(events: Iterable[ChatEvent], params: DetectParams) -> DetectionResult:
    """Run one detector per channel over a recorded event stream.

    Parameters
    ----------
    events : iterable of ChatMessage or ConnectionEvent
        Typically :func:`peakreel.chat.replay.read_events`. Connection events are
        required for correct downtime handling.
    params : DetectParams

    Returns
    -------
    DetectionResult

    Examples
    --------
    >>> from pathlib import Path
    >>> from peakreel.chat.replay import read_events
    >>> result = detect_spikes(read_events([Path("data/chat")]), DetectParams())  # doctest: +SKIP
    >>> [s.event_id for s in result.spikes if s.suppressed_by is None]  # doctest: +SKIP
    ['xqc-1794900540']
    """
    detectors: dict[str, SpikeDetector] = {}
    spikes: list[Spike] = []
    for event in events:
        detector = detectors.get(event.channel)
        if detector is None:
            detector = detectors[event.channel] = SpikeDetector(event.channel, params)
        spikes.extend(detector.feed(event))
    for detector in detectors.values():
        spikes.extend(detector.flush())
    spikes.sort(key=lambda s: (s.start_ts, s.channel))
    return DetectionResult(
        spikes=tuple(spikes),
        valid_seconds={name: d.valid_seconds for name, d in sorted(detectors.items())},
        active_seconds={name: d.active_seconds for name, d in sorted(detectors.items())},
    )
