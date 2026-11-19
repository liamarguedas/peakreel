from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from peakreel.config import AutoReviewConfig, Config
from peakreel.db import connect
from peakreel.review import auto, gemini
from peakreel.review.gemini import GeminiError, Verdict, judge_clip
from tests.test_auto_review import clip, clip_state, set_chat, turned_on, verdict
from tests.test_supervisor_review import review  # noqa: F401  # pyright: ignore[reportUnusedImport]  - fixture

ANSWER = {
    "score": 8, "happens": "He crashes into the divider.", "title": "Instant crash",
    "description": "Two seconds of driving.", "reason": "Funny fail", "personal_info": False,
}  # fmt: skip


@pytest.fixture
def server() -> Iterator[tuple[list[dict[str, Any]], list[int], str]]:
    """A fake Interactions endpoint: answers with `statuses` in order, then 200."""
    seen: list[dict[str, Any]] = []
    statuses: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"model": body["model"], "key": self.headers["x-goog-api-key"]})
            code = statuses.pop(0) if statuses else 200
            data = (
                {"output_text": json.dumps(ANSWER)}
                if code == 200
                else {"error": {"message": "high demand", "code": "service_unavailable"}}
            )
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield seen, statuses, f"http://127.0.0.1:{httpd.server_port}/v1beta/interactions"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_copy(_ffmpeg: Path, _clip: Path, out: Path) -> None:
        out.write_bytes(b"mp4")

    monkeypatch.setattr(gemini, "_small_copy", fake_copy)


def test_busy_model_falls_back_to_the_next(
    server: tuple[list[dict[str, Any]], list[int], str], tmp_path: Path
) -> None:
    seen, statuses, url = server
    statuses.append(503)  # seen live on launch day: "high demand"
    result = judge_clip(
        tmp_path / "c.mp4", ffmpeg=Path("ffmpeg"), models=["busy", "free"],
        channel="speed", game="GTA VI", chat="lol x9", key="k", url=url,
    )  # fmt: skip
    assert [s["model"] for s in seen] == ["busy", "free"] and seen[0]["key"] == "k"
    assert (result.score, result.title, result.model) == (8, "Instant crash", "free")

    statuses.extend([503, 503])
    with pytest.raises(GeminiError, match=r"busy: .*503.*free: .*503"):
        judge_clip(
            tmp_path / "c.mp4", ffmpeg=Path("ffmpeg"), models=["busy", "free"],
            channel="speed", game=None, chat="", key="k", url=url,
        )  # fmt: skip


def test_gemini_score_decides_instead_of_chat_thresholds() -> None:
    weak_chat = {"score": 4.0, "flags": [{"kind": "alerts", "message": "sub alert"}]}
    seen = Verdict(8, "He crashes.", "Crash", "Funny")
    assert verdict(clip(**weak_chat), verdict=seen)[0] == "approve"
    assert verdict(clip(), verdict=Verdict(2, "He walks.", "Walk", "Nothing"))[0] == "reject"
    private = Verdict(9, "Crash.", "Crash", "Funny", personal_info=True)
    decision, why = verdict(clip(), verdict=private)
    assert decision == "review" and "personal information" in why


def test_auto_review_waits_for_a_busy_gemini_then_fills_title_and_description(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _client, clip_id = review
    config = turned_on(config)
    set_chat(config, clip_id, [["KEKW", 20], ["what", 20]], 4.0)  # chat alone: review
    answers: list[Verdict | GeminiError] = [
        GeminiError("all busy"),
        Verdict(8, "Crash.", "Instant crash", "Funny", description="Watch this."),
    ]

    def judge(*_args: object) -> Verdict:
        answer = answers.pop(0)
        if isinstance(answer, GeminiError):
            raise answer
        return answer

    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: judge)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    conn = connect(config.db_path)
    assert auto.auto_review_once(config, conn) == []  # busy and young: waits, not recorded
    (decided,) = auto.auto_review_once(config, conn)
    conn.close()
    assert decided[1].decision == "approve"
    state, record = clip_state(config, clip_id)
    assert (state, record["gemini"]["score"]) == ("approved", 8)
    conn = connect(config.db_path)
    row = conn.execute("SELECT title, description FROM clips WHERE id = ?", (clip_id,)).fetchone()
    conn.close()
    assert (row["title"], row["description"]) == ("Instant crash", "Watch this.")


def test_rules_reject_without_spending_a_gemini_request(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _client, clip_id = review
    config = config.model_copy(
        update={"review": config.review.model_copy(update={"auto": AutoReviewConfig()})}
    )
    set_chat(config, clip_id, [["yes", 17], ["no", 9]], 9.0)
    calls: list[object] = []

    def judge(*args: object) -> None:
        calls.append(args)

    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: judge)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    conn = connect(config.db_path)
    assert [d.decision for _, d in auto.auto_review_once(config, conn)] == ["reject"]
    conn.close()
    assert calls == []


def test_verdict_fills_card_text_and_moment_hashtags_reach_the_post(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, client, clip_id = review  # shadow mode: nothing is applied
    set_chat(config, clip_id, [["KEKW", 20], ["what", 20]], 4.0)
    seen = Verdict(6, "Crash.", "Instant crash", "Funny", "Watch this.", ("carcrash", "GTA6Fails"))

    def judge(*_args: object) -> Verdict:
        return seen

    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: judge)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    conn = connect(config.db_path)
    auto.auto_review_once(config, conn)
    conn.close()
    card = client.get("/api/clips?state=new").json()[0]
    assert (card["state"], card["title"], card["description"]) == (
        "new", "Instant crash", "Watch this.",
    )  # fmt: skip
    text = client.get(f"/api/clips/{clip_id}/posts").json()["posts"]["instagram"]["meta"]["text"]
    assert "#carcrash" in text and "#GTA6Fails" in text and "Watch this." in text


@pytest.mark.parametrize(("score", "state"), [(4, "rejected"), (5, "new")])
def test_shadow_mode_removes_gemini_scores_below_five(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    score: int,
    state: str,
) -> None:
    config, _client, clip_id = review  # shadow mode, the default
    set_chat(config, clip_id, [["KEKW", 20], ["what", 20]], 4.0)

    def judge(*_args: object) -> Verdict:
        return Verdict(score, "Walks around.", "Walk", "Nothing much")

    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: judge)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    conn = connect(config.db_path)
    auto.auto_review_once(config, conn)
    conn.close()
    assert clip_state(config, clip_id)[0] == state


def test_auc_and_judge_evaluation_never_show_the_answer() -> None:
    from peakreel.review.evaluate import JudgeEval, auc

    assert auc([8, 7, 6], [3, 6]) == pytest.approx(11 / 12)
    assert auc([5], []) is None
    result = JudgeEval([(1, True, 8), (2, True, 6), (3, False, 6), (4, False, 2)], errors=1)
    assert result.auc == pytest.approx(0.875)
    assert result.approved_at(7) == (1, 0)


def test_posts_wait_for_gemini_text_when_a_person_approves_first(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    config, client, clip_id = review
    gemini.save_key("test-key")
    set_chat(config, clip_id, [["KEKW", 20], ["what", 20]], 4.0)
    conn = connect(config.db_path)
    conn.execute("UPDATE clips SET auto = NULL WHERE id = ?", (clip_id,))
    conn.commit()
    body = client.post(
        f"/api/clips/{clip_id}", json={"state": "approved", "title": "My own title"}
    ).json()
    assert body["state"] == "approved"
    held = conn.execute(
        "SELECT platform, next_attempt_at, metadata FROM publications "
        "WHERE clip_id = ? AND state = 'approved'",
        (clip_id,),
    ).fetchall()
    assert held and all(r["next_attempt_at"] > time.time() + 9 * 60 for r in held)  # waits

    answer = Verdict(8, "Crash.", "Gemini title", "Funny", description="Watch this crash.")
    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: lambda *_a: answer)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    auto.auto_review_once(config, conn)
    row = conn.execute(
        "SELECT title, description, auto FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()
    assert (row["title"], row["description"]) == ("My own title", "Watch this crash.")  # typed kept
    assert json.loads(row["auto"])["gemini"]["score"] == 8
    released = conn.execute(
        "SELECT next_attempt_at, metadata FROM publications "
        "WHERE clip_id = ? AND state = 'approved'",
        (clip_id,),
    ).fetchall()
    assert released and all(r["next_attempt_at"] <= time.time() for r in released)
    assert all("Watch this crash." in json.loads(r["metadata"])["text"] for r in released)
    conn.close()


def test_gemini_title_is_rendered_into_the_video_unless_rejected(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _client, clip_id = review
    rendered: list[str | None] = []
    monkeypatch.setattr(auto, "resolve_tool", lambda *_a: Path("ffmpeg"))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(auto, "render_clip", lambda *_a, title=None, **_k: rendered.append(title))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    set_chat(config, clip_id, [["KEKW", 20], ["what", 20]], 4.0)
    weak = Verdict(3, "Nothing.", "meh", "Boring")
    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: lambda *_a: weak)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    conn = connect(config.db_path)
    (decided,) = auto.auto_review_once(config, conn)
    assert decided[1].decision == "reject" and rendered == []  # no render for a rejected clip

    conn.execute("UPDATE clips SET state = 'new', auto = NULL WHERE id = ?", (clip_id,))
    conn.commit()
    good = Verdict(8, "Crash.", "bro did NOT see that", "Funny", description="lol")
    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: lambda *_a: good)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    auto.auto_review_once(config, conn)
    conn.close()
    assert rendered == ["bro did NOT see that"]
