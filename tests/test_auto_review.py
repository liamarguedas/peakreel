from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from peakreel.config import AutoReviewConfig, Config
from peakreel.db import connect
from peakreel.review import auto
from peakreel.review.auto import auto_review_once, decide
from peakreel.review.store import ClipView
from tests.test_supervisor_review import review  # noqa: F401  # pyright: ignore[reportUnusedImport]  - fixture

SETTINGS = AutoReviewConfig(approve_needs_gemini=False)  # the chat rules alone


def clip(**changes: Any) -> ClipView:
    base = ClipView(
        id=1, channel="alpha", credit="", state="new", start_utc=None, duration_s=30.0,
        score=12.0, peak_count=80, unique_chatters=60,
        top_messages=[["KEKW", 20], ["LOOOL", 10], ["what", 5]],
        title="", description="", suggested_title="", captions={"status": "ok"},
        has_vertical=True, has_thumbnail=True, exported=False, reviewed_utc=None,
        created_utc=None, flags=[], duplicate_of=None, match_strength=None,
    )  # fmt: skip
    return replace(base, **changes)


def verdict(view: ClipView, **kwargs: Any) -> tuple[str, str]:
    args: dict[str, Any] = {"permission": "credit_only", "has_account": True, "approved_today": 0}
    args.update(kwargs)
    d = decide(view, settings=SETTINGS, **args)
    return d.decision, d.reason


def test_excited_strong_clean_clip_is_approved() -> None:
    assert verdict(clip())[0] == "approve"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"flags": [{"kind": "music", "message": "music"}]}, "flagged: music"),
        ({"captions": {"status": "no_speech"}}, "no speech"),
        # Seen live: the streamer asked chat a question, nothing happened on screen.
        ({"top_messages": [["yes", 17], ["no", 9], ["yeah", 4]]}, "chat not excited (0%"),
        # Seen live (agustin51): one "bro" passes the excitement share, but every line is
        # typed once: chatting, not a moment.
        (
            {
                "top_messages": [
                    ["@alextrix95 bro si tienes esa opinion", 1],
                    ["coge dinero", 1],
                    ["dinero", 1],
                    ["dios mio...", 1],
                    ["el dineeeroooo", 1],
                ]
            },
            "no shared reaction",
        ),
    ],
)
def test_rejections(changes: dict[str, Any], reason: str) -> None:
    decision, why = verdict(clip(**changes))
    assert decision == "reject" and why.startswith(reason)


@pytest.mark.parametrize(
    ("changes", "kwargs", "missing"),
    [
        ({"score": 4.0}, {}, "score 4.0 < 8"),
        ({"top_messages": [["KEKW", 3], ["what", 7]]}, {}, "excitement 30% < 50%"),
        ({"flags": [{"kind": "blocked_words", "message": "x"}]}, {}, "flagged: blocked_words"),
        # Big streams show alerts almost constantly: a person decides, not the rules.
        ({"flags": [{"kind": "alerts", "message": "x"}]}, {}, "flagged: alerts"),
        ({}, {"permission": "unknown"}, "streamer permission unknown"),
        ({}, {"has_account": False}, "no account for its language"),
        ({}, {"approved_today": 6}, "daily cap of 6 reached"),
    ],
)
def test_borderline_clips_are_left_for_a_person(
    changes: dict[str, Any], kwargs: dict[str, Any], missing: str
) -> None:
    decision, why = verdict(clip(**changes), **kwargs)
    assert decision == "review" and missing in why


def set_chat(config: Config, clip_id: int, top: list[list[Any]], score: float) -> None:
    conn = connect(config.db_path)
    channel_id = conn.execute("SELECT channel_id FROM clips WHERE id = ?", (clip_id,)).fetchone()[0]
    cursor = conn.execute(
        "INSERT INTO spikes (channel_id, start_ts, end_ts, peak_count, unique_chatters, "
        "baseline_median, baseline_mad, score, top_messages) "
        "VALUES (?, 0, 10, 80, 60, 10, 2, ?, ?)",
        (channel_id, score, json.dumps(top)),
    )
    # The app decided the clip on start-up (without this chat): decide it again.
    conn.execute(
        "UPDATE clips SET spike_id = ?, auto = NULL WHERE id = ?", (cursor.lastrowid, clip_id)
    )
    conn.commit()
    conn.close()


def clip_state(config: Config, clip_id: int) -> tuple[str, dict[str, Any]]:
    conn = connect(config.db_path)
    row = conn.execute("SELECT state, auto FROM clips WHERE id = ?", (clip_id,)).fetchone()
    conn.close()
    return row["state"], json.loads(row["auto"])


def test_shadow_mode_applies_only_chat_rule_rejections(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, client, clip_id = review
    set_chat(config, clip_id, [["yes", 17], ["no", 9]], 9.0)  # a chat poll: rules reject
    conn = connect(config.db_path)
    assert [d.decision for _, d in auto_review_once(config, conn)] == ["reject"]
    assert auto_review_once(config, conn) == []  # decided once
    conn.close()
    state, record = clip_state(config, clip_id)
    assert (state, record["mode"], record["applied"]) == ("rejected", "shadow", True)
    assert client.get("/api/clips?state=new").json() == []  # gone from the New tab


def test_shadow_mode_can_keep_rule_rejections_as_advice(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, client, clip_id = review
    advice = AutoReviewConfig(apply_rejects=False)
    config = config.model_copy(update={"review": config.review.model_copy(update={"auto": advice})})
    set_chat(config, clip_id, [["yes", 17], ["no", 9]], 9.0)
    conn = connect(config.db_path)
    auto_review_once(config, conn)
    conn.close()
    state, record = clip_state(config, clip_id)
    assert (state, record["applied"]) == ("new", False)
    assert client.get("/api/clips?state=new").json()[0]["auto"]["decision"] == "reject"


def test_on_mode_applies_approvals_like_a_reviewer(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, _client, clip_id = review
    config = config.model_copy(
        update={
            "review": config.review.model_copy(
                update={
                    "auto": AutoReviewConfig(
                        mode="on", approve_score=5.0, approve_needs_gemini=False
                    )
                }
            ),
            "channels": [config.channels[0].model_copy(update={"permission": "granted"})],
        }
    )
    set_chat(config, clip_id, [["KEKW", 20], ["LOOOL", 10]], 9.0)
    conn = connect(config.db_path)
    conn.execute(
        "UPDATE clips SET flags = '[]' WHERE id = ?", (clip_id,)
    )  # finished, no safety flags
    conn.commit()
    (decided,) = auto_review_once(config, conn)
    conn.close()
    state, auto = clip_state(config, clip_id)
    assert decided[1].decision == "approve", decided[1].reason
    assert (state, auto["applied"]) == ("approved", True)


def turned_on(config: Config) -> Config:
    return config.model_copy(
        update={
            "review": config.review.model_copy(
                update={
                    "auto": AutoReviewConfig(
                        mode="on", approve_score=5.0, approve_needs_gemini=False
                    )
                }
            ),
            "channels": [config.channels[0].model_copy(update={"permission": "granted"})],
        }
    )


def test_one_failing_clip_never_stops_the_pass(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _client, clip_id = review
    config = turned_on(config)
    set_chat(config, clip_id, [["KEKW", 20], ["LOOOL", 10]], 9.0)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auto, "update_clip", broken)
    conn = connect(config.db_path)
    (decided,) = auto_review_once(config, conn)
    assert decided[1].decision == "review" and "disk full" in decided[1].reason
    assert auto_review_once(config, conn) == []  # recorded: not retried every pass
    conn.close()


def test_a_click_during_the_decision_wins(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _client, clip_id = review
    config = turned_on(config)
    set_chat(config, clip_id, [["yes", 17], ["no", 9]], 9.0)  # the rules would reject
    real_decide = auto.decide

    def slow_decide(view: ClipView, **kwargs: Any) -> auto.Decision:
        person = connect(config.db_path)  # the reviewer approves on the phone meanwhile
        person.execute("UPDATE clips SET state = 'approved' WHERE id = ?", (clip_id,))
        person.commit()
        person.close()
        return real_decide(view, **kwargs)

    monkeypatch.setattr(auto, "decide", slow_decide)
    conn = connect(config.db_path)
    assert auto_review_once(config, conn) == []
    conn.close()
    conn = connect(config.db_path)
    assert conn.execute("SELECT state FROM clips WHERE id = ?", (clip_id,)).fetchone()[0] == (
        "approved"
    )
    conn.close()


def test_switching_on_applies_shadow_decisions(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, _client, clip_id = review
    set_chat(config, clip_id, [["KEKW", 20], ["LOOOL", 10]], 9.0)
    conn = connect(config.db_path)
    # Shadow only advises on it (unknown streamer permission: left for a person) ...
    assert [d.decision for _, d in auto_review_once(config, conn)] == ["review"]
    # ... and switching on decides it again, with this config's granted permission.
    (decided,) = auto_review_once(turned_on(config), conn)
    conn.close()
    state, record = clip_state(config, clip_id)
    assert (decided[1].decision, state, record["mode"], record["applied"]) == (
        "approve", "approved", "on", True,
    )  # fmt: skip


def test_clip_without_chat_data_is_left_for_a_person() -> None:
    # A clip cut by hand has no spike: no evidence either way, so never auto-rejected.
    assert verdict(clip(top_messages=[])) == ("review", "no chat data")


def test_audit_rejection_reasons_and_taste_examples(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    from peakreel.review.evaluate import audit_summary

    config, client, clip_id = review
    set_chat(config, clip_id, [["yes", 17], ["no", 9]], 9.0)  # a chat poll: rules reject
    conn = connect(config.db_path)
    auto_review_once(config, conn)
    row = conn.execute("SELECT reviewed_by FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert row["reviewed_by"] == "auto"
    # The Check tab shows auto rejections no person has looked at.
    assert [c["id"] for c in client.get("/api/clips?state=audit").json()] == [clip_id]
    assert auto.taste_examples(conn) == ""  # auto review's own decisions teach nothing
    assert audit_summary(conn) == (0, 0)

    # A person confirms it, with a reason: it leaves the Check tab and becomes an example.
    body = client.post(
        f"/api/clips/{clip_id}", json={"state": "rejected", "reason": "boring"}
    ).json()
    assert (body["reviewed_by"], body["review_reason"]) == ("person", "boring")
    assert client.get("/api/clips?state=audit").json() == []
    assert audit_summary(conn) == (0, 1)
    taste = auto.taste_examples(conn)
    assert taste.startswith("- REJECTED (nothing happens) · ") and "chat: yes x17, no x9" in taste
    assert auto.taste_examples(conn, exclude=clip_id) == ""  # never shows the judged clip

    # Another state clears the reason; an unknown reason is refused.
    assert (
        client.post(f"/api/clips/{clip_id}", json={"state": "new"}).json()["review_reason"] is None
    )
    bad = client.post(f"/api/clips/{clip_id}", json={"state": "rejected", "reason": "meh"})
    assert bad.status_code == 422
    conn.close()


def test_only_gemini_approves_by_default() -> None:
    view = clip()
    assert verdict(view)[0] == "approve"  # the chat rules alone would approve it...
    held = decide(view, settings=AutoReviewConfig(), permission="granted", has_account=True,
                  approved_today=0)  # fmt: skip
    assert (held.decision, held.reason) == ("review", "no Gemini verdict (only Gemini may approve)")
