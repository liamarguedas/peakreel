"""Clip queries and updates for the review tool.

Approved clips are posted through the platforms' APIs (:mod:`peakreel.publish.queue`);
the copy of every approved clip once written to ``data/export`` for posting by hand was
dropped on 2026-11-19 (the ``export_path`` column is unused since).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args

from peakreel.blocklist import load_blocklist
from peakreel.config import Config, ConfigError, Platform
from peakreel.log import get_logger
from peakreel.platforms import channel_url
from peakreel.publish.queue import (
    Edit,
    QueueError,
    cancel_clip,
    process_due,
    queue_clip,
    save_drafts,
)

__all__ = [
    "AUDIT_SIZE",
    "REJECT_REASONS",
    "REVIEW_STATES",
    "ClipView",
    "RejectReason",
    "ReviewError",
    "block_channels",
    "get_clip",
    "list_clips",
    "suggest_title",
    "update_clip",
    "view_of",
]

log = get_logger(__name__)

_LANGUAGE = re.compile(r"^[a-z]{2,3}$")

RejectReason = Literal["boring", "no_context", "not_gta", "bad_video", "repeat", "other"]
REJECT_REASONS: tuple[RejectReason, ...] = get_args(RejectReason)
"""Why a person rejected a clip: nothing happens, an inside joke, not the game, a bad
crop or picture, a moment already posted, anything else. Teaches auto review."""

AUDIT_SIZE = 20
"""How many auto-rejected clips the ``audit`` list shows a person to check."""

REVIEW_STATES = frozenset({"new", "approved", "rejected"})
"""States a reviewer can set. ``blocked`` is set only by the takedown process."""

_SELECT = """
SELECT c.id, ch.name AS channel, ch.credit, c.state, c.start_ts, c.end_ts, c.path,
       c.sidecar_path, c.vertical_path, c.thumbnail_path, c.title, c.description,
       c.reviewed_at, c.export_path, c.created_at, c.flags, c.duplicate_of, c.match_strength,
       c.language, c.purged_at, c.auto, c.reviewed_by, c.review_reason,
       s.score, s.peak_count, s.unique_chatters, s.top_messages
FROM clips c
JOIN channels ch ON ch.id = c.channel_id
LEFT JOIN spikes s ON s.id = c.spike_id
"""


class ReviewError(ValueError):
    """Raised for an invalid review action; the message is shown to the reviewer."""


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class ClipView:
    """A clip as the review tool shows it.

    ``flags`` are the safety flags (``{"kind", "message"}``) for the reviewer;
    ``duplicate_of`` is set when the clip shows the same moment as a better clip,
    with the audio ``match_strength`` that matched them. ``auto`` is the automatic
    review decision (:mod:`peakreel.review.auto`), or ``None`` before one is made.
    ``reviewed_by`` says who set the state (``person``, ``auto`` or ``None``) and
    ``review_reason`` is a person's reject reason (:data:`REJECT_REASONS`).
    """

    id: int
    channel: str
    credit: str
    state: str
    start_utc: str | None
    duration_s: float
    score: float | None
    peak_count: int | None
    unique_chatters: int | None
    top_messages: list[list[Any]]
    title: str
    description: str
    suggested_title: str
    captions: dict[str, Any] | None
    has_vertical: bool
    has_thumbnail: bool
    reviewed_utc: str | None
    created_utc: str | None
    flags: list[dict[str, str]]
    duplicate_of: int | None
    match_strength: float | None
    publications: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    purged: bool = False
    auto: dict[str, Any] | None = None
    reviewed_by: str | None = None
    review_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


_UNSUITABLE = re.compile(
    r"https?://|www\.|\b[\w-]+\.(?:com|net|org|tv|gg|io|jp|be|ly|me|co)\b|@\w", re.IGNORECASE
)


def suggest_title(channel: str, top_messages: list[list[Any]]) -> str:
    """Suggest a clip title from the most repeated chat message.

    Chat is attacker-controlled, so messages with links or ``@mentions`` are
    skipped: a title is published, and must not advertise a chatter's link or
    name a private person.

    Parameters
    ----------
    channel : str
        Channel name, used as the title prefix (and as the whole title when no
        message is suitable).
    top_messages : list of [str, int]
        ``[text, count]`` pairs, most repeated first, as stored with the spike.

    Returns
    -------
    str
        At most 100 characters.

    Examples
    --------
    >>> suggest_title("gaules", [["https://x.com/spam", 9], ["GG", 5]])
    'GG'
    """
    del channel  # the channel's name is on the video, not in the post text
    for text, _count in top_messages:
        if text and not _UNSUITABLE.search(str(text)):
            return str(text)[:100]
    return "💀"


def view_of(row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> ClipView:
    """Build the review view of a row from :func:`get_clip`.

    With ``conn``, the view also lists the clip's publications (platform, state,
    suggested time, location, error).
    """
    view = _view(row)
    if conn is None:
        return view
    pubs = [
        {
            "account": r["account"],
            "platform": r["platform"],
            "state": r["state"],
            "scheduled_utc": _iso(r["scheduled_at"]),
            "url": r["url"],
            "error": r["error"],
            "views": r["views"],
        }
        for r in conn.execute(
            "SELECT account, platform, state, scheduled_at, url, error, "
            "(SELECT s.views FROM post_stats s WHERE s.publication_id = p.id "
            "ORDER BY s.checkpoint_h DESC LIMIT 1) AS views FROM publications p "
            "WHERE clip_id = ? AND state != 'cancelled' ORDER BY account, platform",
            (view.id,),
        )
    ]
    return replace(view, publications=pubs)


def _view(row: sqlite3.Row) -> ClipView:
    top = cast("list[list[Any]]", json.loads(row["top_messages"] or "[]"))
    captions: dict[str, Any] | None = None
    sidecar_path = row["sidecar_path"]
    if sidecar_path and Path(sidecar_path).exists():
        try:
            sidecar = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # One damaged sidecar must not take the whole review list down.
            log.warning("unreadable clip sidecar", clip_id=row["id"], path=sidecar_path)
            sidecar = {}
        if isinstance(sidecar, dict):
            captions = cast(
                "dict[str, Any] | None", cast("dict[str, Any]", sidecar).get("captions")
            )
    channel = str(row["channel"])
    return ClipView(
        id=int(row["id"]),
        channel=channel,
        credit=str(row["credit"] or f"Clip from {channel_url(channel)}"),
        state=str(row["state"]),
        start_utc=_iso(row["start_ts"]),
        duration_s=round(float(row["end_ts"]) - float(row["start_ts"]), 1),
        score=row["score"],
        peak_count=row["peak_count"],
        unique_chatters=row["unique_chatters"],
        top_messages=top,
        title=str(row["title"] or ""),
        description=str(row["description"] or ""),
        suggested_title=suggest_title(channel, top),
        captions=captions,
        has_vertical=bool(row["vertical_path"] and Path(row["vertical_path"]).exists()),
        has_thumbnail=bool(row["thumbnail_path"] and Path(row["thumbnail_path"]).exists()),
        reviewed_utc=_iso(row["reviewed_at"]),
        created_utc=_iso(row["created_at"]),
        flags=cast("list[dict[str, str]]", json.loads(row["flags"] or "[]")),
        duplicate_of=row["duplicate_of"],
        match_strength=row["match_strength"],
        purged=row["purged_at"] is not None,
        auto=json.loads(row["auto"]) if row["auto"] else None,
        reviewed_by=row["reviewed_by"],
        review_reason=row["review_reason"],
    )


ON_PLATFORM_STATES = frozenset({"processing", "published", "private_only"})
"""Publication states meaning the clip is on a platform (live, private or processing)."""


PROCESSING_GRACE_S = 3600.0  # 10 min was too short with 50 channels: Whisper queued 50+
"""After this long, an unfinished clip (no flags recorded) is reviewable anyway."""


# Auto review has decided a clip for real (shadow decisions are redone in on mode).
_DECIDED = "(c.auto IS NOT NULL AND json_extract(c.auto, '$.mode') != 'shadow')"


def list_clips(
    conn: sqlite3.Connection,
    state: str | None = None,
    limit: int = 200,
    *,
    ai_hold_s: float | None = None,
) -> list[ClipView]:
    """List clips, newest first.

    Parameters
    ----------
    conn : sqlite3.Connection
    state : str or None
        Filter by state; ``None`` lists every state. ``"new"`` leaves out
        duplicates, which ``"duplicate"`` lists (new clips that duplicate another);
        both leave out clips whose media was deleted by retention. ``"published"``
        lists clips on at least one platform (``ON_PLATFORM_STATES``), in any
        review state, purged media included, so their links stay reachable.
        ``"audit"`` lists a fixed sample of :data:`AUDIT_SIZE` clips auto review
        rejected that no person has looked at, to measure what it throws away.
        ``"checking"`` lists the new clips held back by ``ai_hold_s``.
    limit : int, default 200
    ai_hold_s : float, optional
        When auto review applies its decisions: ``"new"`` holds back a clip until
        auto review has decided it (left it for a person), or for this many
        seconds at most, so a stalled auto review never hides a clip for good.

    Returns
    -------
    list of ClipView
    """
    where = "WHERE c.state = ?" if state else ""
    args: list[object] = [state] if state else []
    if state == "new":
        # Only finished clips: render, captions, flags and the streamer's permission
        # land ~20 s after the row (flags stays NULL until then). Shown anyway after
        # PROCESSING_GRACE_S, so a clip from a crashed run is never hidden for good.
        where += (
            " AND c.duplicate_of IS NULL AND c.purged_at IS NULL"
            " AND (c.flags IS NOT NULL OR c.created_at < ?)"
        )
        args.append(time.time() - PROCESSING_GRACE_S)
        if ai_hold_s is not None:
            where += f" AND ({_DECIDED} OR c.created_at < ?)"
            args.append(time.time() - ai_hold_s)
    elif state == "checking":
        where = (
            "WHERE c.state = 'new' AND c.duplicate_of IS NULL AND c.purged_at IS NULL "
            f"AND NOT {_DECIDED} AND c.created_at >= ?"
        )
        args = [time.time() - (ai_hold_s or 0.0)]
    elif state == "duplicate":
        where = "WHERE c.state = 'new' AND c.duplicate_of IS NOT NULL AND c.purged_at IS NULL"
        args = []
    elif state == "published":
        where = (
            "WHERE EXISTS (SELECT 1 FROM publications p WHERE p.clip_id = c.id "
            "AND p.state IN ('processing', 'published', 'private_only'))"  # ON_PLATFORM_STATES
        )
        args = []
    order = "c.start_ts DESC"
    if state == "audit":
        where = (
            "WHERE c.state = 'rejected' AND c.reviewed_by = 'auto' AND c.purged_at IS NULL "
            "AND c.translation_of IS NULL"
        )
        args = []
        order = "(c.id * 2654435761) % 4294967296"  # a stable pseudo-random sample
        limit = min(limit, AUDIT_SIZE)
    rows = conn.execute(f"{_SELECT} {where} ORDER BY {order} LIMIT ?", [*args, limit])
    return [view_of(row, conn) for row in rows.fetchall()]


def get_clip(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row | None:
    """Return the raw row of one clip, or ``None``."""
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", [clip_id]).fetchone()
    return cast("sqlite3.Row | None", row)


def _require(row: sqlite3.Row | None, clip_id: int) -> sqlite3.Row:
    if row is None:
        raise ReviewError(f"clip {clip_id} disappeared during the update")
    return row


def block_channels(config: Config, conn: sqlite3.Connection, channels: Iterable[str]) -> int:
    """Mark every clip of the given channels as ``blocked`` and cancel their posts.

    This is the takedown step for clips that already exist: a blocked clip can no
    longer be approved or edited. The clip files themselves are kept for the record;
    every queued or exported publication package is removed. Posts already on a
    platform are logged for removal by hand.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
        Open database connection; the change is committed.
    channels : iterable of str
        Channel names (lowercase, without ``#``).

    Returns
    -------
    int
        Number of clips newly blocked.
    """
    names = sorted(set(channels))
    if not names:
        return 0
    marks = ", ".join("?" * len(names))
    rows = conn.execute(
        f"{_SELECT} WHERE ch.name IN ({marks}) AND c.state != 'blocked'",
        names,
    ).fetchall()
    for row in rows:
        cancel_clip(config, conn, _view(row), row)
        conn.execute("UPDATE clips SET state = 'blocked' WHERE id = ?", (row["id"],))
    conn.commit()
    if rows:
        log.warning("takedown: clips blocked", channels=names, clips=len(rows))
    return len(rows)


def _awaits_gemini_text(config: Config, view: ClipView) -> bool:
    """Whether Gemini could still write this clip's empty title or description."""
    settings = config.review.auto
    if view.title and view.description:
        return False
    if (view.auto or {}).get("gemini") or settings.mode == "off" or not settings.gemini_models:
        return False
    from peakreel.review.gemini import load_key  # the store does not need Gemini otherwise

    return bool(load_key())


def update_clip(
    config: Config,
    conn: sqlite3.Connection,
    clip_id: int,
    *,
    state: str | None = None,
    title: str | None = None,
    description: str | None = None,
    platforms: Sequence[Platform] | None = None,
    posts: Mapping[Platform, Edit] | None = None,
    language: str | None = None,
    reason: str | None = None,
    by: Literal["person", "auto"] = "person",
) -> ClipView:
    """Apply a review action and keep the publication queue in step.

    Approving queues the clip for ``platforms`` (:mod:`peakreel.publish.queue`); in
    ``export`` mode their packages are written at once. Moving away from ``approved``
    cancels publications that are not on a platform yet. Saving without a state
    change stores edited post text as drafts.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    clip_id : int
    state : {"new", "approved", "rejected"}, optional
    title, description : str, optional
    platforms : sequence of str, optional
        Platforms to publish to when approving; defaults to ``publish.platforms``.
        An empty sequence approves without publishing.
    posts : mapping, optional
        The reviewer's edited post text per platform.
    language : str, optional
        The clip's language (ISO 639-1), which picks the posting account; an
        empty string clears the choice (back to the channel's language).
    reason : str, optional
        Why the clip is rejected, one of :data:`REJECT_REASONS`; kept only with
        ``state="rejected"`` (any other state change clears it).
    by : {"person", "auto"}, default "person"
        Who decides, stored with a state change so auto review can be measured
        against people's decisions.

    Returns
    -------
    ClipView

    Raises
    ------
    ReviewError
        Unknown clip, invalid state, a blocked clip (takedown) being changed, an
        approval while the blocklist cannot be read, or post text that fails a
        platform's limits or the credit rule (nothing is changed then).

    Notes
    -----
    The blocklist is re-read on every call, so a takedown applies here even when
    no pipeline is running: a clip of a listed channel is blocked instead of
    changed. If the blocklist is unreadable, approving fails closed.
    """
    row = get_clip(conn, clip_id)
    if row is None:
        raise ReviewError(f"no clip {clip_id}")
    if state is not None and state not in REVIEW_STATES:
        raise ReviewError(f"invalid state {state!r}")
    new_state = state or str(row["state"])
    try:
        blocked = load_blocklist(config.blocklist_path)
    except ConfigError as exc:
        if new_state == "approved":
            raise ReviewError(f"blocklist unreadable, approvals refused: {exc}") from exc
        blocked = frozenset[str]()
    if row["channel"] in blocked:
        block_channels(config, conn, [row["channel"]])
    if row["state"] == "blocked" or row["channel"] in blocked:
        raise ReviewError("clip is blocked by a takedown and cannot be changed")
    if (
        new_state == "approved"
        and row["flags"] is None
        and row["created_at"] > time.time() - PROCESSING_GRACE_S
    ):
        raise ReviewError("clip is still being processed (render, flags); try again in a minute")
    if language is not None and language and not _LANGUAGE.match(language):
        raise ReviewError(f"not a language code: {language!r}")
    if reason is not None and reason not in REJECT_REASONS:
        raise ReviewError(f"unknown reject reason {reason!r}")
    conn.execute(
        "UPDATE clips SET state = ?, title = coalesce(?, title), "
        "description = coalesce(?, description), reviewed_at = ? WHERE id = ?",
        (new_state, title, description, time.time(), clip_id),
    )
    if language is not None:
        conn.execute("UPDATE clips SET language = ? WHERE id = ?", (language or None, clip_id))
    if state is not None:
        conn.execute(
            "UPDATE clips SET reviewed_by = ?, review_reason = ? WHERE id = ?",
            (by, reason if new_state == "rejected" else None, clip_id),
        )
    refreshed = _require(get_clip(conn, clip_id), clip_id)
    view = _view(refreshed)
    if new_state == "approved":
        chosen = config.publish.platforms if platforms is None else platforms
        try:
            queue_clip(config, conn, view, refreshed, chosen, posts)
        except QueueError as exc:
            conn.rollback()
            raise ReviewError(str(exc)) from exc
        if by == "person" and _awaits_gemini_text(config, view):
            # Post text is fixed at queueing: give Gemini a moment to write the title and
            # description first (peakreel.review.auto writes them, then releases the posts).
            hold = time.time() + config.review.auto.text_wait_min * 60
            conn.execute(
                "UPDATE publications SET next_attempt_at = max(next_attempt_at, ?) "
                "WHERE clip_id = ? AND state = 'approved'",
                (hold, clip_id),
            )
    else:
        if posts:
            save_drafts(config, conn, view, refreshed, posts)
        cancel_clip(config, conn, view, refreshed)
    conn.commit()
    if new_state == "approved":
        process_due(config, conn, export_only=True)  # API posts: the background loop
    final = _require(get_clip(conn, clip_id), clip_id)
    log.info(
        "clip reviewed", clip_id=clip_id, state=new_state, by=by, reason=reason
    )  # fmt: skip
    return view_of(final, conn)
