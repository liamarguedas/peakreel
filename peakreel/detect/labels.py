"""Labeling sheet and parameter evaluation for the spike detector.

Workflow
--------
1. :func:`write_label_sheet` runs a permissive "candidate" parameter set and writes
   one CSV row per event, with a VOD link at the moment.
2. A person fills the ``label`` column with ``good``, ``bad`` or ``unsure``.
3. :func:`evaluate` runs any number of parameter sets over the same logs and scores
   each against the labels.

Events are matched to labels by channel and time overlap, not by id, because
different parameters shift event boundaries by a bucket or two.

The CSV is UTF-8 with a byte order mark so Excel shows non-Latin chat correctly. In
Excel, save it back as "CSV UTF-8".
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from peakreel.config import DetectConfig, DetectParams
from peakreel.detect.spikes import DetectionResult, Spike

__all__ = [
    "FIELDS",
    "LABEL_VALUES",
    "VOD_LEAD_S",
    "LabelError",
    "LabelRow",
    "SetScore",
    "evaluate",
    "format_results",
    "load_grid",
    "read_labels",
    "write_label_sheet",
]

FIELDS = (
    "event_id",
    "channel",
    "start_utc",
    "end_utc",
    "peak",
    "unique",
    "score",
    "top_messages",
    "vod_url",
    "label",
    "notes",
)
LABEL_VALUES = frozenset({"good", "bad", "unsure", ""})
VOD_LEAD_S = 15.0
"""The VOD link starts this many seconds before the event, since chat reacts late."""

Locate = Callable[[Spike], tuple[str, str]]
"""Returns ``(vod_url, note)`` for a spike; either may be empty."""


class LabelError(ValueError):
    """Raised when a label sheet is malformed. The message names the offending rows."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True, slots=True)
class LabelRow:
    """One row of a label sheet.

    Attributes
    ----------
    event_id, channel : str
    start_ts, end_ts : float
        UTC epoch seconds.
    label : str
        ``"good"``, ``"bad"``, ``"unsure"`` or ``""`` (not labeled yet).
    fields : dict of str to str
        The full CSV row, so rewriting the sheet preserves every column.
    """

    event_id: str
    channel: str
    start_ts: float
    end_ts: float
    label: str
    fields: dict[str, str] = field(default_factory=dict[str, str])

    def overlaps(self, spike: Spike) -> bool:
        """Whether ``spike`` is the same channel and overlaps this row in time."""
        return (
            spike.channel == self.channel
            and spike.start_ts < self.end_ts
            and spike.end_ts > self.start_ts
        )


def read_labels(path: Path) -> list[LabelRow]:
    """Read a label sheet.

    Parameters
    ----------
    path : pathlib.Path

    Returns
    -------
    list of LabelRow
        In file order. An empty list if the file does not exist.

    Raises
    ------
    LabelError
        If required columns are missing, a time cannot be parsed, or a label is not
        one of ``good``, ``bad``, ``unsure`` or empty. All bad rows are listed.
    """
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {"event_id", "channel", "start_utc", "end_utc", "label"} - set(
            reader.fieldnames or []
        )
        if missing:
            raise LabelError(f"{path}: missing column(s) {', '.join(sorted(missing))}")
        rows: list[LabelRow] = []
        problems: list[str] = []
        for line, raw in enumerate(reader, start=2):
            fields = {k: (v or "").strip() for k, v in raw.items() if k is not None}
            label = fields["label"].lower()
            try:
                start, end = _parse_iso(fields["start_utc"]), _parse_iso(fields["end_utc"])
            except ValueError:
                problems.append(f"line {line}: bad start_utc/end_utc")
                continue
            if label not in LABEL_VALUES:
                problems.append(f"line {line}: label {fields['label']!r} is not good/bad/unsure")
                continue
            fields["label"] = label
            rows.append(LabelRow(fields["event_id"], fields["channel"], start, end, label, fields))
    if problems:
        raise LabelError(f"{path}:\n  " + "\n  ".join(problems))
    return rows


def _row_for(spike: Spike, vod_url: str, note: str) -> dict[str, str]:
    return {
        "event_id": spike.event_id,
        "channel": spike.channel,
        "start_utc": _iso(spike.start_ts),
        "end_utc": _iso(spike.end_ts),
        "peak": str(spike.peak_count),
        "unique": str(spike.unique_chatters),
        "score": f"{spike.score:g}",
        "top_messages": " | ".join(f"{text} x{count}" for text, count in spike.top_messages),
        "vod_url": vod_url,
        "label": "",
        "notes": note,
    }


def write_label_sheet(path: Path, spikes: Iterable[Spike], locate: Locate | None = None) -> int:
    """Write or update a label sheet, keeping every existing label.

    Parameters
    ----------
    path : pathlib.Path
        The CSV. Created if missing.
    spikes : iterable of Spike
        Events to list, normally the unsuppressed candidate-set events.
    locate : callable, optional
        Gives each new or still unlinked row its VOD link and note.

    Returns
    -------
    int
        Number of rows added.

    Notes
    -----
    A spike that overlaps an existing row keeps that row (and its label, notes and
    link). Existing rows that no longer match any spike are kept as well, so no
    labeling work is ever lost. Rows are written in time order.
    """
    existing = read_labels(path)
    claimed: set[int] = set()
    out: list[tuple[float, str, dict[str, str]]] = []
    added = 0
    for spike in spikes:
        match = next(
            (i for i, row in enumerate(existing) if i not in claimed and row.overlaps(spike)), None
        )
        if match is not None:
            claimed.add(match)
            fields = dict(existing[match].fields)
            if not fields.get("vod_url") and locate is not None:
                fields["vod_url"], fields["notes"] = _merge_note(
                    fields.get("notes", ""), *locate(spike)
                )
            out.append((existing[match].start_ts, existing[match].channel, fields))
            continue
        vod, note = locate(spike) if locate is not None else ("", "")
        out.append((spike.start_ts, spike.channel, _row_for(spike, vod, note)))
        added += 1
    out.extend(
        (row.start_ts, row.channel, dict(row.fields))
        for i, row in enumerate(existing)
        if i not in claimed
    )
    out.sort(key=lambda item: (item[0], item[1]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for _, _, fields in out:
            writer.writerow({name: fields.get(name, "") for name in FIELDS})
    return added


def _merge_note(current: str, vod_url: str, note: str) -> tuple[str, str]:
    if not note or note in current:
        return vod_url, current
    return vod_url, f"{current}; {note}" if current else note


@dataclass(frozen=True, slots=True)
class SetScore:
    """How one parameter set performs against the labels.

    Attributes
    ----------
    name : str
    events : int
        Unsuppressed events the set produced.
    good, bad, unknown : int
        Events matched to a ``good`` label, to only ``bad`` labels, or to no
        decisive label (unlabeled, ``unsure``, or outside the sheet).
    good_found : int
        Distinct ``good`` rows matched by at least one event.
    good_total : int
        All ``good`` rows in the sheet.
    active_hours : float
        Chat-active hours of input, see :attr:`DetectionResult.active_hours`.

    Notes
    -----
    Precision counts only decisive labels; unknown events are reported, not guessed.
    Recall is measured against the candidate set's ``good`` events, so it is an
    upper bound relative to that set, not recall against every real moment.
    """

    name: str
    events: int
    good: int
    bad: int
    unknown: int
    good_found: int
    good_total: int
    active_hours: float

    @property
    def precision(self) -> float | None:
        """``good / (good + bad)``, or ``None`` with no decisive labels."""
        decided = self.good + self.bad
        return self.good / decided if decided else None

    @property
    def recall(self) -> float | None:
        """``good_found / good_total``, or ``None`` with no good labels."""
        return self.good_found / self.good_total if self.good_total else None

    @property
    def events_per_hour(self) -> float | None:
        """Events per chat-active hour."""
        return self.events / self.active_hours if self.active_hours else None


def evaluate(
    name: str,
    result: DetectionResult,
    labels: Sequence[LabelRow],
    *,
    include_suppressed: bool = False,
) -> SetScore:
    """Score one parameter set's events against the labels.

    Parameters
    ----------
    name : str
        Label for the report.
    result : DetectionResult
        Output of :func:`peakreel.detect.spikes.detect_spikes` for this set.
    labels : sequence of LabelRow
    include_suppressed : bool, default False
        Whether suppressed events count as output.

    Returns
    -------
    SetScore
    """
    spikes = [s for s in result.spikes if include_suppressed or s.suppressed_by is None]
    good = bad = unknown = 0
    found: set[int] = set()
    for spike in spikes:
        matched = [i for i, row in enumerate(labels) if row.overlaps(spike)]
        verdicts = {labels[i].label for i in matched}
        if "good" in verdicts:
            good += 1
            found.update(i for i in matched if labels[i].label == "good")
        elif "bad" in verdicts:
            bad += 1
        else:
            unknown += 1
    return SetScore(
        name=name,
        events=len(spikes),
        good=good,
        bad=bad,
        unknown=unknown,
        good_found=len(found),
        good_total=sum(1 for row in labels if row.label == "good"),
        active_hours=result.active_hours,
    )


def load_grid(path: Path, detect: DetectConfig) -> list[tuple[str, DetectParams]]:
    """Read a parameter grid for :func:`evaluate`.

    File format::

        sets:
          - name: k3
            k: 3
          - name: k3-floor15
            k: 3
            floor: 15

    Each set overrides the configured defaults.

    Parameters
    ----------
    path : pathlib.Path
    detect : DetectConfig
        The configured ``detect:`` section, providing the defaults.

    Returns
    -------
    list of (str, DetectParams)

    Raises
    ------
    LabelError
        If the file cannot be read, a set has no name, or a parameter is invalid.
    """
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LabelError(f"cannot read grid {path}: {exc}") from exc
    entries = cast("dict[str, object]", raw).get("sets") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise LabelError(f"grid {path} must be a mapping with a `sets` list")
    sets: list[tuple[str, DetectParams]] = []
    for index, entry in enumerate(cast("list[object]", entries)):
        if not isinstance(entry, dict) or "name" not in entry:
            raise LabelError(f"grid {path}: set #{index + 1} needs a `name`")
        overrides = {str(k): v for k, v in cast("dict[object, object]", entry).items()}
        name = str(overrides.pop("name"))
        try:
            sets.append((name, detect.params(overrides)))
        except ValidationError as exc:
            raise LabelError(f"grid {path}: set {name!r}: {exc}") from exc
    return sets


def format_results(scores: Sequence[SetScore]) -> str:
    """Render scores as a Markdown table.

    Parameters
    ----------
    scores : sequence of SetScore

    Returns
    -------
    str
    """

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    lines = [
        "| Set | Events | Events/active h | Good | Bad | Unknown | Precision "
        "| Recall vs candidate |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        per_hour = "n/a" if s.events_per_hour is None else f"{s.events_per_hour:.1f}"
        lines.append(
            f"| {s.name} | {s.events} | {per_hour} | {s.good} | {s.bad} | {s.unknown} "
            f"| {pct(s.precision)} | {pct(s.recall)} ({s.good_found}/{s.good_total}) |"
        )
    return "\n".join(lines)
