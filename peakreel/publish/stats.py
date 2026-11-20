"""How posts perform: views, likes and comments 12, 24 and 72 hours after posting.

Every post on a platform is read once per checkpoint (:data:`CHECKPOINTS_H`) and the
numbers are stored in ``post_stats``, so what works (a title style, a channel, a
Gemini score, a posting hour) is measured instead of guessed.

What each platform gives with Peakreel's sign-in:

- **YouTube:** views, likes and comments (``youtube.readonly``).
- **Instagram:** likes and comments; views only with the ``instagram_manage_insights``
  permission (see ``docs/m12-meta.md``).
- **Facebook:** likes and comments; views only with ``read_insights``.

A number a platform does not give is stored as ``NULL``, never guessed.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from peakreel.log import get_logger
from peakreel.publish.publishers import PublishError

__all__ = ["CHECKPOINTS_H", "Fetcher", "Stats", "default_fetchers", "read_stats_once", "report"]

log = get_logger(__name__)

CHECKPOINTS_H = (3, 12, 24, 72)  # 3 h: the promotion read (publish.promote)
"""Hours after posting at which a post's numbers are read."""

_PER_PASS = 40  # reads per pass; a backlog (the page was off) catches up over a few passes
_GIVE_UP_H = 6  # a post still unreadable this long after its checkpoint (deleted) is skipped
# Meta's answer for a deleted post. Retried every pass, 5 deleted posts made 843 failed
# calls in 2 hours (2026-11-20) and used up the app's request limit: posting was refused.
_GONE = ("does not exist", "Unsupported get request", "nonexisting field")


@dataclass(frozen=True, slots=True)
class Stats:
    """A post's numbers at one moment; ``None`` where the platform does not tell."""

    views: int | None = None
    likes: int | None = None
    comments: int | None = None


Fetcher = Callable[[str, str], Stats]
"""``fetch(account, remote_id) -> Stats`` for one platform."""


def _int(value: object) -> int | None:
    try:
        return None if value is None else int(str(value))
    except ValueError:
        return None


def default_fetchers() -> dict[str, Fetcher]:
    """Return a fetcher per platform whose credentials are stored."""
    fetchers: dict[str, Fetcher] = {}
    from peakreel.publish.youtube import YouTubeApi, load_client, load_token

    client = load_client()
    if client is not None:
        youtube = YouTubeApi(client)

        def youtube_stats(account: str, video_id: str) -> Stats:
            token = load_token(account)
            if token is None:
                raise PublishError(f"no YouTube login for {account}")
            numbers = youtube.statistics(youtube.access_token(token), video_id)
            return Stats(
                _int(numbers.get("viewCount")),
                _int(numbers.get("likeCount")),
                _int(numbers.get("commentCount")),
            )

        fetchers["youtube"] = youtube_stats
    from peakreel.publish.meta import MetaApi, load_app, load_page

    app = load_app()
    if app is not None:
        meta = MetaApi(app)

        def page_token(account: str) -> str:
            page = load_page(account)
            if page is None:
                raise PublishError(f"no Meta login for {account}")
            return page.page_token

        def instagram_stats(account: str, media_id: str) -> Stats:
            numbers = meta.instagram_stats(media_id, page_token(account))
            return Stats(numbers.get("views"), numbers.get("likes"), numbers.get("comments"))

        def facebook_stats(account: str, video_id: str) -> Stats:
            numbers = meta.facebook_stats(video_id, page_token(account))
            return Stats(numbers.get("views"), numbers.get("likes"), numbers.get("comments"))

        fetchers["instagram"] = instagram_stats
        fetchers["facebook"] = facebook_stats
    return fetchers


def read_stats_once(
    conn: sqlite3.Connection,
    fetchers: Mapping[str, Fetcher] | None = None,
    *,
    now: float | None = None,
) -> int:
    """Read the numbers of every post that reached a checkpoint since the last pass.

    Parameters
    ----------
    conn : sqlite3.Connection
    fetchers : mapping, optional
        Per platform; default :func:`default_fetchers`.
    now : float, optional

    Returns
    -------
    int
        Checkpoints recorded.

    Notes
    -----
    A post read late (the page was off) is stored with its real age (``age_h``); a
    checkpoint already passed and the next one share one read. A failed read is
    logged and tried again next pass, until it is :data:`_GIVE_UP_H` hours past its
    checkpoint: the checkpoint is then stored with no numbers, so a dead post never
    takes the place of a live one. A post the platform says no longer exists gets every
    checkpoint stored empty at once and is never asked for again. A rate limit ends the
    pass: the other reads would only use up more of the platform's limit, which
    publishing shares.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT p.id, p.platform, p.account, p.remote_id, p.published_at, "
        "(SELECT max(s.checkpoint_h) FROM post_stats s WHERE s.publication_id = p.id) AS done "
        "FROM publications p WHERE p.state IN ('published', 'processing') "
        "AND p.remote_id IS NOT NULL AND p.published_at IS NOT NULL "
        "AND p.published_at <= ? ORDER BY p.published_at",
        (now - CHECKPOINTS_H[0] * 3600,),
    ).fetchall()
    due = [
        (
            row,
            [
                c
                for c in CHECKPOINTS_H
                if c > (row["done"] or 0) and row["published_at"] + c * 3600 <= now
            ],
        )
        for row in rows
    ]
    due = [(row, checkpoints) for row, checkpoints in due if checkpoints][:_PER_PASS]
    if not due:
        return 0
    fetchers = default_fetchers() if fetchers is None else fetchers
    recorded = 0
    for row, checkpoints in due:
        fetch = fetchers.get(str(row["platform"]))
        if fetch is None:
            continue
        age_h = round((now - row["published_at"]) / 3600, 2)
        try:
            stats = fetch(str(row["account"]), str(row["remote_id"]))
        except Exception as exc:  # one post must not stop the others (or publishing)
            log.warning("post stats not read", publication=row["id"], error=str(exc)[:200])
            if isinstance(exc, PublishError) and exc.retry_after_s is not None:
                break  # rate limited: stop asking until the next pass
            if any(gone in str(exc) for gone in _GONE):
                checkpoints = [c for c in CHECKPOINTS_H if c > (row["done"] or 0)]
                log.info("post gone, its stats are no longer read", publication=row["id"])
            elif age_h < checkpoints[-1] + _GIVE_UP_H:
                continue
            stats = Stats()  # deleted or unreadable for hours: record the gap, stop retrying
        conn.executemany(
            "INSERT OR IGNORE INTO post_stats (publication_id, checkpoint_h, age_h, read_at, "
            "views, likes, comments) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (row["id"], c, age_h, now, stats.views, stats.likes, stats.comments)
                for c in checkpoints
            ],
        )
        conn.commit()
        recorded += len(checkpoints)
        log.info("post stats", publication=row["id"], platform=row["platform"],
                 checkpoint_h=checkpoints[-1], views=stats.views, likes=stats.likes)  # fmt: skip
    return recorded


def _rank_correlation(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman rank correlation, or ``None`` below 5 pairs.

    Examples
    --------
    >>> _rank_correlation([(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)])
    1.0
    """
    if len(pairs) < 5:
        return None
    try:
        return statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs], method="ranked")
    except statistics.StatisticsError:  # every value the same
        return None


def report(conn: sqlite3.Connection, checkpoint_h: int = 24) -> dict[str, Any]:
    """Summarise views at ``checkpoint_h``: per account and platform, top posts, Gemini.

    Returns
    -------
    dict
        ``groups``: ``{(account, platform): (posts, median views)}``; ``top``: the 10
        most viewed posts (views, account, platform, url, title); ``gemini``: rank
        correlation of Gemini's score with views (1 = it predicts them, 0 = no
        relation) and the number of posts it is based on; ``late``: reads left out
        because they were taken more than 25% after the checkpoint (views only grow,
        so a late read would flatter the post).
    """
    rows = conn.execute(
        "SELECT p.account, p.platform, p.url, s.views, "
        "json_extract(c.auto, '$.gemini.score') AS score, "
        "coalesce(nullif(c.title, ''), json_extract(c.auto, '$.gemini.title')) AS title "
        "FROM post_stats s JOIN publications p ON p.id = s.publication_id "
        "JOIN clips c ON c.id = p.clip_id WHERE s.checkpoint_h = ? AND s.views IS NOT NULL "
        "AND s.age_h <= s.checkpoint_h * 1.25",
        (checkpoint_h,),
    ).fetchall()
    (late,) = conn.execute(
        "SELECT count(*) FROM post_stats WHERE checkpoint_h = ? AND age_h > checkpoint_h * 1.25",
        (checkpoint_h,),
    ).fetchone()
    groups: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        groups.setdefault((r["account"], r["platform"]), []).append(int(r["views"]))
    top = sorted(rows, key=lambda r: -int(r["views"]))[:10]
    scored = [(float(r["score"]), float(r["views"])) for r in rows if r["score"] is not None]
    return {
        "late": int(late),
        "groups": {k: (len(v), statistics.median(v)) for k, v in sorted(groups.items())},
        "top": [(int(r["views"]), r["account"], r["platform"], r["url"], r["title"]) for r in top],
        "gemini": (_rank_correlation(scored), len(scored)),
    }
