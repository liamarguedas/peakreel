"""Clip queries and updates for the review tool, plus export of approved clips."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from peakreel.blocklist import load_blocklist
from peakreel.config import Config, ConfigError
from peakreel.log import get_logger

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
]

log = get_logger(__name__)

REVIEW_STATES = frozenset({"new", "approved", "rejected"})
"""States a reviewer can set. ``blocked`` is set only by the takedown process."""

_SELECT = """
SELECT c.id, ch.name AS channel, ch.credit, c.state, c.start_ts, c.end_ts, c.path,
       c.sidecar_path, c.vertical_path, c.thumbnail_path, c.title, c.description,
       c.reviewed_at, c.export_path, c.created_at, c.flags, c.duplicate_of, c.similarity,
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
    with the audio ``similarity`` that matched them.
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
    similarity: float | None

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


def _view(row: sqlite3.Row) -> ClipView:
    top = cast("list[list[Any]]", json.loads(row["top_messages"] or "[]"))
    captions: dict[str, Any] | None = None
    sidecar_path = row["sidecar_path"]
    if sidecar_path and Path(sidecar_path).exists():
        sidecar = cast("dict[str, Any]", json.loads(Path(sidecar_path).read_text(encoding="utf-8")))
        captions = cast("dict[str, Any] | None", sidecar.get("captions"))
    channel = str(row["channel"])
    return ClipView(
        id=int(row["id"]),
        channel=channel,
        credit=str(row["credit"] or f"Clip from twitch.tv/{channel}"),
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
        similarity=row["similarity"],
    )


def list_clips(
    conn: sqlite3.Connection, state: str | None = None, limit: int = 200
) -> list[ClipView]:
    """List clips, newest first.

    Parameters
    ----------
    conn : sqlite3.Connection
    state : str or None
        Filter by state; ``None`` lists every state. ``"new"`` leaves out
        duplicates, which ``"duplicate"`` lists (new clips that duplicate another).
    limit : int, default 200

    Returns
    -------
    list of ClipView
    """
    where, args = ("WHERE c.state = ?", [state]) if state else ("", [])
    if state == "new":
        where += " AND c.duplicate_of IS NULL"
    elif state == "duplicate":
        where, args = ("WHERE c.state = 'new' AND c.duplicate_of IS NOT NULL", [])
    rows = conn.execute(f"{_SELECT} {where} ORDER BY c.start_ts DESC LIMIT ?", [*args, limit])
    return [_view(row) for row in rows.fetchall()]


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
        view.credit if view.channel in view.credit else f"{view.credit} · twitch.tv/{view.channel}",
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
        "source": f"https://www.twitch.tv/{view.channel}",
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
    the record; only the export (the publishable copy) is removed.

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
) -> ClipView:
    """Apply a review action and keep the export folder in step.

    Approving exports the clip (again, if metadata changed); moving away from
    ``approved`` removes the export.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    clip_id : int
    state : {"new", "approved", "rejected"}, optional
    title, description : str, optional

    Returns
    -------
    ClipView

    Raises
    ------
    ReviewError
        Unknown clip, invalid state, a blocked clip (takedown) being changed, or
        an approval while the blocklist cannot be read.

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
    conn.execute(
        "UPDATE clips SET state = ?, title = coalesce(?, title), "
        "description = coalesce(?, description), reviewed_at = ? WHERE id = ?",
        (new_state, title, description, time.time(), clip_id),
    )
    refreshed = _require(get_clip(conn, clip_id), clip_id)
    export_path: str | None = refreshed["export_path"]
    if new_state == "approved":
        export_path = str(export_clip(config, refreshed))
    elif export_path:
        _remove_export(config, export_path)
        export_path = None
    conn.execute("UPDATE clips SET export_path = ? WHERE id = ?", (export_path, clip_id))
    conn.commit()
    final = _require(get_clip(conn, clip_id), clip_id)
    log.info("clip reviewed", clip_id=clip_id, state=new_state, exported=export_path)
    return _view(final)
