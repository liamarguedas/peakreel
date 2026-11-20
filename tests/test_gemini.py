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
    writing: list[bool] = []
    monkeypatch.setattr(auto, "resolve_tool", lambda *_a: Path("ffmpeg"))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(auto, "render_clip", lambda *_a, **_k: writing.append(conn.in_transaction))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    auto.auto_review_once(config, conn)
    assert writing == [False]  # rendered with no write open: the pipeline can save clips meanwhile
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


def test_sexual_content_is_never_posted(
    review: tuple[Config, TestClient, int],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gemini's top score does not save it: Instagram blocks accounts for it.
    naked = Verdict(10, "The character showers naked.", "lol", "Funny", sexual_content=True)
    assert verdict(clip(), verdict=naked)[0] == "reject"
    assert "sexual content" in verdict(clip(), verdict=naked)[1]

    # A clip a person approved before Gemini watched it: its held posts are cancelled.
    config, client, clip_id = review
    gemini.save_key("test-key")
    set_chat(config, clip_id, [["KEKW", 20], ["what", 20]], 4.0)
    conn = connect(config.db_path)
    conn.execute("UPDATE clips SET auto = NULL WHERE id = ?", (clip_id,))
    conn.commit()
    assert client.post(f"/api/clips/{clip_id}", json={"state": "approved"}).status_code == 200
    monkeypatch.setattr(auto, "gemini_judge", lambda _config, _conn: lambda *_a: naked)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    auto.auto_review_once(config, conn)
    states = {
        r["state"]
        for r in conn.execute("SELECT state FROM publications WHERE clip_id = ?", (clip_id,))
    }
    assert states == {"cancelled"}
    assert json.loads(
        conn.execute("SELECT auto FROM clips WHERE id = ?", (clip_id,)).fetchone()[0]
    )["gemini"]["sexual_content"]
    conn.close()


def test_models_out_of_quota_are_skipped_until_their_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    gemini._EXHAUSTED.clear()  # pyright: ignore[reportPrivateUsage]
    calls: list[str] = []
    clock = [1000.0]
    monkeypatch.setattr(gemini.time, "time", lambda: clock[0])
    monkeypatch.setattr(gemini, "load_key", lambda: "g-key")
    monkeypatch.setattr(gemini, "load_openrouter_key", lambda: "or-key")
    out = {"gemini-a", "openrouter/vendor/free-model"}  # both out at first

    def refuse(model: str) -> None:
        calls.append(model)
        if model in out:
            error = GeminiError("HTTP 429: Please retry in 2h0m0s")
            error.quota, error.retry_after_s = True, gemini._retry_after("retry in 2h0m0s")  # pyright: ignore[reportPrivateUsage]
            raise error

    def gemini_post(url: str, key: str, model: str, *_a: object) -> dict[str, Any]:
        refuse(model)
        return {"by": model}

    def router_post(key: str, model: str, *_a: object) -> dict[str, Any]:
        refuse(gemini.OPENROUTER_PREFIX + model)
        return {"by": "router"}

    monkeypatch.setattr(gemini, "_post", gemini_post)
    monkeypatch.setattr(gemini, "_post_openrouter", router_post)
    models = ["gemini-a", "openrouter/vendor/free-model", "gemini-b"]
    ask = lambda: gemini.ask_json([], {}, models=models)  # noqa: E731
    assert ask() == ({"by": "gemini-b"}, "gemini-b")  # down the list
    calls.clear()
    assert ask()[1] == "gemini-b" and calls == ["gemini-b"]  # the out ones are not asked
    assert set(gemini.exhausted_until()) == {"gemini-a", "openrouter/vendor/free-model"}
    out.clear()
    clock[0] += 2 * 3600 + 1  # their reset: back to the first in line
    assert ask()[1] == "gemini-a" and gemini.exhausted_until() == {}
    gemini._EXHAUSTED.clear()  # pyright: ignore[reportPrivateUsage]


def test_openrouter_request_and_answer() -> None:
    seen: dict[str, Any] = {}
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            seen["auth"] = self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if state["status"] == 402:
                self.send_response(402)
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "requires at least $1.00 in balance"}}')
                return
            if state["status"] == 503:  # busy upstream: a 429 with no reset time
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "temporarily rate-limited upstream"}}')
                return
            if state["status"] == 429:
                self.send_response(429)
                self.send_header("X-RateLimit-Reset", "9999999999000")
                self.end_headers()
                self.wfile.write(
                    b'{"error": {"message": "Rate limit exceeded: free-models-per-day"}}'
                )
                return
            reply = {"choices": [{"message": {"content": '```json\n{"score": 7}\n```'}}]}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    parts = [
        {"type": "video", "data": "QUJD", "mime_type": "video/mp4"},
        {"type": "text", "text": "Judge."},
    ]
    try:
        post = gemini._post_openrouter  # pyright: ignore[reportPrivateUsage]
        answer = post("or-key", "vendor/m:free", parts, {"type": "object"}, 10, url)
        assert answer == {"score": 7}  # the code fence around the JSON is dropped
        assert seen["auth"] == "Bearer or-key" and seen["body"]["model"] == "vendor/m:free"
        content = seen["body"]["messages"][0]["content"]
        assert content[0] == {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,QUJD"},
        }
        state["status"] = 429
        with pytest.raises(GeminiError) as caught:
            post("or-key", "vendor/m:free", parts, {"type": "object"}, 10, url)
        assert caught.value.quota and caught.value.retry_after_s > 3600  # until the reset header
        state["status"] = 503  # busy at the provider: a short pause, not an hour
        with pytest.raises(GeminiError) as caught:
            post("or-key", "vendor/m:free", parts, {"type": "object"}, 10, url)
        assert caught.value.quota and caught.value.retry_after_s == 120
        state["status"] = 402  # no balance: skipped for an hour, then tried again
        with pytest.raises(GeminiError) as caught:
            post("or-key", "vendor/m:free", parts, {"type": "object"}, 10, url)
        assert caught.value.quota and caught.value.retry_after_s == 3600
    finally:
        server.shutdown()
