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
from typing import Any, Literal, cast

from peakreel.config import AutoReviewConfig, Config, Platform, resolve_tool
from peakreel.detect.filters import excitement, shared_reaction
from peakreel.log import get_logger
from peakreel.publish.queue import clip_facts, clip_route, queue_clip
from peakreel.render.vertical import render_clip
from peakreel.review.gemini import GeminiError, Verdict, judge_clip, load_key
from peakreel.review.store import ClipView, ReviewError, get_clip, update_clip, view_of

log = get_logger(__name__)

__all__ = [
    "Decision",
    "Judge",
    "auto_review_once",
    "decide",
    "excitement",
    "gemini_judge",
    "taste_examples",
]


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
    shared = shared_reaction(view.top_messages, settings.excitement_words)
    if shared < settings.min_shared_reaction:
        return Decision(
            "reject", f"no shared reaction, everyone typing something else ({chat})", exc
        )

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
        if settings.approve_needs_gemini:
            missing.append("no Gemini verdict (only Gemini may approve)")
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
    judge = gemini_judge(config, conn)
    budget = [settings.ai_per_pass]
    if judge is not None:
        _write_approved_text(config, conn, judge, budget, now)
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
            record["gemini"] = _gemini_record(verdict)
        conn.execute(
            "UPDATE clips SET auto = ? WHERE id = ?",
            (json.dumps(record, ensure_ascii=False), clip_id),
        )
        conn.commit()
        log.info("auto review", clip_id=clip_id, **record)
        done.append((clip_id, decision))
    return done


Judge = Callable[[sqlite3.Row, ClipView, str | None, str | None], Verdict]


def _write_approved_text(
    config: Config, conn: sqlite3.Connection, judge: Judge, budget: list[int], now: float
) -> None:
    """Write Gemini's title and description for clips a person approved first.

    :func:`peakreel.review.store.update_clip` holds their posts for ``text_wait_min``;
    here Gemini judges them ahead of new clips, fills only the empty fields (never what
    the person typed), rebuilds the post text and releases the posts at once. If Gemini
    does not answer in time, the hold simply runs out and the posts go out as they are.
    """
    settings = config.review.auto
    rows = conn.execute(
        "SELECT id FROM clips WHERE state = 'approved' AND reviewed_by = 'person' "
        "AND json_extract(auto, '$.gemini') IS NULL AND reviewed_at > ? "
        "AND (coalesce(title, '') = '' OR coalesce(description, '') = '') "
        "AND EXISTS (SELECT 1 FROM publications p WHERE p.clip_id = clips.id "
        "AND p.state = 'approved') ORDER BY reviewed_at",
        (now - settings.text_wait_min * 60,),
    ).fetchall()
    for (clip_id,) in rows:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        row = get_clip(conn, int(clip_id))
        if row is None:
            continue
        view = view_of(row, conn)
        route = clip_route(config, view, row)
        try:
            verdict = judge(row, view, clip_facts(config, view, row).game, route.language)
        except GeminiError as exc:
            log.info("gemini unavailable; approved clip waits", clip_id=clip_id,
                     error=str(exc)[:200])  # fmt: skip
            continue
        try:
            _apply_text(config, conn, int(clip_id), verdict)
        except Exception:  # one clip must not stop the pass or publishing
            log.exception("writing an approved clip's text failed", clip_id=clip_id)
            conn.rollback()


def _apply_text(config: Config, conn: sqlite3.Connection, clip_id: int, verdict: Verdict) -> None:
    conn.execute(
        "UPDATE clips SET title = coalesce(nullif(title, ''), ?), "
        "description = coalesce(nullif(description, ''), ?) WHERE id = ?",
        (verdict.title or None, verdict.description or None, clip_id),
    )
    row = _require_row(conn, clip_id)
    record: dict[str, Any] = json.loads(row["auto"]) if row["auto"] else {
        "mode": config.review.auto.mode, "decision": "review", "applied": False,
        "reason": "approved by a person before Gemini judged it",
    }  # fmt: skip
    record["gemini"] = _gemini_record(verdict)
    conn.execute(
        "UPDATE clips SET auto = ? WHERE id = ?", (json.dumps(record, ensure_ascii=False), clip_id)
    )
    _title_video(config, row, verdict.title)  # before the held posts are released
    row = _require_row(conn, clip_id)
    view = view_of(row, conn)
    pending = [
        str(r["platform"])
        for r in conn.execute(
            "SELECT platform FROM publications WHERE clip_id = ? AND state = 'approved'",
            (clip_id,),
        )
    ]
    queue_clip(config, conn, view, row, cast("list[Platform]", pending))  # new text, same slots
    conn.execute(
        "UPDATE publications SET next_attempt_at = min(next_attempt_at, ?) "
        "WHERE clip_id = ? AND state = 'approved'",
        (time.time(), clip_id),
    )
    conn.commit()
    log.info("gemini text written for an approved clip", clip_id=clip_id, title=verdict.title)


def _title_video(config: Config, row: sqlite3.Row, title: str) -> None:
    """Render the clip again with Gemini's title (the hook) in the band.

    The first render runs right after the cut, before Gemini has judged the clip.
    A failure keeps the video as it is: the clip is never held up by its title.
    """
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if not title or ffmpeg is None:
        return
    try:
        render_clip(config, Path(row["path"]), ffmpeg, title=title)
    except Exception:
        log.exception("render with the title failed; the video is kept", clip_id=row["id"])


def _require_row(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row:
    row = get_clip(conn, clip_id)
    if row is None:
        raise ReviewError(f"no clip {clip_id}")
    return row


def _gemini_record(verdict: Verdict) -> dict[str, Any]:
    return {
        "model": verdict.model,
        "score": verdict.score,
        "happens": verdict.happens,
        "title": verdict.title,
        "description": verdict.description,
        "hashtags": list(verdict.hashtags),
        "personal_info": verdict.personal_info,
    }


_REASON_TEXT = {
    "boring": "nothing happens",
    "no_context": "needs context / inside joke",
    "not_gta": "not the game",
    "bad_video": "bad picture or crop",
    "repeat": "already posted",
    "other": "other",
}


def taste_examples(
    conn: sqlite3.Connection, *, exclude: int | None = None, per_side: int = 10
) -> str:
    """Describe a person's latest review decisions, for Gemini to learn their taste.

    Parameters
    ----------
    conn : sqlite3.Connection
    exclude : int, optional
        A clip to leave out (the one being judged, so an evaluation never shows
        Gemini the answer).
    per_side : int, default 10
        Most approved and most rejected clips each.

    Returns
    -------
    str
        One line per clip (newest first per side), ``""`` without decisions. Each
        line has the channel, what happened (Gemini's summary, else the title), the
        top chat and the decision with the reject reason.

    Notes
    -----
    Only decisions by a person (``reviewed_by = 'person'``) count, never auto
    review's own. Chat text comes from viewers: it is quoted as data in the prompt
    and only ever shapes a score.
    """
    lines: list[str] = []
    for state in ("approved", "rejected"):
        rows = conn.execute(
            "SELECT c.id, ch.name AS channel, c.title, c.review_reason, c.auto, s.top_messages "
            "FROM clips c JOIN channels ch ON ch.id = c.channel_id "
            "LEFT JOIN spikes s ON s.id = c.spike_id "
            "WHERE c.state = ? AND c.reviewed_by = 'person' AND c.translation_of IS NULL "
            "AND c.id IS NOT ? ORDER BY c.reviewed_at DESC LIMIT ?",
            (state, exclude, per_side),
        ).fetchall()
        for r in rows:
            auto: dict[str, Any] = json.loads(r["auto"]) if r["auto"] else {}
            gemini: dict[str, Any] = auto.get("gemini") or {}
            happened = str(gemini.get("happens") or r["title"] or "unknown")
            chat = ", ".join(
                f"{text} x{count}" for text, count in json.loads(r["top_messages"] or "[]")[:3]
            )
            verdict = "APPROVED"
            if state == "rejected":
                verdict = "REJECTED"
                if r["review_reason"]:
                    verdict += f" ({_REASON_TEXT.get(r['review_reason'], r['review_reason'])})"
            lines.append(f"- {verdict} · {r['channel']} · {happened[:200]} · chat: {chat}")
    return "\n".join(lines)


def gemini_judge(config: Config, conn: sqlite3.Connection) -> Judge | None:
    """Return a Gemini judge taught with the reviewer's decisions.

    Returns ``None`` without a key, models or ffmpeg. The judge leaves the clip it
    judges out of the examples (:func:`taste_examples`).
    """
    settings = config.review.auto
    key = load_key() if settings.gemini_models else None
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if not key or ffmpeg is None:
        return None

    def judge(row: sqlite3.Row, view: ClipView, game: str | None, language: str | None) -> Verdict:
        chat = ", ".join(f"{text} x{count}" for text, count in view.top_messages[:5])
        return judge_clip(
            Path(row["path"]), ffmpeg=ffmpeg, models=settings.gemini_models,
            channel=view.channel, game=game, chat=chat, language=language, key=key,
            taste=taste_examples(conn, exclude=view.id),
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
    route = clip_route(config, view, row)
    account = route.account
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
            verdict = judge(row, view, facts.game, route.language)
        except GeminiError as exc:
            if now - float(row["created_at"]) < settings.ai_wait_min * 60:
                log.info("gemini unavailable; clip waits", clip_id=clip_id, error=str(exc)[:200])
                return None
            note = f" (Gemini unavailable: {str(exc)[:120]})"
            decision = Decision(decision.decision, decision.reason + note, decision.excitement)
        else:
            decision = rules(verdict)
            if decision.decision != "reject":  # posted or reviewed: show its hook
                _title_video(config, row, verdict.title)
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
            by="auto",
        )
    except ReviewError as exc:
        reason = f"could not {decision.decision}: {exc}"
        return Decision("review", reason, decision.excitement), account_id, False, verdict
    return decision, account_id, True, verdict
