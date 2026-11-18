from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from peakreel.config import Config, PublishAccount
from peakreel.db import connect
from peakreel.review import translate
from peakreel.review.store import update_clip
from peakreel.review.translate import read_ass, translate_once
from tests.test_supervisor_review import review  # noqa: F401  # pyright: ignore[reportUnusedImport]  - fixture

ASS = (
    "[Script Info]\nPlayResX: 1080\n\n[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:00.05,0:00:00.43,Caption,,0,0,0,,{\\c&H0035E1FF}No{\\c&H00FFFFFF} way\n"
    "Dialogue: 0,0:00:00.43,0:00:00.80,Caption,,0,0,0,,No {\\c&H0035E1FF}way{\\c&H00FFFFFF}\n"
    "Dialogue: 0,0:00:01.00,0:00:01.60,Caption,,0,0,0,,he crashed\n"
)


def test_read_ass_merges_karaoke_events_into_lines() -> None:
    header, chunks = read_ass(ASS)
    assert header.endswith("Effect, Text")
    assert [(c.start, c.end, c.text) for c in chunks] == [
        ("0:00:00.05", "0:00:00.80", "No way"),
        ("0:00:01.00", "0:00:01.60", "he crashed"),
    ]


@pytest.fixture
def two_accounts(review: tuple[Config, TestClient, int]) -> tuple[Config, int]:  # noqa: F811
    config, _client, clip_id = review
    accounts = [
        PublishAccount(id="en", languages=("en",), credit="Clip from {channel_url}"),
        PublishAccount(id="es", languages=("es",), credit="Clip de {channel_url}"),
    ]
    config = config.model_copy(
        update={"publish": config.publish.model_copy(update={"accounts": accounts})}
    )
    folder = config.outbox_dir / "alpha"
    (folder / "c1.ass").write_text(ASS, encoding="utf-8")
    (folder / "c1.json").write_text(
        json.dumps({"channel": "alpha", "captions": {"status": "ok", "language": "en",
                                                     "file": "c1.ass"}}),
        encoding="utf-8",
    )  # fmt: skip
    conn = connect(config.db_path)
    conn.execute("UPDATE clips SET language = 'en', flags = '[]' WHERE id = ?", (clip_id,))
    conn.commit()
    update_clip(config, conn, clip_id, state="approved", title="No way he crashed")
    conn.close()
    return config, clip_id


def test_approved_clip_gets_a_translated_copy_for_the_other_account(
    two_accounts: tuple[Config, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, clip_id = two_accounts
    asked: list[str] = []

    def fake_gemini(
        parts: list[dict[str, Any]], *_args: object, **_kw: object
    ) -> tuple[dict[str, Any], str]:
        asked.append(parts[0]["text"])
        answer = {"lines": ["Ni de broma", "se estrelló"], "title": "Imposible, se estrelló",
                  "description": "Mira esto.", "hashtags": ["#choque", "fail"]}  # fmt: skip
        return answer, "fake"

    rendered: dict[str, Any] = {}

    def fake_render(clip: Path, vertical: Path, thumbnail: Path, **kw: Any) -> None:
        rendered.update(kw, clip=clip)
        vertical.write_bytes(b"v")
        thumbnail.write_bytes(b"j")

    monkeypatch.setattr(translate, "ask_json", fake_gemini)
    monkeypatch.setattr(translate, "render_vertical", fake_render)
    conn = connect(config.db_path)
    made = translate_once(config, conn, ffmpeg=Path("ffmpeg"), key="k", failed={})
    assert made is not None
    source, language, copy_id = made
    assert (source, language) == (clip_id, "es")
    assert "Spanish" in asked[0] and '"No way", "he crashed"' in asked[0]
    # Burned-in credit in the target account's language; translated subtitles, same timing.
    assert rendered["credit"] == "Clip de twitch.tv/alpha"
    subtitles = Path(rendered["subtitles"]).read_text(encoding="utf-8")
    assert "0:00:00.05,0:00:00.80,Caption,,0,0,0,,Ni de broma" in subtitles
    row = conn.execute(
        "SELECT state, language, title, description, translation_of FROM clips WHERE id = ?",
        (copy_id,),
    ).fetchone()
    assert tuple(row) == ("approved", "es", "Imposible, se estrelló", "Mira esto.", clip_id)
    pubs = conn.execute("SELECT DISTINCT account FROM publications WHERE clip_id = ?", (copy_id,))
    assert [r[0] for r in pubs] == ["es"]
    assert translate_once(config, conn, ffmpeg=Path("ffmpeg"), key="k", failed={}) is None  # done
    conn.close()


def test_a_failed_copy_waits_before_retrying(
    two_accounts: tuple[Config, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, clip_id = two_accounts

    def busy(*_args: object, **_kw: object) -> tuple[dict[str, Any], str]:
        raise translate.GeminiError("high demand")

    monkeypatch.setattr(translate, "ask_json", busy)
    failed: dict[tuple[int, str], float] = {}
    conn = connect(config.db_path)
    assert (
        translate_once(config, conn, ffmpeg=Path("ffmpeg"), key="k", failed=failed, now=1e9) is None
    )
    assert set(failed) == {(clip_id, "es")}
    calls: list[object] = []
    monkeypatch.setattr(translate, "ask_json", lambda *a, **k: calls.append(a))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    translate_once(config, conn, ffmpeg=Path("ffmpeg"), key="k", failed=failed, now=1e9 + 60)
    assert calls == []  # within the retry wait
    conn.close()
