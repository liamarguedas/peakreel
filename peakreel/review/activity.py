"""What Peakreel is doing, in plain words, for the review page's Activity tab.

Read from the end of the JSON-lines log (``logging.file``) and sorted into blocks:
uploads, clips detected, the same moment found on two channels, AI review, problems,
channels starting and stopping, and moments skipped. Chat traffic, buffer pruning and
the like are left out. Each block keeps its own latest events, so the many skipped
moments never push the rarer events out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

__all__ = ["KINDS", "activity", "read_activity"]

_TAIL_BYTES = 4_000_000  # about the last 15 000 log lines

KINDS = ("upload", "detect", "duplicate", "review", "problem", "channel", "skip")
"""The blocks, in the order the page shows them."""

_PLATFORM = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "tiktok": "TikTok",
}


def _where(e: dict[str, Any]) -> str:
    """``Instagram pt2``: where a post goes."""
    platform = str(e.get("platform", ""))
    return f"{_PLATFORM.get(platform, platform)} {e.get('account', '')}".strip()


def _publication(e: dict[str, Any], clip: str) -> tuple[str, str, str]:
    state = str(e.get("state"))
    detail = " ".join(str(e.get("detail") or "").split())[:200]
    if state == "failed":
        return "problem", f"Clip {clip} not posted, gave up: {detail}", _where(e)
    if state == "approved" and detail:  # refused this time; retried later
        return "problem", f"Clip {clip} refused, will retry: {detail}", _where(e)
    label = {"published": "posted", "processing": "uploaded, the platform is processing it",
             "exported": "export package written", "private_only": "uploaded as private",
             "uploading": "uploading"}.get(state, state)  # fmt: skip
    return "upload", f"Clip {clip} {label}", _where(e)


def _text(e: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(kind, text, tag)`` for an event worth showing, or ``None``.

    ``tag`` is a short label shown beside the text: the channel, or where a post goes.
    """
    event = e.get("event")
    clip = f"#{e['clip_id']}" if e.get("clip_id") is not None else ""
    channel = str(e.get("channel", ""))
    if event == "clip ready":
        score = e.get("score")
        shown = f"{score:.1f}" if isinstance(score, int | float) else str(score)
        return "detect", f"Clip {clip} cut, chat score {shown}", channel
    if event == "same moment found":
        other = f"clip #{e.get('other')}"
        if e.get("other_channel"):  # logged since 2026-11-20
            other += f" from {e['other_channel']}"
        return "duplicate", f"Clip {clip} is the same moment as {other}", channel
    if event == "spike suppressed":
        reason = {"unexcited": "chat not excited", "budget": "hourly clip budget used"}.get(
            str(e.get("reason")), str(e.get("reason"))
        )
        return "skip", reason, channel
    if event == "auto review":
        verb = {"approve": "approved", "reject": "rejected", "review": "left for you"}.get(
            str(e.get("decision")), str(e.get("decision"))
        )
        done = "" if e.get("applied") else " (shadow, not applied)"
        why = " ".join(str(e.get("reason", "")).split())[:200]
        return "review", f"Clip {clip} {verb}{done}: {why}", "AI"
    if event == "clip reviewed":
        if e.get("by") == "auto":  # the "auto review" line already says it
            return None
        return "review", f"Clip {clip} marked {e.get('state')}", "You"
    if event == "sexual content: posts cancelled":
        return "review", f"Clip {clip} rejected for sexual content; its posts were cancelled", "AI"
    if event == "publication queued":
        return "upload", f"Clip {clip} queued", _where(e)
    if event == "publication":
        return _publication(e, clip)
    if event == "stale posts dropped":
        hours = e.get("max_wait_h")
        return "upload", f"{e.get('count')} waiting posts dropped (clips older than {hours} h)", ""
    if event == "publish postponed":
        error = " ".join(str(e.get("error", "")).split())[:200]
        return "problem", f"Clip {clip} postponed: {error}", _where(e)
    if event == "channel added":
        return "channel", f"Recording started ({e.get('source', 'manual')})", channel
    if event in ("channel removed", "channel offline"):
        return "channel", f"Recording stopped: {e.get('reason', 'offline')}", channel
    if e.get("level") == "error":
        detail = " ".join(str(e.get("error", "")).split())[:200]
        what = " ".join(str(event).split())[:120]
        return "problem", f"{what}: {detail}" if detail else what, channel
    return None


def activity(lines: Iterable[str], limit: int = 100) -> list[dict[str, str]]:
    """Return the latest interesting events, newest first, at most ``limit`` per block.

    Parameters
    ----------
    lines : iterable of str
        JSON log lines, oldest first; lines that do not parse are skipped.
    limit : int, default 100
        Most events kept per kind (block).

    Returns
    -------
    list of dict
        ``{"time": ISO UTC, "kind": one of KINDS, "text", "tag"}``.

    Examples
    --------
    >>> _ = activity(['{"event": "clip ready", "clip_id": 7, "channel": "speed", "score": 9.4, '
    ...           '"timestamp": "2026-11-18T23:54:38Z"}', 'junk',
    ...           '{"event": "chat throughput", "timestamp": "2026-11-18T23:55:00Z"}'])
    >>> [(a["kind"], a["text"], a["tag"]) for a in _]
    [('detect', 'Clip #7 cut, chat score 9.4', 'speed')]
    """
    shown: list[dict[str, str]] = []
    for line in lines:
        try:
            event: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        fields = cast("dict[str, Any]", event)
        found = _text(fields)
        if found is not None:
            kind, text, tag = found
            shown.append(
                {"time": str(fields.get("timestamp", "")), "kind": kind, "text": text, "tag": tag}
            )
    out: list[dict[str, str]] = []
    per_kind: dict[str, int] = {}
    for item in reversed(shown):
        if per_kind.get(item["kind"], 0) < limit:
            per_kind[item["kind"]] = per_kind.get(item["kind"], 0) + 1
            out.append(item)
    return out


def read_activity(log_file: Path, limit: int = 100) -> list[dict[str, str]]:
    """Return :func:`activity` for the end of ``log_file`` (empty if there is none)."""
    if not log_file.is_file():
        return []
    with log_file.open("rb") as f:
        f.seek(max(0, log_file.stat().st_size - _TAIL_BYTES))
        data = f.read().decode("utf-8", "replace")
    return activity(data.splitlines()[1:], limit)  # the first line may be cut in half
