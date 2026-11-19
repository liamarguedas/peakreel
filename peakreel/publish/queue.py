"""The publication queue: one row per clip and platform, processed idempotently.

States::

    pending ──approve──> approved ──process──> uploading ──> exported | published
       ▲                    │  ▲                   │            | processing | private_only
       └──── save draft     │  └── retry (backoff) ┘
                            └──> cancelled (un-approved, taken down)      failed (max attempts)

Guarantees:

- ``UNIQUE(clip_id, account, platform)``: at most one publication per account and platform.
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

Routing: a clip goes to the posting account for its language (see
:func:`clip_route`); a clip whose language has no account is held for the
reviewer. Publications are per clip, account and platform.

Scheduling: each account and platform gets a suggested time that keeps
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

from peakreel.config import (
    PLATFORMS,
    PRIVATE_PLATFORMS,
    ChannelConfig,
    Config,
    Platform,
    PublishAccount,
)
from peakreel.log import get_logger
from peakreel.platforms import split_key
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
    "Route",
    "cancel_clip",
    "clip_package",
    "clip_route",
    "default_publishers",
    "posts_preview",
    "process_due",
    "queue_clip",
    "save_drafts",
]

log = get_logger(__name__)

LOCKED_STATES = frozenset({"uploading", "processing", "published", "private_only"})

_POLL_S = 60.0
"""How often a publication still being processed by the platform is checked."""
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
    """Return the API publisher of every platform not in export mode.

    Export publishers are made per row (:func:`_publisher`); a platform whose API
    publisher cannot be built (no stored OAuth client) is left out, and its due
    publications fail with the reason.
    """
    publishers: dict[Platform, Publisher] = {}
    for platform in PLATFORMS:
        if config.publish.mode_for(platform) == "export":
            continue
        if platform == "youtube":
            from peakreel.publish.youtube import (  # imported here: optional at runtime
                YouTubeApi,
                YouTubePublisher,
                load_client,
            )

            client = load_client()
            if client is not None:
                publishers[platform] = YouTubePublisher(YouTubeApi(client))
        elif platform in ("instagram", "facebook"):
            from peakreel.publish.meta import (  # imported here: optional at runtime
                FacebookPublisher,
                InstagramPublisher,
                MetaApi,
                load_app,
            )

            app = load_app()
            if app is not None:
                api = MetaApi(app)
                publishers[platform] = (
                    InstagramPublisher(api) if platform == "instagram" else FacebookPublisher(api)
                )
    return publishers


def _publisher(
    config: Config, publishers: Mapping[Platform, Publisher], platform: Platform, mode: str
) -> Publisher | None:
    """Return the publisher for one publication; export rows always export."""
    chosen = publishers.get(platform)
    if mode == "export":
        if chosen is None or "export" not in chosen.modes:
            return ExportPublisher(platform, config.publish_dir)
        return chosen
    if chosen is None or mode not in chosen.modes:
        return None
    return chosen


def _sidecar(row: sqlite3.Row) -> dict[str, Any]:
    path = row["sidecar_path"]
    if path and Path(path).exists():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            log.warning("unreadable clip sidecar", path=path)
            return {}
        if isinstance(data, dict):
            return cast("dict[str, Any]", data)
    return {}


def _channel(config: Config, name: str, row: sqlite3.Row | None = None) -> ChannelConfig:
    """Return the channel's settings: configured by hand, or recorded by discovery.

    A channel added by category discovery is not in the config; its clips carry
    the permission discovery gave it (``discover.permission``) in their sidecar.
    """
    configured = config.channel(name)
    if configured is not None:
        return configured
    discovered: dict[str, Any] = (_sidecar(row).get("discovered") or {}) if row is not None else {}
    permission = discovered.get("permission", "unknown")
    if permission not in ("granted", "credit_only", "unknown"):
        permission = "unknown"
    return ChannelConfig(name=name, platform=split_key(name)[0], permission=permission)


def _moment_hashtags(view: ClipView) -> tuple[str, ...]:
    gemini = cast("dict[str, Any]", (view.auto or {}).get("gemini") or {})
    return tuple(str(tag) for tag in cast("list[object]", gemini.get("hashtags") or []))


def clip_facts(config: Config, view: ClipView, row: sqlite3.Row) -> ClipFacts:
    """Platform-independent facts of a clip, for :func:`build_metadata`."""
    from peakreel.review.store import suggest_title  # imported here: store imports this module

    stream: dict[str, Any] = _sidecar(row).get("stream") or {}
    channel = _channel(config, view.channel, row)
    return ClipFacts(
        channel=channel,
        # Without a reviewer title, posts name the streamer as they present themselves.
        title=view.title or suggest_title(channel.display, view.top_messages),
        description=view.description,
        game=stream.get("game") or None,
        # The moment's own hashtags, from Gemini (auto review), after the channel's.
        hashtags=_moment_hashtags(view),
    )


@dataclass(frozen=True, slots=True)
class Route:
    """Where a clip is posted.

    Attributes
    ----------
    language : str or None
        The clip's language, or ``None`` if nothing tells.
    source : {"chosen", "channel", "twitch", "speech", "unknown"}
        Where the language came from: the reviewer, the channel's ``language``,
        Twitch's broadcast language, or the language Whisper heard.
    account : PublishAccount or None
        The account posting it; ``None`` means the clip is held for the reviewer.
    """

    language: str | None
    source: str
    account: PublishAccount | None

    def to_json(self) -> dict[str, Any]:
        """JSON-ready form for the review tool."""
        account = self.account
        return {
            "language": self.language,
            "source": self.source,
            "account": None
            if account is None
            else {"id": account.id, "label": account.label, "platforms": list(account.platforms)},
        }


def clip_route(
    config: Config, view: ClipView, row: sqlite3.Row, language: str | None = None
) -> Route:
    """Decide the clip's language and the account that posts it.

    The language is, in order: ``language`` (a preview of the reviewer's choice),
    the reviewer's stored choice, the channel's configured ``language``, Twitch's
    broadcast language (stream status), the language Whisper detected.
    """
    sidecar = _sidecar(row)
    stream: dict[str, Any] = sidecar.get("stream") or {}
    captions: dict[str, Any] = sidecar.get("captions") or {}
    channel = config.channel(view.channel)
    candidates = (
        (language, "chosen"),
        (row["language"], "chosen"),
        (channel.language if channel else None, "channel"),
        (stream.get("language"), "twitch"),
        (captions.get("language"), "speech"),
    )
    for code, source in candidates:
        if code:
            code = str(code).lower()
            return Route(code, source, config.publish.account_for(code))
    return Route(None, "unknown", config.publish.account_for(None))


def clip_package(
    config: Config,
    view: ClipView,
    row: sqlite3.Row,
    scheduled_at: float | None = None,
    account: str = "main",
) -> ClipPackage:
    """Build what a publisher needs to post ``view`` from ``account``."""
    vertical = Path(row["vertical_path"]) if row["vertical_path"] else None
    video = vertical if vertical is not None and vertical.exists() else Path(row["path"])
    thumbnail = Path(row["thumbnail_path"]) if row["thumbnail_path"] else None
    return ClipPackage(
        clip_id=view.id,
        name=Path(row["path"]).stem,
        video=video,
        thumbnail=thumbnail,
        channel=_channel(config, view.channel, row),
        start_utc=view.start_utc,
        flags=view.flags,
        scheduled_utc=_iso(scheduled_at),
        account=account,
    )


def _publication(
    conn: sqlite3.Connection, clip_id: int, account: str, platform: str
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM publications WHERE clip_id = ? AND account = ? AND platform = ?",
        (clip_id, account, platform),
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def _generated(
    config: Config, facts: ClipFacts, platform: Platform, account: PublishAccount
) -> PostMetadata:
    templates = config.publish.templates.get(platform)
    return build_metadata(facts, platform, config.publish, templates, account)


def posts_preview(
    config: Config,
    conn: sqlite3.Connection,
    view: ClipView,
    row: sqlite3.Row,
    language: str | None = None,
) -> dict[str, Any]:
    """Routing and, per platform of the routed account, post text, problems and state.

    A saved or queued publication shows its stored text (which may be edited);
    otherwise the text is generated from the clip's current title and description.
    ``language`` previews another language's account without saving anything.
    """
    route = clip_route(config, view, row, language)
    facts = clip_facts(config, view, row)
    posts: dict[str, dict[str, Any]] = {}
    account = route.account
    if account is None:  # held: no account for this language
        return {"route": route.to_json(), "posts": posts}
    for platform in account.platforms:
        limits = config.publish.limits(platform)
        pub = _publication(conn, view.id, account.id, platform)
        stored = pub is not None and pub["state"] != "cancelled"
        meta = (
            PostMetadata.from_json(json.loads(pub["metadata"]))
            if stored and pub is not None
            else _generated(config, facts, platform, account)
        )
        posts[platform] = {
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
    return {"route": route.to_json(), "posts": posts}


def _schedule(
    conn: sqlite3.Connection, config: Config, account: str, platform: Platform, now: float
) -> float:
    limits = config.publish.limits(platform)
    taken = sorted(
        float(r[0])
        for r in conn.execute(
            "SELECT scheduled_at FROM publications WHERE account = ? AND platform = ? "
            "AND scheduled_at IS NOT NULL AND state != 'cancelled' AND state != 'failed'",
            (account, platform),
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
    config: Config,
    facts: ClipFacts,
    platform: Platform,
    account: PublishAccount,
    edit: Edit | None,
) -> PostMetadata:
    meta = _generated(config, facts, platform, account)
    if edit is not None and (edit.title is not None or edit.text is not None):
        meta = with_edits(meta, edit.title, edit.text)
    return meta


def _upsert(
    conn: sqlite3.Connection,
    clip_id: int,
    account: str,
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
        INSERT INTO publications (clip_id, account, platform, mode, state, metadata, attempts,
                                  scheduled_at, next_attempt_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT (clip_id, account, platform) DO UPDATE SET
            mode = excluded.mode, state = excluded.state, metadata = excluded.metadata,
            attempts = 0, error = NULL, scheduled_at = excluded.scheduled_at,
            next_attempt_at = excluded.next_attempt_at, updated_at = excluded.updated_at
        """,
        (clip_id, account, platform, mode, state, json.dumps(meta.to_json(), ensure_ascii=False),
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
    """Store edited post text as ``pending`` publications of the routed account."""
    now = time.time() if now is None else now
    account = clip_route(config, view, row).account
    if account is None:
        return
    facts = clip_facts(config, view, row)
    for platform, edit in edits.items():
        if platform not in account.platforms:
            continue
        pub = _publication(conn, view.id, account.id, platform)
        if pub is not None and pub["state"] in LOCKED_STATES:
            continue
        meta = _meta_for(config, facts, platform, account, edit)
        state = (
            "pending" if pub is None or pub["state"] in ("cancelled", "failed") else pub["state"]
        )
        _upsert(conn, view.id, account.id, platform, mode=config.publish.mode_for(platform),
                state=state,
                meta=meta, now=now, scheduled_at=pub["scheduled_at"] if pub else None,
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
    """Queue an approved clip on its account for ``platforms`` (all-or-nothing).

    The account comes from the clip's language (:func:`clip_route`); a clip whose
    language has no account is held: nothing is queued and a note says why.
    Publications of the clip on other accounts (its language was changed) are
    cancelled unless they may already be on a platform. Platforms the account
    is not on are skipped.

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
        Notes for the reviewer (held clip, platforms already posted and left alone).

    Raises
    ------
    QueueError
        If any platform's text fails validation; nothing is queued then.
    """
    now = time.time() if now is None else now
    edits = edits or {}
    route = clip_route(config, view, row)
    account = route.account
    if account is None:
        language = route.language or "unknown"
        return [f"held: no account for language {language!r}; choose a language to post it"]
    facts = clip_facts(config, view, row)
    planned: list[tuple[Platform, PostMetadata]] = []
    problems: list[str] = []
    notes: list[str] = []
    for platform in platforms:
        if platform not in account.platforms:
            continue
        pub = _publication(conn, view.id, account.id, platform)
        if pub is not None and pub["state"] in LOCKED_STATES:
            notes.append(f"{platform}: already {pub['state']}, left as is")
            continue
        edit = edits.get(platform)
        if edit is None and pub is not None and pub["state"] != "cancelled":
            stored = PostMetadata.from_json(json.loads(pub["metadata"]))
            if "edited by reviewer" in stored.notes:
                edit = Edit(stored.title, stored.text)
        meta = _meta_for(config, facts, platform, account, edit)
        problems += [f"{platform}: {p}" for p in validate(meta, config.publish.limits(platform))]
        planned.append((platform, meta))
    if problems:
        raise QueueError("; ".join(problems))
    notes += _cancel(config, conn, view, row, keep=account.id, now=now)
    for platform, meta in planned:
        pub = _publication(conn, view.id, account.id, platform)
        keep_slot = pub is not None and pub["state"] in ("approved", "exported", "pending")
        scheduled = (
            pub["scheduled_at"]
            if keep_slot and pub is not None and pub["scheduled_at"]
            else _schedule(conn, config, account.id, platform, now)
        )
        mode = config.publish.mode_for(platform)
        if mode == "public" and facts.channel.permission == "unknown":
            # Never public without the streamer's permission: private where the platform
            # can hold a post privately, otherwise an export package for a decision by hand.
            mode = "private" if platform in PRIVATE_PLATFORMS else "export"
            notes.append(f"{platform}: {mode} (permission of {facts.channel.name} unknown)")
        due = now if mode == "export" else scheduled
        _upsert(conn, view.id, account.id, platform, mode=mode, state="approved", meta=meta,
                now=now, scheduled_at=scheduled, next_attempt_at=due)  # fmt: skip
        log.info("publication queued", clip_id=view.id, account=account.id, platform=platform,
                 mode=mode)  # fmt: skip
    conn.commit()
    return notes


def _cancel(
    config: Config,
    conn: sqlite3.Connection,
    view: ClipView,
    row: sqlite3.Row,
    *,
    keep: str | None = None,
    publishers: Mapping[Platform, Publisher] | None = None,
    now: float,
) -> list[str]:
    remaining: list[str] = []
    rows = conn.execute(
        "SELECT * FROM publications WHERE clip_id = ? AND state != 'cancelled'", (view.id,)
    ).fetchall()
    for pub in rows:
        if pub["account"] == keep:
            continue
        platform = cast("Platform", pub["platform"])
        where = f"{pub['account']}/{platform}"
        if pub["state"] in LOCKED_STATES:
            remaining.append(f"{where}: {pub['state']} {pub['url'] or ''}".strip())
            continue
        if pub["state"] == "exported":
            package = clip_package(config, view, row, account=pub["account"])
            ExportPublisher(platform, config.publish_dir).remove(package, pub["url"])
        conn.execute(
            "UPDATE publications SET state = 'cancelled', updated_at = ? WHERE id = ?",
            (now, pub["id"]),
        )
    conn.commit()
    if remaining:
        log.warning("clip may still be posted; remove it by hand", clip_id=view.id, posts=remaining)
    return remaining


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
        ``account/platform`` entries where the clip may still be posted.
    """
    now = time.time() if now is None else now
    return _cancel(config, conn, view, row, publishers=publishers, now=now)


@dataclass(frozen=True, slots=True)
class Processed:
    """What :func:`process_due` did with one publication."""

    clip_id: int
    platform: str
    state: str
    detail: str = ""
    account: str = "main"


def process_due(
    config: Config,
    conn: sqlite3.Connection,
    publishers: Mapping[Platform, Publisher] | None = None,
    *,
    now: float | None = None,
    dry_run: bool = False,
    export_only: bool = False,
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
    export_only : bool, default False
        Only write export packages (fast, local); leave API posts to the
        background loop, so approving a clip never waits for an upload.

    Returns
    -------
    list of Processed
    """
    from peakreel.review.store import get_clip, view_of

    now = time.time() if now is None else now
    publishers = publishers or default_publishers(config)
    done: list[Processed] = []
    rows = conn.execute(
        "SELECT * FROM publications WHERE (state = 'uploading' "
        "OR (state IN ('approved', 'processing') AND next_attempt_at <= ?)) "
        "AND (? = 0 OR mode = 'export') ORDER BY scheduled_at, id",
        (now, int(export_only)),
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
            done.append(
                Processed(
                    int(pub["clip_id"]), platform, "cancelled", "clip blocked", str(pub["account"])
                )
            )
            continue
        view = view_of(clip_row)
        account = str(pub["account"])
        package = clip_package(config, view, clip_row, pub["scheduled_at"], account)
        publisher = _publisher(config, publishers, platform, str(pub["mode"]))
        meta = PostMetadata.from_json(json.loads(pub["metadata"]))
        if publisher is None:
            error = f"no {platform} publisher for {pub['mode']} mode (is the account set up?)"
            if not dry_run:
                _fail(conn, pub["id"], error, now, final=True)
            done.append(Processed(view.id, platform, "failed", error, account))
            continue
        if pub["state"] == "processing":
            # Uploaded; the platform is still processing it.
            try:
                status = publisher.poll_status(package, pub["remote_id"])
            except PublishError as exc:
                if not dry_run:
                    _fail(conn, pub["id"], str(exc), now, final=True)
                done.append(Processed(view.id, platform, "failed", str(exc), account))
                continue
            if status is None or status.state == "processing":
                if not dry_run:
                    conn.execute(
                        "UPDATE publications SET next_attempt_at = ?, updated_at = ? WHERE id = ?",
                        (now + _POLL_S, now, pub["id"]),
                    )
                    conn.commit()
                done.append(Processed(view.id, platform, "processing", "still processing", account))
                continue
            if not dry_run:
                _finish(conn, pub["id"], status.state, status.remote_id, status.url, now)
            done.append(Processed(view.id, platform, status.state, status.url or "", account))
            continue
        if pub["state"] == "uploading":
            # Interrupted while publishing: ask, never blindly send again.
            status = publisher.poll_status(package, pub["remote_id"])
            if status is not None:
                if not dry_run:
                    _finish(conn, pub["id"], status.state, status.remote_id, status.url, now)
                done.append(Processed(view.id, platform, status.state, "recovered", account))
                continue
            if pub["mode"] != "export":
                error = "interrupted during upload; check the platform before retrying"
                if not dry_run:
                    _fail(conn, pub["id"], error, now, final=True)
                done.append(Processed(view.id, platform, "failed", error, account))
                continue
        problems = validate(meta, config.publish.limits(platform))
        problems += publisher.validate(package, meta)
        if problems:
            if not dry_run:
                _fail(conn, pub["id"], "; ".join(problems), now, final=True)
            done.append(Processed(view.id, platform, "failed", "; ".join(problems), account))
            continue
        if dry_run:
            done.append(Processed(view.id, platform, "approved", f"would {pub['mode']}", account))
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
            retry_after = exc.retry_after_s if isinstance(exc, PublishError) else None
            if retry_after is not None:
                # A temporary refusal (quota): try again later, without using an attempt.
                conn.execute(
                    "UPDATE publications SET state = 'approved', attempts = attempts - 1, "
                    "error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                    (str(exc), now + retry_after, now, pub["id"]),
                )
                conn.commit()
                log.warning("publish postponed", clip_id=view.id, platform=platform, error=str(exc))
                done.append(Processed(view.id, platform, "approved", str(exc), account))
                continue
            attempts = int(pub["attempts"]) + 1
            final = attempts >= config.publish.max_attempts
            _fail(conn, pub["id"], str(exc), now, final=final, attempts=attempts,
                  retry_s=config.publish.retry_initial_s)  # fmt: skip
            log.warning("publish failed", clip_id=view.id, platform=platform, error=str(exc))
            done.append(
                Processed(view.id, platform, "failed" if final else "approved", str(exc), account)
            )
            continue
        poll_at = now + _POLL_S if result.state == "processing" else None
        _finish(conn, pub["id"], result.state, result.remote_id, result.url, now,
                next_attempt_at=poll_at)  # fmt: skip
        log.info(
            "published", clip_id=view.id, platform=platform, state=result.state, url=result.url
        )
        done.append(Processed(view.id, platform, result.state, result.url or "", account))
    return done


def _finish(
    conn: sqlite3.Connection,
    pub_id: int,
    state: str,
    remote_id: str | None,
    url: str | None,
    now: float,
    *,
    next_attempt_at: float | None = None,
) -> None:
    conn.execute(
        "UPDATE publications SET state = ?, remote_id = ?, url = ?, error = NULL, "
        "next_attempt_at = coalesce(?, next_attempt_at), updated_at = ?, "
        "published_at = CASE WHEN ? IN ('published', 'processing', 'private_only') "
        "THEN coalesce(published_at, ?) ELSE published_at END WHERE id = ?",
        (state, remote_id, url, next_attempt_at, now, state, now, pub_id),
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
