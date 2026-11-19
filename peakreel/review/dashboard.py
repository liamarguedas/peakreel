"""Numbers for the review page's Stats tab: clips, reviews, posts and views.

Everything is read from the database on each call; nothing is cached or stored.
Views are the latest reading per post (:mod:`peakreel.publish.stats`), so a post
read at 12 h counts its 12 h views until its 24 h reading arrives.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from peakreel.publish.stats import report
from peakreel.review.evaluate import audit_summary

__all__ = ["dashboard", "schedule"]

_LIVE = "('published', 'processing', 'private_only')"  # see store.ON_PLATFORM_STATES
_HOURS = 48

# Each post with its latest view and like counts (NULL before the first read).
# 'exported' is the dropped hand-posting mode; 'cancelled' never went anywhere.
_POSTS = """
    SELECT p.id, p.clip_id, p.account, p.platform, p.state, p.url,
           coalesce(p.published_at, p.updated_at) AS live_at,
           s.views, s.likes, ch.name AS channel,
           coalesce(nullif(json_extract(p.metadata, '$.title'), ''), c.title) AS title
    FROM publications p
    JOIN clips c ON c.id = p.clip_id
    JOIN channels ch ON ch.id = c.channel_id
    LEFT JOIN post_stats s ON s.publication_id = p.id AND s.checkpoint_h = (
        SELECT max(checkpoint_h) FROM post_stats WHERE publication_id = p.id)
    WHERE p.state NOT IN ('cancelled', 'exported')
"""


def dashboard(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, Any]:
    """Summarise the pipeline for the Stats tab.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database connection with ``row_factory = sqlite3.Row``.
    now : float, optional
        Current Unix time; defaults to the clock. Sets the end of the hourly chart.

    Returns
    -------
    dict
        ``clips``: counts of clips by state and by who decided (``person`` or
        ``auto``), and ``reasons``, the reviewer's reject reasons.
        ``check``: ``[approved, rejected]`` among auto-rejected clips the reviewer
        re-checked (the first is what auto review got wrong).
        ``gemini``: ``[rank correlation, posts]`` of Gemini's score with 12 h views,
        correlation ``None`` with too few posts.
        ``posts``: per account and platform, ``live``, ``waiting``, ``failed``,
        ``views`` and ``likes`` (``views`` is ``None`` until one post is read).
        ``hourly``: ``[hour start (ISO, UTC), posts that went live]`` for the last
        48 hours, oldest first, zeros included.
        ``top``: the 10 most viewed posts; ``channels``: the 10 channels with most
        approved clips, with the views of their posts.
    """
    now = time.time() if now is None else now
    clips = conn.execute(
        "SELECT state, coalesce(reviewed_by, '') AS by, count(*) AS n FROM clips "
        "WHERE translation_of IS NULL GROUP BY 1, 2"
    ).fetchall()
    reasons = conn.execute(
        "SELECT review_reason, count(*) AS n FROM clips WHERE review_reason IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    posts = conn.execute(_POSTS).fetchall()

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for p in posts:
        g = groups.setdefault(
            (p["account"], p["platform"]),
            {"account": p["account"], "platform": p["platform"], "live": 0, "waiting": 0,
             "failed": 0, "views": None, "likes": None},
        )  # fmt: skip
        kind = "failed" if p["state"] == "failed" else "live" if p["state"] in _LIVE else "waiting"
        g[kind] += 1
        for key in ("views", "likes"):
            if p[key] is not None:
                g[key] = (g[key] or 0) + p[key]

    end = int(now // 3600) * 3600
    start = end - (_HOURS - 1) * 3600
    hourly = dict.fromkeys(range(start, end + 3600, 3600), 0)
    for p in posts:
        if p["state"] in _LIVE and p["live_at"] >= start:
            hourly[int(p["live_at"] // 3600) * 3600] += 1

    viewed = [p for p in posts if p["views"] is not None]
    top = sorted(viewed, key=lambda p: -p["views"])[:10]
    channel_views: dict[str, int] = {}
    for p in viewed:
        if p["state"] in _LIVE:
            channel_views[p["channel"]] = channel_views.get(p["channel"], 0) + p["views"]
    channels = conn.execute(
        "SELECT ch.name AS channel, count(*) AS approved FROM clips c "
        "JOIN channels ch ON ch.id = c.channel_id "
        "WHERE c.state = 'approved' AND c.translation_of IS NULL "
        "GROUP BY ch.id ORDER BY approved DESC, ch.name LIMIT 10"
    ).fetchall()
    gemini = report(conn, 12)["gemini"]
    return {
        "clips": {
            "counts": [{"state": r["state"], "by": r["by"], "n": r["n"]} for r in clips],
            "reasons": [{"reason": r["review_reason"], "n": r["n"]} for r in reasons],
        },
        "check": list(audit_summary(conn)),
        "gemini": list(gemini),
        "posts": sorted(groups.values(), key=lambda g: (g["account"], g["platform"])),
        "hourly": [[datetime.fromtimestamp(h, UTC).isoformat(), n] for h, n in hourly.items()],
        "top": [
            {k: p[k] for k in ("views", "likes", "account", "platform", "url", "title", "channel")}
            for p in top
        ],
        "channels": [{**dict(r), "views": channel_views.get(r["channel"])} for r in channels],
    }


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat()


def schedule(
    conn: sqlite3.Connection, *, hold_s: float | None = None, now: float | None = None
) -> dict[str, Any]:
    """Where every clip and post is in the pipeline, for the Schedule tab.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database connection with ``row_factory = sqlite3.Row``.
    hold_s : float, optional
        Auto review's hold (``list_clips(ai_hold_s=...)``): a new clip it has not
        decided counts as "AI checking" for this long, then as waiting for a person.
        ``None`` (auto review not in ``on`` mode): every new clip waits for a person.
    now : float, optional
        Current Unix time; defaults to the clock.

    Returns
    -------
    dict
        ``stages``: counts for the strip, ``made``, ``approved``, ``rejected`` and
        ``posted`` over the last 24 hours, ``checking``, ``to_review`` and ``queued``
        now. ``queued``: every post waiting to go out, soonest first, with ``due``
        (ISO, UTC: when it is tried next), ``state`` (``approved``: waiting,
        ``uploading``, ``processing``: the platform is processing it) and ``note``,
        the last refusal (a quota, a block) when it is waiting on one.
        ``posted``: posts that went live in the last 24 hours, newest first, with
        ``at``, ``url`` and ``views``.
    """
    now = time.time() if now is None else now
    day = now - 86400
    decided = "auto IS NOT NULL AND json_extract(auto, '$.mode') != 'shadow'"
    new = "state = 'new' AND duplicate_of IS NULL AND purged_at IS NULL"
    hold_since = now - hold_s if hold_s is not None else now
    (made, approved, rejected, checking, to_review) = conn.execute(
        f"""SELECT
            sum(created_at >= ?), sum(state = 'approved' AND reviewed_at >= ?),
            sum(state = 'rejected' AND reviewed_at >= ?),
            sum({new} AND NOT ({decided}) AND created_at >= ?),
            sum({new} AND (({decided}) OR created_at < ?))
            FROM clips WHERE translation_of IS NULL""",  # noqa: S608 - constant text
        (day, day, day, hold_since, hold_since),
    ).fetchone()
    post = """SELECT p.id, p.account, p.platform, p.state, p.url, p.error,
                p.next_attempt_at, coalesce(p.published_at, p.updated_at) AS live_at,
                ch.name AS channel,
                coalesce(nullif(json_extract(p.metadata, '$.title'), ''), c.title) AS title,
                (SELECT s.views FROM post_stats s WHERE s.publication_id = p.id
                 ORDER BY s.checkpoint_h DESC LIMIT 1) AS views
              FROM publications p JOIN clips c ON c.id = p.clip_id
              JOIN channels ch ON ch.id = c.channel_id"""
    queued = conn.execute(
        f"{post} WHERE p.state IN ('approved', 'uploading', 'processing') "
        "ORDER BY p.next_attempt_at, p.id"
    ).fetchall()
    posted = conn.execute(
        f"{post} WHERE p.state IN ('published', 'private_only') "
        "AND coalesce(p.published_at, p.updated_at) >= ? ORDER BY live_at DESC",
        (day,),
    ).fetchall()
    keys = ("account", "platform", "state", "channel", "title")
    return {
        "stages": {
            "made": made or 0,
            "checking": checking or 0,
            "to_review": to_review or 0,
            "approved": approved or 0,
            "rejected": rejected or 0,
            "queued": len(queued),
            "posted": len(posted),
        },
        "queued": [
            {**{k: r[k] for k in keys}, "due": _iso(r["next_attempt_at"]), "note": r["error"]}
            for r in queued
        ],
        "posted": [
            {
                **{k: r[k] for k in keys},
                "at": _iso(r["live_at"]),
                "url": r["url"],
                "views": r["views"],
            }
            for r in posted
        ],
    }
