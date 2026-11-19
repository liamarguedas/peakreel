"""Measure auto review against a person's decisions.

Two numbers decide whether auto review may act alone:

- :func:`evaluate_judge`: Gemini judges clips a person already decided, without
  seeing that decision, and the separation (AUC) of its scores is computed.
- :func:`audit_summary`: of the auto-rejected clips a person re-checked (the
  review page's Check tab), how many they would have approved.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from peakreel.config import Config
from peakreel.log import get_logger
from peakreel.publish.queue import clip_facts, clip_route
from peakreel.review.auto import Judge
from peakreel.review.gemini import GeminiError
from peakreel.review.store import get_clip, view_of

__all__ = ["JudgeEval", "auc", "audit_summary", "evaluate_judge"]

log = get_logger(__name__)


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float | None:
    """Return how well scores separate two groups (ROC AUC).

    The share of (positive, negative) pairs where the positive scores higher, ties
    counting half: 0.5 is a coin flip, 1.0 perfect.

    Parameters
    ----------
    positives, negatives : sequence of float
        Scores of the clips a person approved and rejected.

    Returns
    -------
    float or None
        ``None`` when either group is empty.

    Examples
    --------
    >>> auc([8, 7, 6], [3, 6])
    0.9166666666666666
    >>> auc([5], [5])
    0.5
    """
    if not positives or not negatives:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


@dataclass(frozen=True, slots=True)
class JudgeEval:
    """Gemini's scores on clips a person decided.

    Attributes
    ----------
    scores : list of (int, bool, int)
        Clip id, whether the person approved it, Gemini's score.
    errors : int
        Clips Gemini could not judge (busy, quota).
    """

    scores: list[tuple[int, bool, int]]
    errors: int

    @property
    def auc(self) -> float | None:
        """Separation of approved from rejected clips, see :func:`auc`."""
        return auc([s for _, ok, s in self.scores if ok], [s for _, ok, s in self.scores if not ok])

    def approved_at(self, threshold: int) -> tuple[int, int]:
        """Return ``(approved clips, rejected clips)`` scoring ``threshold`` or more."""
        return (
            sum(ok and s >= threshold for _, ok, s in self.scores),
            sum(not ok and s >= threshold for _, ok, s in self.scores),
        )


def evaluate_judge(
    config: Config,
    conn: sqlite3.Connection,
    judge: Judge,
    *,
    limit: int = 30,
    pause_s: float = 4.0,
    progress: Callable[[int, bool, int | None], None] | None = None,
) -> JudgeEval:
    """Have ``judge`` score clips a person decided and collect the scores.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    judge : Judge
        :func:`peakreel.review.auto.gemini_judge`; it leaves the judged clip out of
        its examples, so it never sees the answer.
    limit : int, default 30
        Clips to judge, half approved and half rejected (the latest with media);
        each uses one Gemini request.
    pause_s : float, default 4.0
        Wait between requests (free-tier rate limit).
    progress : callable, optional
        ``progress(clip_id, approved, score)`` after each clip (``score`` is
        ``None`` on an error).

    Returns
    -------
    JudgeEval
    """
    picked: list[tuple[int, bool]] = []
    for state, count in (("approved", (limit + 1) // 2), ("rejected", limit // 2)):
        rows = conn.execute(
            "SELECT id, path FROM clips WHERE state = ? AND reviewed_by = 'person' "
            "AND translation_of IS NULL AND purged_at IS NULL ORDER BY reviewed_at DESC",
            (state,),
        ).fetchall()
        ids = [int(r["id"]) for r in rows if r["path"] and Path(r["path"]).is_file()]
        picked += [(i, state == "approved") for i in ids[:count]]
    scores: list[tuple[int, bool, int]] = []
    errors = 0
    for n, (clip_id, approved) in enumerate(picked):
        if n:
            time.sleep(pause_s)
        row = get_clip(conn, clip_id)
        if row is None:
            continue
        view = view_of(row)
        try:
            verdict = judge(
                row,
                view,
                clip_facts(config, view, row).game,
                clip_route(config, view, row).language,
            )
        except GeminiError as exc:
            log.warning("evaluation: Gemini could not judge", clip_id=clip_id, error=str(exc)[:200])
            errors += 1
            if progress:
                progress(clip_id, approved, None)
            continue
        scores.append((clip_id, approved, verdict.score))
        if progress:
            progress(clip_id, approved, verdict.score)
    return JudgeEval(scores, errors)


def audit_summary(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return ``(approved, rejected)`` among auto-rejected clips a person re-checked.

    A high first number means auto review throws good clips away.
    """
    row = conn.execute(
        "SELECT sum(state = 'approved'), sum(state = 'rejected') FROM clips "
        "WHERE reviewed_by = 'person' AND translation_of IS NULL "
        "AND json_extract(auto, '$.decision') = 'reject' AND json_extract(auto, '$.applied') = 1"
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)
