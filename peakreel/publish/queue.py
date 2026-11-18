"""The publication queue: one row per clip and platform, processed idempotently.

States::

    pending ──approve──> approved ──process──> uploading ──> exported | published
       ▲                    │  ▲                   │            | processing | private_only
       └──── save draft     │  └── retry (backoff) ┘
                            └──> cancelled (un-approved, taken down)      failed (max attempts)

Guarantees:

- ``UNIQUE(clip_id, platform)``: a clip has at most one publication per platform.
- A row is marked ``uploading`` and committed *before* the publisher is called.
  If the process dies there, the next run finds it ``uploading`` and asks the
  publisher what happened (:meth:`Publisher.poll_status`) instead of sending it
  again. For a network publisher an unknown outcome becomes ``failed`` with a
  "check the platform" error, never a second upload. Exporting is local and
  safe to repeat, so an interrupted export is simply written again.
- Rows already ``uploading``, ``processing``, ``published`` or ``private_only``
  are never re-queued.
- Failures retry with exponential backoff (``publish.retry_initial_s`` doubling)
  up to ``publish.max_attempts``.

Scheduling: each platform gets a suggested time that keeps
``min_spacing_min`` between posts and at most ``daily_cap`` per UTC day. In
``export`` mode the package is written at once and the time goes into its
checklist; ``private`` and ``public`` modes (M10 to M12) wait for it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from peakreel.config import PLATFORMS, ChannelConfig, Config, Platform
from peakreel.log import get_logger
from peakreel.publish.metadata import (
    ClipFacts,
    PostMetadata,
    build_metadata,
    validate,
    with_edits,
)
from peakreel.publish.publishers import ClipPackage, ExportPublisher, Publisher, PublishError

if TYPE_CHECKING:
    from peakreel.review.store import ClipView

__all__ = [
    "LOCKED_STATES",
    "Edit",
    "QueueError",
    "cancel_clip",
    "clip_package",
    "default_publishers",
    "posts_preview",
    "process_due",
    "queue_clip",
    "save_drafts",
]

log = get_logger(__name__)

LOCKED_STATES = frozenset({"uploading", "processing", "published", "private_only"})
"""States in which a publication is (or may be) on the platform: never re-queued."""


class QueueError(ValueError):
    """Raised when a clip cannot be queued; the message is shown to the reviewer."""


@dataclass(frozen=True, slots=True)
class Edit:
    """The reviewer's edited text for one platform (``None`` keeps the generated part)."""

    title: str | None = None
    text: str | None = None


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def default_publishers(config: Config) -> dict[Platform, Publisher]:
    """Return the publisher of every platform for the configured mode."""
    return {p: ExportPublisher(p, config.publish_dir) for p in PLATFORMS}


def _sidecar(row: sqlite3.Row) -> dict[str, Any]:
    path = row["sidecar_path"]
    if path and Path(path).exists():
        return cast("dict[str, Any]", json.loads(Path(path).read_text(encoding="utf-8")))
    return {}


def _channel(config: Config, name: str) -> ChannelConfig:
    return config.channel(name) or ChannelConfig(name=name)


def clip_facts(config: Config, view: ClipView, row: sqlite3.Row) -> ClipFacts:
    """Platform-independent facts of a clip, for :func:`build_metadata`."""
    from peakreel.review.store import suggest_title  # imported here: store imports this module

    stream: dict[str, Any] = _sidecar(row).get("stream") or {}
    channel = _channel(config, view.channel)
    return ClipFacts(
        channel=channel,
        # Without a reviewer title, posts name the streamer as they present themselves.
        title=view.title or suggest_title(channel.display, view.top_messages),
        description=view.description,
        game=stream.get("game") or None,
    )


def clip_package(
    config: Config, view: ClipView, row: sqlite3.Row, scheduled_at: float | None = None
) -> ClipPackage:
    """Build what a publisher needs to post ``view``."""
    vertical = Path(row["vertical_path"]) if row["vertical_path"] else None
    video = vertical if vertical is not None and vertical.exists() else Path(row["path"])
    thumbnail = Path(row["thumbnail_path"]) if row["thumbnail_path"] else None
    return ClipPackage(
        clip_id=view.id,
        name=Path(row["path"]).stem,
        video=video,
        thumbnail=thumbnail,
        channel=_channel(config, view.channel),
        start_utc=view.start_utc,
        flags=view.flags,
        scheduled_utc=_iso(scheduled_at),
    )


def _publication(conn: sqlite3.Connection, clip_id: int, platform: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM publications WHERE clip_id = ? AND platform = ?", (clip_id, platform)
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def _generated(config: Config, facts: ClipFacts, platform: Platform) -> PostMetadata:
    return build_metadata(facts, platform, config.publish, config.publish.templates.get(platform))


def posts_preview(
    config: Config, conn: sqlite3.Connection, view: ClipView, row: sqlite3.Row
) -> dict[str, dict[str, Any]]:
    """Post text, problems, limits and publication state per platform, for the review tool.

    A saved or queued publication shows its stored text (which may be edited);
    otherwise the text is generated from the clip's current title and description.
    """
    facts = clip_facts(config, view, row)
    preview: dict[str, dict[str, Any]] = {}
    for platform in PLATFORMS:
        limits = config.publish.limits(platform)
        pub = _publication(conn, view.id, platform)
        stored = pub is not None and pub["state"] != "cancelled"
        meta = (
            PostMetadata.from_json(json.loads(pub["metadata"]))
            if stored and pub is not None
            else _generated(config, facts, platform)
        )
        preview[platform] = {
            "meta": meta.to_json(),
            "problems": validate(meta, limits),
            "limits": {
                "title_max": limits.title_max,
                "text_max": limits.text_max,
                "text_unit": limits.text_unit,
            },
            "enabled": platform in config.publish.platforms,
            "publication": None
            if pub is None
            else {
                "state": pub["state"],
                "mode": pub["mode"],
                "scheduled_utc": _iso(pub["scheduled_at"]),
                "url": pub["url"],
                "error": pub["error"],
                "attempts": pub["attempts"],
            },
        }
    return preview


def _schedule(conn: sqlite3.Connection, config: Config, platform: Platform, now: float) -> float:
    limits = config.publish.limits(platform)
    taken = sorted(
        float(r[0])
        for r in conn.execute(
            "SELECT scheduled_at FROM publications WHERE platform = ? AND scheduled_at IS NOT NULL "
            "AND state != 'cancelled' AND state != 'failed'",
            (platform,),
        )
    )
    spacing = limits.min_spacing_min * 60
    slot = now
    while True:
        if any(abs(slot - t) < spacing for t in taken):
            slot = max(t for t in taken if abs(slot - t) < spacing) + spacing
            continue
        day = datetime.fromtimestamp(slot, UTC).date()
        same_day = [t for t in taken if datetime.fromtimestamp(t, UTC).date() == day]
        if len(same_day) >= limits.daily_cap:
            next_day = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
            slot = next_day.timestamp()
            continue
        return slot


def _meta_for(
    config: Config, facts: ClipFacts, platform: Platform, edit: Edit | None
) -> PostMetadata:
    meta = _generated(config, facts, platform)
    if edit is not None and (edit.title is not None or edit.text is not None):
        meta = with_edits(meta, edit.title, edit.text)
    return meta


def _upsert(
    conn: sqlite3.Connection,
    clip_id: int,
    platform: Platform,
    *,
    mode: str,
    state: str,
    meta: PostMetadata,
    now: float,
    scheduled_at: float | None,
    next_attempt_at: float | None,
) -> None:
    conn.execute(
        """
        INSERT INTO publications (clip_id, platform, mode, state, metadata, attempts,
                                  scheduled_at, next_attempt_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT (clip_id, platform) DO UPDATE SET
            mode = excluded.mode, state = excluded.state, metadata = excluded.metadata,
            attempts = 0, error = NULL, scheduled_at = excluded.scheduled_at,
            next_attempt_at = excluded.next_attempt_at, updated_at = excluded.updated_at
        """,
        (clip_id, platform, mode, state, json.dumps(meta.to_json(), ensure_ascii=False),
         scheduled_at, next_attempt_at, now, now),
    )  # fmt: skip


def save_drafts(
    config: Config,
    conn: sqlite3.Connection,
    view: ClipView,
    row: sqlite3.Row,
    edits: Mapping[Platform, Edit],
    now: float | None = None,
) -> None:
    """Store edited post text as ``pending`` publications, without queueing them."""
    now = time.time() if now is None else now
    facts = clip_facts(config, view, row)
    for platform, edit in edits.items():
        pub = _publication(conn, view.id, platform)
        if pub is not None and pub["state"] in LOCKED_STATES:
            continue
        meta = _meta_for(config, facts, platform, edit)
        state = (
            "pending" if pub is None or pub["state"] in ("cancelled", "failed") else pub["state"]
        )
        _upsert(conn, view.id, platform, mode=config.publish.mode, state=state, meta=meta,
                now=now, scheduled_at=pub["scheduled_at"] if pub else None,
                next_attempt_at=pub["next_attempt_at"] if pub else None)  # fmt: skip
    conn.commit()


def queue_clip(
    config: Config,
    conn: sqlite3.Connection,
    view: ClipView,
    row: sqlite3.Row,
    platforms: Sequence[Platform],
    edits: Mapping[Platform, Edit] | None = None,
    now: float | None = None,
) -> list[str]:
    """Queue an approved clip for ``platforms`` (all-or-nothing).

    Text is generated from the clip's current title and description, except for
    platforms the reviewer edited (``edits``) or whose stored text was edited
    earlier. Every platform is validated before anything is written.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
        Committed on success.
    view : ClipView
    row : sqlite3.Row
        The clip's row from :func:`peakreel.review.store.get_clip`.
    platforms : sequence of str
    edits : mapping, optional
    now : float, optional

    Returns
    -------
    list of str
        Notes for the reviewer (platforms already posted and left alone).

    Raises
    ------
    QueueError
        If any platform's text fails validation; nothing is queued then.
    """
    now = time.time() if now is None else now
    edits = edits or {}
    facts = clip_facts(config, view, row)
    planned: list[tuple[Platform, PostMetadata]] = []
    problems: list[str] = []
    notes: list[str] = []
    for platform in platforms:
        pub = _publication(conn, view.id, platform)
        if pub is not None and pub["state"] in LOCKED_STATES:
            notes.append(f"{platform}: already {pub['state']}, left as is")
            continue
        edit = edits.get(platform)
        if edit is None and pub is not None and pub["state"] != "cancelled":
            stored = PostMetadata.from_json(json.loads(pub["metadata"]))
            if "edited by reviewer" in stored.notes:
                edit = Edit(stored.title, stored.text)
        meta = _meta_for(config, facts, platform, edit)
        problems += [f"{platform}: {p}" for p in validate(meta, config.publish.limits(platform))]
        planned.append((platform, meta))
    if problems:
        raise QueueError("; ".join(problems))
    for platform, meta in planned:
        pub = _publication(conn, view.id, platform)
        keep_slot = pub is not None and pub["state"] in ("approved", "exported", "pending")
        scheduled = (
            pub["scheduled_at"]
            if keep_slot and pub is not None and pub["scheduled_at"]
            else _schedule(conn, config, platform, now)
        )
        mode = config.publish.mode
        due = now if mode == "export" else scheduled
        _upsert(conn, view.id, platform, mode=mode, state="approved", meta=meta, now=now,
                scheduled_at=scheduled, next_attempt_at=due)  # fmt: skip
        log.info("publication queued", clip_id=view.id, platform=platform, mode=mode)
    conn.commit()
    return notes


def cancel_clip(
    config: Config,
    conn: sqlite3.Connection,
    view: ClipView,
    row: sqlite3.Row,
    publishers: Mapping[Platform, Publisher] | None = None,
    now: float | None = None,
) -> list[str]:
    """Cancel every publication of a clip that is not on a platform yet.

    Exported packages are deleted. Posts that may already be on a platform
    (``LOCKED_STATES``) are left, and reported so they can be removed by hand.

    Returns
    -------
    list of str
        Platforms where the clip may still be posted.
    """
    now = time.time() if now is None else now
    publishers = publishers or default_publishers(config)
    package = clip_package(config, view, row)
    remaining: list[str] = []
    for pub in conn.execute("SELECT * FROM publications WHERE clip_id = ?", (view.id,)).fetchall():
        platform = cast("Platform", pub["platform"])
        if pub["state"] in LOCKED_STATES:
            remaining.append(f"{platform}: {pub['state']} {pub['url'] or ''}".strip())
            continue
        if pub["state"] == "exported":
            publishers[platform].remove(package, pub["url"])
        conn.execute(
            "UPDATE publications SET state = 'cancelled', updated_at = ? WHERE id = ?",
            (now, pub["id"]),
        )
    conn.commit()
    if remaining:
        log.warning("clip may still be posted; remove it by hand", clip_id=view.id, posts=remaining)
    return remaining


@dataclass(frozen=True, slots=True)
class Processed:
    """What :func:`process_due` did with one publication."""

    clip_id: int
    platform: str
    state: str
    detail: str = ""


def process_due(
    config: Config,
    conn: sqlite3.Connection,
    publishers: Mapping[Platform, Publisher] | None = None,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> list[Processed]:
    """Publish every due publication, and resolve ones interrupted by a crash.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    publishers : mapping, optional
        Defaults to :func:`default_publishers`.
    now : float, optional
    dry_run : bool, default False
        Validate and report what would happen; change nothing and call no publisher.

    Returns
    -------
    list of Processed
    """
    from peakreel.review.store import get_clip, view_of

    now = time.time() if now is None else now
    publishers = publishers or default_publishers(config)
    done: list[Processed] = []
    rows = conn.execute(
        "SELECT * FROM publications WHERE state = 'uploading' "
        "OR (state = 'approved' AND next_attempt_at <= ?) ORDER BY scheduled_at, id",
        (now,),
    ).fetchall()
    for pub in rows:
        platform = cast("Platform", pub["platform"])
        clip_row = get_clip(conn, int(pub["clip_id"]))
        if clip_row is None or clip_row["state"] == "blocked":
            if not dry_run:
                conn.execute(
                    "UPDATE publications SET state = 'cancelled', updated_at = ? WHERE id = ?",
                    (now, pub["id"]),
                )
                conn.commit()
            done.append(Processed(int(pub["clip_id"]), platform, "cancelled", "clip blocked"))
            continue
        view = view_of(clip_row)
        package = clip_package(config, view, clip_row, pub["scheduled_at"])
        publisher = publishers[platform]
        meta = PostMetadata.from_json(json.loads(pub["metadata"]))
        if pub["state"] == "uploading":
            # Interrupted while publishing: ask, never blindly send again.
            status = publisher.poll_status(package, pub["remote_id"])
            if status is not None:
                if not dry_run:
                    _finish(conn, pub["id"], status.state, status.remote_id, status.url, now)
                done.append(Processed(view.id, platform, status.state, "recovered"))
                continue
            if pub["mode"] != "export":
                error = "interrupted during upload; check the platform before retrying"
                if not dry_run:
                    _fail(conn, pub["id"], error, now, final=True)
                done.append(Processed(view.id, platform, "failed", error))
                continue
        problems = validate(meta, config.publish.limits(platform))
        problems += publisher.validate(package, meta)
        if problems:
            if not dry_run:
                _fail(conn, pub["id"], "; ".join(problems), now, final=True)
            done.append(Processed(view.id, platform, "failed", "; ".join(problems)))
            continue
        if dry_run:
            done.append(Processed(view.id, platform, "approved", f"would {pub['mode']}"))
            continue
        conn.execute(
            "UPDATE publications SET state = 'uploading', attempts = attempts + 1, "
            "updated_at = ? WHERE id = ?",
            (now, pub["id"]),
        )
        conn.commit()
        try:
            result = publisher.publish(package, meta, pub["mode"])
        except (PublishError, OSError) as exc:
            attempts = int(pub["attempts"]) + 1
            final = attempts >= config.publish.max_attempts
            _fail(conn, pub["id"], str(exc), now, final=final, attempts=attempts,
                  retry_s=config.publish.retry_initial_s)  # fmt: skip
            log.warning("publish failed", clip_id=view.id, platform=platform, error=str(exc))
            done.append(Processed(view.id, platform, "failed" if final else "approved", str(exc)))
            continue
        _finish(conn, pub["id"], result.state, result.remote_id, result.url, now)
        log.info(
            "published", clip_id=view.id, platform=platform, state=result.state, url=result.url
        )
        done.append(Processed(view.id, platform, result.state, result.url or ""))
    return done


def _finish(
    conn: sqlite3.Connection,
    pub_id: int,
    state: str,
    remote_id: str | None,
    url: str | None,
    now: float,
) -> None:
    conn.execute(
        "UPDATE publications SET state = ?, remote_id = ?, url = ?, error = NULL, "
        "updated_at = ? WHERE id = ?",
        (state, remote_id, url, now, pub_id),
    )
    conn.commit()


def _fail(
    conn: sqlite3.Connection,
    pub_id: int,
    error: str,
    now: float,
    *,
    final: bool,
    attempts: int = 0,
    retry_s: float = 60.0,
) -> None:
    if final:
        conn.execute(
            "UPDATE publications SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error, now, pub_id),
        )
    else:
        retry_at = now + retry_s * 2 ** max(0, attempts - 1)
        conn.execute(
            "UPDATE publications SET state = 'approved', error = ?, next_attempt_at = ?, "
            "updated_at = ? WHERE id = ?",
            (error, retry_at, now, pub_id),
        )
    conn.commit()
