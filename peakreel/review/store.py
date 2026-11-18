"""Clip queries and updates for the review tool, plus export of approved clips."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from peakreel.blocklist import load_blocklist
from peakreel.config import Config, ConfigError, Platform
from peakreel.log import get_logger
from peakreel.platforms import channel_link, channel_name, channel_url
from peakreel.publish.queue import (
    Edit,
    QueueError,
    cancel_clip,
    process_due,
    queue_clip,
    save_drafts,
)

__all__ = [
    "REVIEW_STATES",
    "ClipView",
    "ReviewError",
    "block_channels",
    "export_clip",
    "get_clip",
    "list_clips",
    "suggest_title",
    "update_clip",
    "view_of",
]

log = get_logger(__name__)

_LANGUAGE = re.compile(r"^[a-z]{2,3}$")

REVIEW_STATES = frozenset({"new", "approved", "rejected"})
"""States a reviewer can set. ``blocked`` is set only by the takedown process."""

_SELECT = """
SELECT c.id, ch.name AS channel, ch.credit, c.state, c.start_ts, c.end_ts, c.path,
       c.sidecar_path, c.vertical_path, c.thumbnail_path, c.title, c.description,
       c.reviewed_at, c.export_path, c.created_at, c.flags, c.duplicate_of, c.match_strength,
       c.language, c.purged_at, c.auto,
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
    exported: bool
    reviewed_utc: str | None
    created_utc: str | None
    flags: list[dict[str, str]]
    duplicate_of: int | None
    match_strength: float | None
    publications: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    purged: bool = False
    auto: dict[str, Any] | None = None

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
    'gaules: GG'
    """
    for text, _count in top_messages:
        if text and not _UNSUITABLE.search(str(text)):
            return f"{channel}: {text}"[:100]
    return channel


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
        }
        for r in conn.execute(
            "SELECT account, platform, state, scheduled_at, url, error FROM publications "
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
        exported=bool(row["export_path"] and Path(row["export_path"]).exists()),
        reviewed_utc=_iso(row["reviewed_at"]),
        created_utc=_iso(row["created_at"]),
        flags=cast("list[dict[str, str]]", json.loads(row["flags"] or "[]")),
        duplicate_of=row["duplicate_of"],
        match_strength=row["match_strength"],
        purged=row["purged_at"] is not None,
        auto=json.loads(row["auto"]) if row["auto"] else None,
    )


ON_PLATFORM_STATES = frozenset({"processing", "published", "private_only"})
"""Publication states meaning the clip is on a platform (live, private or processing)."""


PROCESSING_GRACE_S = 600.0
"""After this long, an unfinished clip (no flags recorded) is reviewable anyway."""


def list_clips(
    conn: sqlite3.Connection, state: str | None = None, limit: int = 200
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
    limit : int, default 200

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
    elif state == "duplicate":
        where = "WHERE c.state = 'new' AND c.duplicate_of IS NOT NULL AND c.purged_at IS NULL"
        args = []
    elif state == "published":
        where = (
            "WHERE EXISTS (SELECT 1 FROM publications p WHERE p.clip_id = c.id "
            "AND p.state IN ('processing', 'published', 'private_only'))"  # ON_PLATFORM_STATES
        )
        args = []
    rows = conn.execute(f"{_SELECT} {where} ORDER BY c.start_ts DESC LIMIT ?", [*args, limit])
    return [view_of(row, conn) for row in rows.fetchall()]


def get_clip(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row | None:
    """Return the raw row of one clip, or ``None``."""
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", [clip_id]).fetchone()
    return cast("sqlite3.Row | None", row)


def _caption_text(view: ClipView) -> str:
    lines = [view.title or view.suggested_title]
    if view.description:
        lines += ["", view.description]
    lines += [
        "",
        view.credit
        if channel_name(view.channel) in view.credit
        else f"{view.credit} · {channel_url(view.channel)}",
    ]
    return "\n".join(lines) + "\n"


def export_clip(config: Config, row: sqlite3.Row) -> Path:
    """Copy an approved clip and its metadata into the export folder.

    Layout::

        <export>/<YYYY-MM-DD>/<clip name>/clip.mp4        vertical render (or horizontal)
                                          thumbnail.jpg
                                          captions.ass     when captioned
                                          metadata.json
                                          caption.txt      title, description, credit

    Parameters
    ----------
    config : Config
    row : sqlite3.Row
        From :func:`get_clip`.

    Returns
    -------
    pathlib.Path
        The export directory. Written to a temporary name and renamed, so a partial
        export is never visible.

    Raises
    ------
    ReviewError
        If the clip file is missing.
    """
    view = _view(row)
    horizontal = Path(row["path"])
    vertical = Path(row["vertical_path"]) if row["vertical_path"] else None
    source = vertical if vertical is not None and vertical.exists() else horizontal
    if not source.exists():
        raise ReviewError(f"clip file missing: {source}")
    day = (view.start_utc or "unknown")[:10]
    target = config.export_dir / day / horizontal.stem
    temp = target.with_name(target.name + ".part")
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    shutil.copyfile(source, temp / "clip.mp4")
    if row["thumbnail_path"] and Path(row["thumbnail_path"]).exists():
        shutil.copyfile(row["thumbnail_path"], temp / "thumbnail.jpg")
    ass = horizontal.with_suffix(".ass")
    if ass.exists():
        shutil.copyfile(ass, temp / "captions.ass")
    metadata = {
        "title": view.title or view.suggested_title,
        "description": view.description,
        "channel": view.channel,
        "credit": view.credit,
        "source": channel_link(view.channel),
        "clip_start_utc": view.start_utc,
        "duration_s": view.duration_s,
        "vertical": source == vertical,
        "score": view.score,
        "top_messages": view.top_messages,
        "captions": view.captions,
        "flags": view.flags,
        "approved_utc": _iso(time.time()),
    }
    (temp / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (temp / "caption.txt").write_text(_caption_text(view), encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    temp.replace(target)
    return target


def _remove_export(config: Config, path: str | None) -> None:
    if not path:
        return
    target = Path(path).resolve()
    root = config.export_dir.resolve()
    if root in target.parents and target.exists():
        shutil.rmtree(target)


def _require(row: sqlite3.Row | None, clip_id: int) -> sqlite3.Row:
    if row is None:
        raise ReviewError(f"clip {clip_id} disappeared during the update")
    return row


def block_channels(config: Config, conn: sqlite3.Connection, channels: Iterable[str]) -> int:
    """Mark every clip of the given channels as ``blocked`` and delete their exports.

    This is the takedown step for clips that already exist: a blocked clip can no
    longer be approved, edited or exported. The clip files themselves are kept for
    the record; the export and every queued or exported publication package are
    removed. Posts already on a platform are logged for removal by hand.

    Parameters
    ----------
    config : Config
        Supplies the export directory.
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
        _remove_export(config, row["export_path"])
        cancel_clip(config, conn, _view(row), row)
        conn.execute(
            "UPDATE clips SET state = 'blocked', export_path = NULL WHERE id = ?", (row["id"],)
        )
    conn.commit()
    if rows:
        log.warning("takedown: clips blocked", channels=names, clips=len(rows))
    return len(rows)


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
) -> ClipView:
    """Apply a review action and keep the export and the publication queue in step.

    Approving exports the clip (again, if metadata changed) and queues it for
    ``platforms`` (:mod:`peakreel.publish.queue`); in ``export`` mode their
    packages are written at once. Moving away from ``approved`` removes the export
    and cancels publications that are not on a platform yet. Saving without a state
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
    conn.execute(
        "UPDATE clips SET state = ?, title = coalesce(?, title), "
        "description = coalesce(?, description), reviewed_at = ? WHERE id = ?",
        (new_state, title, description, time.time(), clip_id),
    )
    if language is not None:
        conn.execute("UPDATE clips SET language = ? WHERE id = ?", (language or None, clip_id))
    refreshed = _require(get_clip(conn, clip_id), clip_id)
    view = _view(refreshed)
    export_path: str | None = refreshed["export_path"]
    if new_state == "approved":
        chosen = config.publish.platforms if platforms is None else platforms
        try:
            queue_clip(config, conn, view, refreshed, chosen, posts)
        except QueueError as exc:
            conn.rollback()
            raise ReviewError(str(exc)) from exc
        export_path = str(export_clip(config, refreshed))
    else:
        if posts:
            save_drafts(config, conn, view, refreshed, posts)
        cancel_clip(config, conn, view, refreshed)
        if export_path:
            _remove_export(config, export_path)
            export_path = None
    conn.execute("UPDATE clips SET export_path = ? WHERE id = ?", (export_path, clip_id))
    conn.commit()
    if new_state == "approved":
        process_due(config, conn, export_only=True)  # API posts: the background loop
    final = _require(get_clip(conn, clip_id), clip_id)
    log.info("clip reviewed", clip_id=clip_id, state=new_state, exported=export_path)
    return view_of(final, conn)
