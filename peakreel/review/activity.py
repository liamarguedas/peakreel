"""What Peakreel is doing, in plain words, for the review page's Activity tab.

Read from the end of the JSON-lines log (``logging.file``): clips detected and
skipped, auto review decisions, uploads and their results, channels starting and
stopping, and errors. Chat traffic, buffer pruning and the like are left out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

__all__ = ["activity", "read_activity"]

_TAIL_BYTES = 4_000_000  # about the last 15 000 log lines


def _text(e: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(kind, text)`` for an event worth showing, or ``None``."""
    event = e.get("event")
    clip = f"#{e['clip_id']}" if e.get("clip_id") is not None else ""
    channel = e.get("channel", "")
    if event == "clip ready":
        return "detect", f"Clip detected {clip} · {channel} · score {e.get('score')}"
    if event == "spike suppressed":
        reason = {"unexcited": "chat not excited", "budget": "hourly clip budget"}.get(
            str(e.get("reason")), str(e.get("reason"))
        )
        return "skip", f"Moment skipped · {channel} · {reason}"
    if event == "auto review":
        verb = {"approve": "approve", "reject": "reject", "review": "leave for you"}.get(
            str(e.get("decision")), str(e.get("decision"))
        )
        done = "" if e.get("applied") else " (shadow)"
        return "review", f"Auto review {clip}: {verb}{done} · {str(e.get('reason', ''))[:160]}"
    if event == "clip reviewed":
        return "review", f"Clip {clip} {e.get('state')}"
    if event == "publication queued":
        return (
            "queue",
            f"Queued {clip} for {e.get('account')} {e.get('platform')} ({e.get('mode')})",
        )
    if event == "publication":
        state = str(e.get("state"))
        kind = "fail" if state == "failed" else "publish"
        label = {"processing": "uploaded, platform processing", "published": "published",
                 "exported": "export package written", "private_only": "uploaded (private)",
                 "failed": "FAILED"}.get(state, state)  # fmt: skip
        return (
            kind,
            f"{clip} {e.get('account')} {e.get('platform')}: {label} · {e.get('detail', '')}",
        )
    if event == "channel added":
        return "channel", f"Recording {channel} ({e.get('source', 'manual')})"
    if event in ("channel removed", "channel offline"):
        return "channel", f"Stopped {channel}: {e.get('reason', 'offline')}"
    if e.get("level") == "error":
        where = f" · {channel}" if channel else ""
        detail = " ".join(str(e.get("error", "")).split())[:160]
        return "fail", f"Error · {' '.join(str(event).split())[:120]}{where} · {detail}"
    return None


def activity(lines: Iterable[str], limit: int = 200) -> list[dict[str, str]]:
    """Return the latest interesting events, newest first.

    Parameters
    ----------
    lines : iterable of str
        JSON log lines, oldest first; lines that do not parse are skipped.
    limit : int, default 200

    Returns
    -------
    list of dict
        ``{"time": ISO UTC, "kind": detect|skip|review|queue|publish|fail|channel, "text"}``.

    Examples
    --------
    >>> _ = activity(['{"event": "clip ready", "clip_id": 7, "channel": "speed", "score": 9.4, '
    ...           '"timestamp": "2026-11-18T23:54:38Z"}', 'junk',
    ...           '{"event": "chat throughput", "timestamp": "2026-11-18T23:55:00Z"}'])
    >>> [(a["kind"], a["text"]) for a in _]
    [('detect', 'Clip detected #7 · speed · score 9.4')]
    """
    out: list[dict[str, str]] = []
    for line in lines:
        try:
            event: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        fields = cast("dict[str, Any]", event)
        shown = _text(fields)
        if shown is not None:
            out.append(
                {"time": str(fields.get("timestamp", "")), "kind": shown[0], "text": shown[1]}
            )
    return out[::-1][:limit]


def read_activity(log_file: Path, limit: int = 200) -> list[dict[str, str]]:
    """Return :func:`activity` for the end of ``log_file`` (empty if there is none)."""
    if not log_file.is_file():
        return []
    with log_file.open("rb") as f:
        f.seek(max(0, log_file.stat().st_size - _TAIL_BYTES))
        data = f.read().decode("utf-8", "replace")
    return activity(data.splitlines()[1:], limit)  # the first line may be cut in half
