"""Automatic review by rules: reject weak clips, approve strong ones, leave the rest.

No AI is involved. A finished clip (render, captions and safety flags recorded) gets
one decision, stored in ``clips.auto``:

- **reject**: a safety flag in ``review.auto.reject_flags`` (viewer names on
  screen, music), no speech, or a chat that is not excited (a poll: "yes", "no").
- **approve**: excited chat, a strong spike, no flags, captions, a streamer whose
  permission allows public posts, a posting account, and under the daily cap.
- **review**: anything else, left for a person, with what it lacked.

In ``shadow`` mode the decision is only shown on the review card; in ``on`` mode
it is applied through :func:`peakreel.review.store.update_clip`, exactly like a
reviewer's click (exports, publication queue, blocklist check).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from peakreel.config import AutoReviewConfig, Config, resolve_tool
from peakreel.detect.filters import excitement
from peakreel.log import get_logger
from peakreel.publish.queue import clip_facts, clip_route
from peakreel.review.gemini import GeminiError, Verdict, judge_clip, load_key
from peakreel.review.store import ClipView, ReviewError, get_clip, update_clip, view_of

log = get_logger(__name__)

__all__ = ["Decision", "auto_review_once", "decide", "excitement"]


@dataclass(frozen=True, slots=True)
class Decision:
    """An automatic review decision.

    Attributes
    ----------
    decision : {"approve", "reject", "review"}
        ``review`` leaves the clip for a person.
    reason : str
        Why, in words for the review card.
    excitement : float
        Share of the moment's top chat messages with an excited word (0 to 1).
    """

    decision: Literal["approve", "reject", "review"]
    reason: str
    excitement: float


def decide(
    view: ClipView,
    *,
    settings: AutoReviewConfig,
    permission: str,
    has_account: bool,
    approved_today: int,
    verdict: Verdict | None = None,
) -> Decision:
    """Decide one clip by the rules in ``settings``, and Gemini's ``verdict`` if any.

    Parameters
    ----------
    view : ClipView
        A finished clip (its flags are recorded).
    settings : AutoReviewConfig
    permission : {"granted", "credit_only", "unknown"}
        The streamer's permission; only the first two may be auto-approved.
    has_account : bool
        Whether a posting account covers the clip's language.
    approved_today : int
        Auto-approvals already made today for the clip's account.
    verdict : Verdict, optional
        Gemini's judgment. With one, its score decides instead of chat excitement
        and spike score (``ai_reject`` rejects, ``ai_approve`` may approve); without
        one, the chat rules decide alone.

    Returns
    -------
    Decision
    """
    exc = excitement(view.top_messages, settings.excitement_words)
    chat = ", ".join(f"{text} ×{count}" for text, count in view.top_messages[:3]) or "no chat"  # noqa: RUF001 - the cards use the multiplication sign
    kinds = sorted({flag["kind"] for flag in view.flags})
    rejecting = [kind for kind in kinds if kind in settings.reject_flags]
    captions = (view.captions or {}).get("status")
    if rejecting:
        return Decision("reject", f"flagged: {', '.join(rejecting)}", exc)
    if captions == "no_speech":
        return Decision("reject", "no speech", exc)
    if not view.top_messages:
        # No chat data (a clip cut by hand, a missing spike): no evidence, not a reject.
        return Decision("review", "no chat data", exc)
    if exc < settings.reject_below_excitement:
        return Decision("reject", f"chat not excited ({exc:.0%}: {chat})", exc)

    missing: list[str] = []
    if verdict is not None:
        # Gemini watched it: its score replaces the chat thresholds, and it checks the
        # screen for personal information itself (so an "alerts" flag no longer holds).
        seen = f"Gemini {verdict.score}/10: {verdict.happens}"
        if verdict.score <= settings.ai_reject:
            return Decision("reject", seen, exc)
        if verdict.personal_info:
            missing.append("Gemini saw personal information on screen")
        if verdict.score < settings.ai_approve:
            missing.append(f"below {settings.ai_approve}/10")
    else:
        seen = f"excited chat ({exc:.0%}: {chat}), score {view.score}"
        if kinds:
            missing.append(f"flagged: {', '.join(kinds)}")
        if exc < settings.approve_excitement:
            missing.append(f"excitement {exc:.0%} < {settings.approve_excitement:.0%}")
        if (view.score or 0.0) < settings.approve_score:
            missing.append(f"score {view.score} < {settings.approve_score:g}")
    if permission not in ("granted", "credit_only"):
        missing.append(f"streamer permission {permission}")
    if not has_account:
        missing.append("no account for its language")
    if captions != "ok":
        missing.append(f"captions {captions or 'missing'}")
    if approved_today >= settings.daily_cap:
        missing.append(f"daily cap of {settings.daily_cap} reached")
    if missing:
        prefix = f"{seen}; " if verdict is not None else ""
        return Decision("review", prefix + "; ".join(missing), exc)
    return Decision("approve", seen, exc)


def _approved_today(conn: sqlite3.Connection, account: str, now: float) -> int:
    day = dt.datetime.fromtimestamp(now, dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT count(*) FROM clips WHERE json_extract(auto, '$.decision') = 'approve' "
        "AND json_extract(auto, '$.mode') != 'translation' "  # copies are not auto-approvals
        "AND json_extract(auto, '$.account') = ? AND json_extract(auto, '$.at') >= ?",
        (account, day.timestamp()),
    ).fetchone()
    return int(row[0])


def auto_review_once(
    config: Config, conn: sqlite3.Connection, *, now: float | None = None
) -> list[tuple[int, Decision]]:
    """Decide every finished, undecided new clip; apply the decisions in ``on`` mode.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    now : float, optional
        Epoch seconds, default the current time.

    Returns
    -------
    list of (int, Decision)
        Clip id and decision, in clip order. Each clip is decided once; a person's
        later click overrides it as usual.

    Notes
    -----
    A clip a person already reviewed is never touched: only state ``new`` is read,
    and it is read again right before a decision is applied. An apply that fails
    (a takedown, an unreadable blocklist) is recorded as ``review`` with the error;
    so is any other error for one clip, which never stops the pass (publishing runs
    right after it). Clips decided in ``shadow`` mode are decided again in ``on``
    mode, so switching it on also handles the waiting queue.
    """
    settings = config.review.auto
    if settings.mode == "off":
        return []
    now = time.time() if now is None else now
    undecided = "auto IS NULL"
    if settings.mode == "on":
        undecided = "(auto IS NULL OR json_extract(auto, '$.mode') = 'shadow')"
    ids = [
        int(r[0])
        for r in conn.execute(
            f"SELECT id FROM clips WHERE state = 'new' AND {undecided} AND flags IS NOT NULL "  # noqa: S608 - constant text
            "AND duplicate_of IS NULL AND purged_at IS NULL ORDER BY start_ts"
        )
    ]
    judge = _gemini(config)
    budget = [settings.ai_per_pass]
    done: list[tuple[int, Decision]] = []
    for clip_id in ids:
        try:
            outcome = _decide_one(config, conn, clip_id, now, judge, budget)
        except Exception as exc:  # one broken clip must not stop the pass or publishing
            log.exception("auto review failed", clip_id=clip_id)
            conn.rollback()
            outcome = (Decision("review", f"auto review failed: {exc}", 0.0), None, False, None)
        if outcome is None:
            continue
        decision, account_id, applied, verdict = outcome
        record: dict[str, Any] = {
            "mode": settings.mode,
            "decision": decision.decision,
            "reason": decision.reason,
            "excitement": round(decision.excitement, 3),
            "account": account_id,
            "applied": applied,
            "at": now,
        }
        if verdict is not None:
            record["gemini"] = {
                "model": verdict.model,
                "score": verdict.score,
                "happens": verdict.happens,
                "title": verdict.title,
                "description": verdict.description,
                "hashtags": list(verdict.hashtags),
                "personal_info": verdict.personal_info,
            }
        conn.execute(
            "UPDATE clips SET auto = ? WHERE id = ?",
            (json.dumps(record, ensure_ascii=False), clip_id),
        )
        conn.commit()
        log.info("auto review", clip_id=clip_id, **record)
        done.append((clip_id, decision))
    return done


Judge = Callable[[sqlite3.Row, ClipView, str | None], Verdict]


def _gemini(config: Config) -> Judge | None:
    """Return a Gemini judge, or ``None`` without a key, models or ffmpeg."""
    settings = config.review.auto
    key = load_key() if settings.gemini_models else None
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if not key or ffmpeg is None:
        return None

    def judge(row: sqlite3.Row, view: ClipView, game: str | None) -> Verdict:
        chat = ", ".join(f"{text} x{count}" for text, count in view.top_messages[:5])
        return judge_clip(
            Path(row["path"]), ffmpeg=ffmpeg, models=settings.gemini_models,
            channel=view.channel, game=game, chat=chat, key=key,
        )  # fmt: skip

    return judge


def _decide_one(
    config: Config,
    conn: sqlite3.Connection,
    clip_id: int,
    now: float,
    judge: Judge | None = None,
    budget: list[int] | None = None,
) -> tuple[Decision, str | None, bool, Verdict | None] | None:
    """Decide one clip and apply it in ``on`` mode.

    Returns ``None`` when the clip is no longer new, or when it waits for Gemini
    (busy, or this pass's ``ai_per_pass`` calls are used) and is tried next pass.
    """
    settings = config.review.auto
    row = get_clip(conn, clip_id)
    if row is None or row["state"] != "new":
        return None
    view = view_of(row, conn)
    account = clip_route(config, view, row).account
    account_id = account.id if account is not None else None
    facts = clip_facts(config, view, row)
    permission = facts.channel.permission
    approved_today = _approved_today(conn, account_id, now) if account_id else 0
    has_account = account is not None

    def rules(verdict: Verdict | None) -> Decision:
        return decide(
            view, settings=settings, permission=permission, has_account=has_account,
            approved_today=approved_today, verdict=verdict,
        )  # fmt: skip

    decision = rules(None)
    verdict: Verdict | None = None
    # The rules reject polls, bots and music without spending a Gemini request.
    if decision.decision != "reject" and judge is not None and budget is not None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            verdict = judge(row, view, facts.game)
        except GeminiError as exc:
            if now - float(row["created_at"]) < settings.ai_wait_min * 60:
                log.info("gemini unavailable; clip waits", clip_id=clip_id, error=str(exc)[:200])
                return None
            note = f" (Gemini unavailable: {str(exc)[:120]})"
            decision = Decision(decision.decision, decision.reason + note, decision.excitement)
        else:
            decision = rules(verdict)
            # Gemini's title and description fill in what is still empty, in any mode,
            # so a person approving by hand gets them too (and can edit them).
            conn.execute(
                "UPDATE clips SET title = coalesce(nullif(title, ''), ?), "
                "description = coalesce(nullif(description, ''), ?) WHERE id = ?",
                (verdict.title or None, verdict.description or None, clip_id),
            )
            conn.commit()
    # Rejections apply in shadow mode too (apply_rejects): a clip whose chat did not
    # react, or that Gemini scored below 5, is not worth a person's time or the disk.
    applies = settings.mode == "on" or (decision.decision == "reject" and settings.apply_rejects)
    if not applies or decision.decision == "review":
        return decision, account_id, False, verdict
    # A person may have clicked since the clip was read: never overwrite that.
    current = conn.execute("SELECT state FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if current is None or current["state"] != "new":
        return None
    try:
        update_clip(
            config,
            conn,
            clip_id,
            state="approved" if decision.decision == "approve" else "rejected",
        )
    except ReviewError as exc:
        reason = f"could not {decision.decision}: {exc}"
        return Decision("review", reason, decision.excitement), account_id, False, verdict
    return decision, account_id, True, verdict
