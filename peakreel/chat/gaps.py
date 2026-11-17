"""Measure ingest downtime from recorded chat logs.

A gap between two chat messages says nothing on its own: the chat may simply have
been quiet. What matters is whether the recorder was *able* to receive messages.
That is reconstructed from the connection events stored alongside the messages:

- A ``disconnected`` or ``stopped`` event carries ``last_alive_ts``, the last moment
  the connection was proven alive.
- The next ``joined`` event marks when data flows again.

The interval between the two is a :class:`Downtime`. If a ``joined`` event follows
other records without a ``disconnected`` or ``stopped`` in between, the recorder
died without writing a shutdown record, and the downtime starts at the last record
seen before it.

While connected, an outage is noticed after at most ``keepalive_interval_s +
keepalive_timeout_s``, so that sum bounds any loss not covered by a downtime.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from peakreel.chat.base import ChatMessage
from peakreel.chat.replay import find_log_files, read_file

__all__ = ["UNCLEAN_STOP", "ChannelGapReport", "Downtime", "analyze_gaps"]

UNCLEAN_STOP = "unclean stop: recorder ended without a shutdown record"


@dataclass(frozen=True, slots=True)
class Downtime:
    """An interval during which a channel's chat may not have been recorded.

    Attributes
    ----------
    start_ts : float
        Last moment the connection was proven alive, UTC epoch seconds.
    end_ts : float or None
        When the channel was joined again. ``None`` if the log ends while down.
    reason : str
        Why the connection ended.
    intentional : bool
        ``True`` when the recorder was stopped on purpose (a ``stopped`` event).
        Intentional downtime is reported but is not an ingest failure.
    """

    start_ts: float
    end_ts: float | None
    reason: str
    intentional: bool

    @property
    def duration_s(self) -> float | None:
        """Length in seconds, or ``None`` while still down."""
        return None if self.end_ts is None else max(0.0, self.end_ts - self.start_ts)


@dataclass(slots=True)
class ChannelGapReport:
    """Liveness summary for one channel.

    Attributes
    ----------
    channel : str
    first_ts, last_ts : float or None
        Timestamps of the first and last record. ``None`` for an empty log.
    messages : int
        Number of chat messages (filtered ones included).
    sessions : int
        Number of ``joined`` events.
    downtimes : list of Downtime
        Every downtime, in order, regardless of length.
    max_message_gap_s : float
        Longest interval between consecutive messages within one session.
        Informational only: it reflects chat activity, not ingest health.
    max_message_gap_at : float or None
        Start of that interval.
    """

    channel: str
    first_ts: float | None = None
    last_ts: float | None = None
    messages: int = 0
    sessions: int = 0
    downtimes: list[Downtime] = field(default_factory=list[Downtime])
    max_message_gap_s: float = 0.0
    max_message_gap_at: float | None = None

    @property
    def recorded_s(self) -> float:
        """Span from first to last record, in seconds."""
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return self.last_ts - self.first_ts

    def failures(self, threshold_s: float) -> list[Downtime]:
        """Return unintentional downtimes longer than ``threshold_s``.

        Parameters
        ----------
        threshold_s : float
            Downtimes at or below this length are tolerated.

        Returns
        -------
        list of Downtime
            Includes a downtime that is still open at the end of the log.
        """
        return [
            d
            for d in self.downtimes
            if not d.intentional and (d.duration_s is None or d.duration_s > threshold_s)
        ]


def analyze_gaps(paths: Iterable[Path]) -> list[ChannelGapReport]:
    """Build a liveness report per channel from recorded logs.

    Parameters
    ----------
    paths : iterable of pathlib.Path
        Files or directories, see :func:`peakreel.chat.replay.find_log_files`.

    Returns
    -------
    list of ChannelGapReport
        One report per channel, in alphabetical order.

    Raises
    ------
    FileNotFoundError
        If a given path does not exist.

    Examples
    --------
    >>> for report in analyze_gaps([Path("data/chat")]):  # doctest: +SKIP
    ...     print(report.channel, len(report.failures(threshold_s=5.0)))
    """
    reports: list[ChannelGapReport] = []
    for channel, files in find_log_files(paths).items():
        report = ChannelGapReport(channel=channel)
        # (last_alive_ts, reason, intentional) of a connection that has ended.
        ended: tuple[float, str, bool] | None = None
        last_message_ts: float | None = None
        for file in files:
            for event in read_file(file):
                if isinstance(event, ChatMessage):
                    report.messages += 1
                    if last_message_ts is not None:
                        gap = event.ts - last_message_ts
                        if gap > report.max_message_gap_s:
                            report.max_message_gap_s = gap
                            report.max_message_gap_at = last_message_ts
                    last_message_ts = event.ts
                elif event.event == "joined":
                    report.sessions += 1
                    if ended is None and report.last_ts is not None:
                        ended = (report.last_ts, UNCLEAN_STOP, False)
                    if ended is not None:
                        report.downtimes.append(Downtime(ended[0], event.ts, ended[1], ended[2]))
                    ended = None
                    last_message_ts = None
                else:
                    start = event.last_alive_ts if event.last_alive_ts is not None else event.ts
                    ended = (start, event.reason or event.event, event.event == "stopped")
                if report.first_ts is None:
                    report.first_ts = event.ts
                report.last_ts = event.ts
        if ended is not None and not ended[2]:
            report.downtimes.append(Downtime(ended[0], None, ended[1], False))
        reports.append(report)
    return reports
