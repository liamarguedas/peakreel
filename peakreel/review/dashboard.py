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

__all__ = ["dashboard"]

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
