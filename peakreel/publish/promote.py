"""Instagram first: clips that do well there are promoted to the scarcer platforms.

YouTube lets the whole project upload 100 videos a day and Facebook 20 Reels per Page
here, far fewer than Instagram takes; queuing every clip everywhere left those queues
days behind (2026-11-19). With ``publish.platforms: [instagram]`` and
``publish.promote_to: [youtube, facebook]``, an approved clip is posted on Instagram
only; its 3-hour numbers decide whether it also goes to the others.

A post is promoted when its engagement (likes + 3 x comments, at its latest reading
from :mod:`peakreel.publish.stats`) beats the median of its language's Instagram posts
of the last 24 hours; while that median is 0 (new accounts), any engagement counts.
Each clip is promoted once: a promotion that expired is not tried again.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time

from peakreel.config import Config
from peakreel.log import get_logger
from peakreel.publish.queue import QueueError, queue_clip

__all__ = ["promote_once"]

log = get_logger(__name__)

_AFTER_H = 3.0  # the first stats reading (stats.CHECKPOINTS_H[0])
_WINDOW_H = 24.0


def promote_once(
    config: Config, conn: sqlite3.Connection, *, now: float | None = None
) -> list[int]:
    """Queue for ``publish.promote_to`` the Instagram posts doing better than their peers.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
        Committed by the queue.
    now : float, optional

    Returns
    -------
    list of int
        The clips promoted in this pass.

    Notes
    -----
    Engagement per post is likes + 3 x comments (comments take more effort). Views
    would be better and replace it once Instagram insights are granted.
    """
    from peakreel.review.store import get_clip, view_of

    targets = list(config.publish.promote_to)
    if not targets:
        return []
    now = time.time() if now is None else now
    language_of = {a.id: (a.languages[0] if a.languages else "") for a in config.publish.accounts}
    rows = conn.execute(
        "SELECT p.clip_id, p.account, p.published_at, "
        "(SELECT coalesce(s.likes, 0) + 3 * coalesce(s.comments, 0) FROM post_stats s "
        " WHERE s.publication_id = p.id AND (s.likes IS NOT NULL OR s.comments IS NOT NULL) "
        " ORDER BY s.checkpoint_h DESC LIMIT 1) AS score "
        "FROM publications p JOIN clips c ON c.id = p.clip_id "
        "WHERE p.platform = 'instagram' AND p.published_at > ? "
        "AND c.state = 'approved' AND c.purged_at IS NULL",
        (now - _WINDOW_H * 3600,),
    ).fetchall()
    scores: dict[str, list[int]] = {}
    for r in rows:
        if r["score"] is not None:
            scores.setdefault(language_of.get(r["account"], ""), []).append(int(r["score"]))
    promoted: list[int] = []
    for r in rows:
        if r["score"] is None or r["published_at"] > now - _AFTER_H * 3600:
            continue  # not read yet: wait for its numbers
        language = language_of.get(r["account"], "")
        median = statistics.median(scores[language])
        if not (r["score"] > median or (median == 0 and r["score"] > 0)):
            continue
        already = conn.execute(
            "SELECT 1 FROM publications WHERE clip_id = ? "
            "AND platform IN (SELECT value FROM json_each(?)) LIMIT 1",
            (r["clip_id"], json.dumps(targets)),
        ).fetchone()
        if already is not None:  # promoted before (posted, waiting or expired)
            continue
        row = get_clip(conn, int(r["clip_id"]))
        if row is None:
            continue
        try:
            queue_clip(config, conn, view_of(row, conn), row, targets, now=now)
        except QueueError as exc:
            log.warning("promotion refused", clip_id=r["clip_id"], error=str(exc))
            continue
        promoted.append(int(r["clip_id"]))
        log.info("promoted", clip_id=r["clip_id"], score=r["score"], median=median,
                 language=language, platforms=targets)  # fmt: skip
    return promoted
