from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from peakreel.captions.ass import Word, build_ass, caption_margins, chunk_words, format_time
from peakreel.captions.whisper import Captioner, CaptionResult
from peakreel.config import CaptionsConfig, ConfigError, OutputConfig, load_config, resolve_tool
from peakreel.render.vertical import font_family, render_clip, resolve_font
from tests.conftest import WriteConfig

FFMPEG = resolve_tool("ffmpeg", Path.cwd())


def w(start: float, end: float, text: str) -> Word:
    return Word(start, end, text)


def test_chunking_rules() -> None:
    words = [
        w(0.0, 0.2, " no"),
        w(0.2, 0.4, " way"),
        w(0.4, 0.7, " he hit it."),  # sentence end closes the chunk
        w(0.8, 1.0, " what"),
        w(2.5, 2.7, " wow"),  # pause > 0.8 s
        w(2.7, 2.9, " "),  # empty words are dropped
    ]
    chunks = chunk_words(words, max_width=40)
    assert [[x.text for x in c.words] for c in chunks] == [
        ["no", "way", "he hit it."],
        ["what"],
        ["wow"],
    ]
    narrow = chunk_words([w(0, 0.1, "abcdef"), w(0.1, 0.2, "ghijkl")], max_width=10)
    assert len(narrow) == 2


def test_cjk_counts_double_and_joins_without_spaces() -> None:
    words = [w(i * 0.1, i * 0.1 + 0.1, ch) for i, ch in enumerate("すっごい悪いかな俺のせい")]
    chunks = chunk_words(words, max_width=20, joiner="")
    assert ["".join(x.text for x in c.words) for c in chunks] == ["すっごい悪いかな俺の", "せい"]


def test_format_time() -> None:
    assert format_time(0) == "0:00:00.00"
    assert format_time(3725.456) == "1:02:05.46"
    assert format_time(-1) == "0:00:00.00"


def test_build_ass_highlights_each_word_and_escapes() -> None:
    settings, output = CaptionsConfig(), OutputConfig()
    chunks = chunk_words(
        [w(1.0, 1.2, "{bad}"), w(1.3, 1.5, "word"), w(5.0, 5.2, "next")], max_width=40
    )
    ass = build_ass(chunks, font_name="Yu Gothic", output=output, settings=settings)
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "Style: Caption,Yu Gothic,64," in ass
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 3
    # First word highlighted (yellow #FFE135 is &H0035E1FF in ASS BGR), lasts until the next word.
    assert (
        events[0] == r"Dialogue: 0,0:00:01.00,0:00:01.30,Caption,,0,0,0,,"
        r"{\c&H0035E1FF}(bad){\c&H00FFFFFF} word"
    )
    assert events[1].startswith("Dialogue: 0,0:00:01.30,0:00:01.80,")  # last word lingers 0.3 s
    assert r"(bad) {\c&H0035E1FF}word" in events[1]
    assert "{" not in events[0].split(",,", 1)[1].replace(r"{\c", "")


def test_caption_margins_stay_in_safe_area() -> None:
    output, settings = OutputConfig(), CaptionsConfig()
    left, right, vertical = caption_margins(output, settings)
    assert (left, right) == (130 + 48, 130 + 48)  # equal: centred on the frame
    baseline = output.height - vertical
    assert baseline <= output.height * (1 - output.safe_zone_bottom)
    assert baseline == 1344


def test_captions_in_unsafe_zone_are_rejected(write_config: WriteConfig) -> None:
    with pytest.raises(ConfigError, match="bottom unsafe zone"):
        load_config(write_config({"captions": {"bottom_fraction": 0.9}}))


# -- Captioner with a fake model -----------------------------------------------------


class FakeModel:
    def __init__(self, words: list[tuple[float, float, str]], language: str = "en") -> None:
        self.words = words
        self.language = language
        self.calls = 0

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[Any], Any]:
        self.calls += 1
        assert kwargs["word_timestamps"] is True
        assert kwargs["beam_size"] == 5
        segment = SimpleNamespace(
            words=[SimpleNamespace(start=s, end=e, word=t) for s, e, t in self.words]
        )
        return [segment], SimpleNamespace(language=self.language)


def captioner(
    tmp_path: Path, model: FakeModel | None, monkeypatch: pytest.MonkeyPatch
) -> Captioner:
    import peakreel.captions.whisper as whisper

    def fake_audio(clip: Path, ffmpeg: Path) -> np.ndarray[Any, np.dtype[np.float32]]:
        return np.ones(16000, dtype=np.float32)

    def no_model(*args: object, **kwargs: object) -> object:
        raise RuntimeError("no model")

    monkeypatch.setattr(whisper, "decode_audio", fake_audio)
    cap = Captioner(
        CaptionsConfig(), OutputConfig(), tmp_path / "models", Path("ffmpeg"), "Yu Gothic"
    )
    if model is None:
        monkeypatch.setattr(whisper, "load_model", no_model)
    else:
        object.__setattr__(cap, "_model", model)  # a fake stands in for WhisperModel
    return cap


def test_caption_ok_writes_ass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"")
    cap = captioner(
        tmp_path, FakeModel([(0.0, 0.2, " a"), (0.2, 0.4, " b"), (0.4, 0.6, " c")]), monkeypatch
    )
    result = cap.caption_sync(clip)
    assert result.status == "ok"
    assert result.ass_path == tmp_path / "c.ass"
    assert result.words == 3
    assert result.language == "en"
    assert result.to_sidecar()["file"] == "c.ass"
    cap.close()


def test_too_few_words_is_no_speech(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "c.mp4"
    cap = captioner(tmp_path, FakeModel([(0.0, 0.2, " uh")]), monkeypatch)
    result = cap.caption_sync(clip)
    assert result.status == "no_speech"
    assert result.ass_path is None
    assert not (tmp_path / "c.ass").exists()
    cap.close()


def test_model_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = captioner(tmp_path, None, monkeypatch)
    result = cap.caption_sync(tmp_path / "c.mp4")
    assert result.status == "failed"
    assert result.error is not None
    assert "no model" in result.error
    cap.close()


async def test_worker_runs_one_clip_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModel([(0.0, 0.2, " a"), (0.2, 0.4, " b"), (0.4, 0.6, " c")])
    cap = captioner(tmp_path, model, monkeypatch)
    running = 0
    peak = 0
    original = cap.caption_sync

    def tracked(clip: Path) -> CaptionResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            return original(clip)
        finally:
            running -= 1

    monkeypatch.setattr(cap, "caption_sync", tracked)
    results = await asyncio.gather(*(cap.caption(tmp_path / f"c{i}.mp4") for i in range(4)))
    assert [r.status for r in results] == ["ok"] * 4
    assert peak == 1
    assert cap.backlog == 0
    assert model.calls == 4
    cap.close()


# -- burn-in with real ffmpeg + libass -------------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_render_burns_in_captions_and_records_them(
    write_config: WriteConfig, tmp_path: Path
) -> None:
    assert FFMPEG is not None
    config = load_config(
        write_config({"channels": [{"name": "alpha"}], "render": {"preset": "ultrafast"}})
    )
    clip = tmp_path / "c.mp4"
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=black:size=1920x1080:rate=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:a", "aac", str(clip)],
        check=True,
    )  # fmt: skip
    clip.with_suffix(".json").write_text(json.dumps({"channel": "alpha"}), encoding="utf-8")
    family = font_family(resolve_font(None))
    chunks = chunk_words([w(0.0, 1.0, "HELLO"), w(1.0, 2.0, "WORLD")], max_width=40)
    ass = clip.with_suffix(".ass")
    ass.write_text(
        build_ass(chunks, font_name=family, output=config.output, settings=config.captions),
        encoding="utf-8",
    )
    captions = CaptionResult("ok", ass, "en", 2, 0.1)
    result = render_clip(config, clip, FFMPEG, captions=captions)

    # The caption band (around the baseline at 1344 px) must contain bright text pixels
    # on the black background; the band between the credit (top) and it must not.
    def band_max(y: int, h: int) -> int:
        assert FFMPEG is not None
        raw = subprocess.run(
            [str(FFMPEG), "-v", "error", "-ss", "0.5", "-i", str(result.vertical), "-frames:v", "1",
             "-vf", f"crop=1080:{h}:0:{y},format=gray", "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout  # fmt: skip
        return max(raw)

    assert band_max(1270, 80) > 200
    assert band_max(700, 200) < 60
    sidecar = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["captions"]["status"] == "ok"
    assert sidecar["captions"]["file"] == "c.ass"
