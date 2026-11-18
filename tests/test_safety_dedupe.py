from __future__ import annotations

import io
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from peakreel.chat.base import ChatMessage
from peakreel.clip.clipper import ClipContext, Clipper
from peakreel.config import Config, ConfigError, DedupeConfig, load_config, resolve_tool
from peakreel.db import connect, ensure_channel, init_db, insert_clip, insert_spike
from peakreel.dedupe import (
    FINGERPRINT_ITEM_S,
    Match,
    fingerprint,
    fingerprint_path,
    load_fingerprint,
    resolve_duplicates,
    similarity,
)
from peakreel.detect.spikes import Spike
from peakreel.record.buffer import Window
from peakreel.render.vertical import RenderedClip
from peakreel.safety import words as words_module
from peakreel.safety.checks import alert_flag, music_flag, word_flags
from peakreel.safety.music import music_score
from peakreel.safety.words import WordList, download_word_lists, find_words, load_word_list
from tests.conftest import WriteConfig
from tests.test_recorder import make_session

T0 = 1_794_900_000.0
SR = 16_000
FFMPEG = resolve_tool("ffmpeg", Path.cwd())


def _has_chromaprint() -> bool:
    if FFMPEG is None:
        return False
    muxers = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-muxers"], capture_output=True, text=True, check=False
    ).stdout
    return " chromaprint " in muxers


needs_chromaprint = pytest.mark.skipif(
    not _has_chromaprint(), reason="ffmpeg with Chromaprint not installed"
)


# -- word lists -------------------------------------------------------------------------------


def lists(tmp_path: Path, user: str | None = None) -> WordList:
    directory = tmp_path / "lists"
    directory.mkdir(exist_ok=True)
    (directory / "en.txt").write_text("darn\nheck off\nass\n", encoding="utf-8")
    (directory / "ja.txt").write_text("くそ\n", encoding="utf-8")
    user_file = tmp_path / "safety.yaml"
    if user is not None:
        user_file.write_text(user, encoding="utf-8")
    return load_word_list(directory, ("en", "ja", "pt"), user_file)


def test_words_match_whole_words_and_unspaced_scripts(tmp_path: Path) -> None:
    words = lists(tmp_path)
    assert words.missing == ("pt",)
    assert find_words("DARN, heck off! classy ASS", words) == ["darn", "heck off", "ass"]
    assert find_words("darned class assets", words) == []  # whole words only
    assert find_words("まじでくそゲー", words) == ["くそ"]  # no spaces in Japanese


def test_user_words_add_and_ignore(tmp_path: Path) -> None:
    words = lists(tmp_path, "blocked_words: [Fooword]\nignore_words: [ass]\n")
    assert find_words("fooword and ass", words) == ["fooword"]
    assert words.size == 4


def test_malformed_user_words_raise(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be a list"):
        lists(tmp_path, "blocked_words: notalist\n")


def test_download_writes_lists_and_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> io.BytesIO:
        requested.append(url)
        return io.BytesIO(b"one\n\ntwo\n")

    monkeypatch.setattr(words_module.urllib.request, "urlopen", fake_urlopen)
    counts = download_word_lists(tmp_path, ("en", "pt"))
    assert counts == {"en": 2, "pt": 2}
    assert all(words_module.LDNOOBW_COMMIT in url for url in requested)
    assert (tmp_path / "en.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert "CC BY 4.0" in (tmp_path / "SOURCE.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="invalid language"):
        download_word_lists(tmp_path, ("../etc",))


def test_word_flags_report_sources_and_missing_lists(tmp_path: Path) -> None:
    flags = word_flags(lists(tmp_path), "well darn", ["gg", "ass lol"])
    assert [(f.kind, f.message) for f in flags] == [
        ("checks_incomplete", "word lists not downloaded (pt): run `peakreel safety download`"),
        ("blocked_words", "blocked words in transcript: darn"),
        ("blocked_words", "blocked words in top chat: ass"),
    ]


# -- music ------------------------------------------------------------------------------------


def beat(seconds: float = 20, bpm: float = 120) -> np.ndarray[Any, np.dtype[np.float32]]:
    """A drum kit (kick, snare on 2 and 4, eighth-note hi-hat) over a held chord."""
    rng = np.random.default_rng(3)
    t = np.arange(int(SR * seconds)) / SR
    period = 60 / bpm
    noise = rng.standard_normal(t.size)
    kick = np.sin(2 * np.pi * 60 * t) * np.exp(-12 * np.mod(t, period))
    snare = noise * np.exp(-25 * np.mod(t - period, 2 * period))
    hat = 0.3 * noise * np.exp(-60 * np.mod(t, period / 2))
    chord = 0.1 * (np.sin(2 * np.pi * 261.6 * t) + np.sin(2 * np.pi * 329.6 * t))
    return (0.5 * kick + 0.3 * snare + 0.2 * hat + chord).astype(np.float32)


def talk(seconds: float = 20, seed: int = 1) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Voiced syllables at random pitch and irregular timing, like speech: no beat."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(SR * seconds), dtype=np.float32)
    position = 0
    while position < out.size:
        length = int(SR * rng.uniform(0.12, 0.3))
        t = np.arange(length) / SR
        f0 = rng.uniform(100, 250)
        harmonics = sum(np.sin(2 * np.pi * f0 * h * t) / h for h in range(1, 12))
        syllable = harmonics * np.hanning(length)
        end = min(out.size, position + length)
        out[position:end] = 0.2 * syllable[: end - position]
        pause = rng.uniform(0.02, 0.25) if rng.random() > 0.15 else rng.uniform(0.3, 0.8)
        position = end + int(SR * pause)
    return out


def rms(samples: np.ndarray[Any, np.dtype[np.float32]]) -> float:
    return float(np.sqrt(np.mean(samples**2)))


def test_music_score_separates_beat_from_speech() -> None:
    assert music_score(beat()) > 0.5
    assert music_score(beat(bpm=90)) > 0.5
    for seed in range(1, 4):
        voice = talk(seed=seed)
        assert music_score(voice) < 0.25
        # Music at half the loudness of a streamer talking over it.
        background = beat() * (0.5 * rms(voice) / rms(beat()))
        assert music_score(voice + background) >= 0.35
    assert music_score(beat(seconds=3)) == 0.0  # too short to judge
    assert music_score(np.zeros(SR * 10, dtype=np.float32)) == 0.0


def test_music_flag() -> None:
    assert music_flag(0.2, 0.35) is None
    flag = music_flag(0.6, 0.35)
    assert flag is not None
    assert flag.kind == "music"
    failed = music_flag(None, 0.35)
    assert failed is not None
    assert failed.kind == "checks_incomplete"


# -- alerts -----------------------------------------------------------------------------------


def chat(text: str, ts: float = T0, tags: dict[str, str] | None = None) -> ChatMessage:
    return ChatMessage(ts, "alpha", "u", text, tags=tags or {})


def notice(msg_id: str, ts: float = T0) -> ChatMessage:
    return ChatMessage(ts, "alpha", "u", "", "usernotice", tags={"msg-id": msg_id})


def test_alert_flag_counts_alert_kinds() -> None:
    events = [
        notice("subgift"),
        notice("submysterygift"),
        notice("raid"),
        notice("viewermilestone"),  # no alert
        chat("Cheer500 gg", tags={"bits": "500"}),
        chat("just chatting"),
    ]
    flag = alert_flag(events)
    assert flag is not None
    assert flag.message == (
        "on-screen alerts likely (1 cheer, 2 gifts, 1 raid): they usually show viewer names"
    )
    assert alert_flag([chat("hello")]) is None


# -- fingerprints and dedupe ------------------------------------------------------------------


def program(seconds: float = 40, seed: int = 7) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Rich, non-repeating 'program audio': random tones changing every 250 ms."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * seconds)) / SR
    out = np.zeros_like(t)
    step = SR // 4
    for start in range(0, t.size, step):
        segment = t[start : start + step]
        for _ in range(3):
            out[start : start + step] += np.sin(2 * np.pi * rng.uniform(200, 3000) * segment)
    return (0.2 * out).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray[Any, np.dtype[np.float32]]) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def costream(
    seconds: float = 30, delay: float = 3.0, voice_seed: int = 9, voice_level: float = 1.0
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """The same program audio, later and quieter, with another streamer talking over it.

    ``voice_level`` is the voice's loudness relative to the shared audio; at 1.0 a
    co-stream still matches the original, but two such co-streams do not reliably
    match each other (both add their own voice).
    """
    shifted = np.concatenate([np.zeros(int(SR * delay), np.float32), program(seconds)])
    voice = talk(seconds, seed=voice_seed)
    level = voice_level * 0.6 * rms(program(seconds)) / rms(voice)
    mixed = 0.6 * shifted[: voice.size] + voice * level
    return mixed.astype(np.float32)


def test_similarity_of_arrays() -> None:
    rng = np.random.default_rng(0)
    a = rng.integers(0, 2**32, 300, dtype=np.uint32)
    same = similarity(a, a)
    assert (same.fraction, same.offset_s) == (1.0, 0.0)
    assert same.strength > 100
    shifted = similarity(a[20:], a)  # a[20:] starts 20 items into a
    assert shifted.fraction == 1.0
    assert shifted.offset_s == pytest.approx(-20 * FINGERPRINT_ITEM_S, abs=0.01)
    unrelated = rng.integers(0, 2**32, 300, dtype=np.uint32)
    assert similarity(a, unrelated).strength < DedupeConfig().min_strength
    assert similarity(a[:50], a[:50]) == Match(0.0, 0.0, 0.0)  # under 10 s of overlap


@needs_chromaprint
def test_fingerprints_match_costreams_but_not_unrelated_audio(tmp_path: Path) -> None:
    assert FFMPEG is not None
    original = write_wav(tmp_path / "a.wav", program(30))
    other = write_wav(tmp_path / "b.wav", costream())
    unrelated = write_wav(tmp_path / "c.wav", program(30, seed=99))
    a, b, c = (load_fingerprint(fingerprint(p, FFMPEG)) for p in (original, other, unrelated))
    assert fingerprint_path(original).exists()
    threshold = DedupeConfig().min_strength
    match = similarity(a, b)
    assert match.strength >= threshold
    assert abs(abs(match.offset_s) - 3.0) < 0.3
    assert similarity(a, c).strength < threshold


def _db_clip(
    config: Config, channel: str, start: float, score: float, audio: Path, state: str = "new"
) -> int:
    conn = connect(config.db_path)
    channel_id = ensure_channel(conn, channel)
    spike = Spike(
        channel=channel, start_ts=start, end_ts=start + 20, peak_ts=start, peak_count=40,
        messages=80, unique_chatters=60, baseline_median=6.0, baseline_mad=1.0, score=score,
        top_messages=(), receive_delay_s=None,
    )  # fmt: skip
    spike_id = insert_spike(conn, channel_id, spike)
    clip_id = insert_clip(
        conn, channel_id=channel_id, spike_id=spike_id, start_ts=start, end_ts=start + 30,
        path=audio, sidecar_path=audio.with_suffix(".json"),
    )  # fmt: skip
    conn.execute("UPDATE clips SET state = ? WHERE id = ?", (state, clip_id))
    conn.commit()
    conn.close()
    return clip_id


@needs_chromaprint
def test_resolve_duplicates_keeps_best_clip(config: Config, tmp_path: Path) -> None:
    assert FFMPEG is not None
    init_db(config.db_path)
    settings = DedupeConfig()
    a = write_wav(tmp_path / "a.wav", program(30))
    b = write_wav(tmp_path / "b.wav", costream())
    c = write_wav(tmp_path / "c.wav", program(30, seed=99))
    late = write_wav(tmp_path / "late.wav", costream(delay=2, voice_seed=11, voice_level=0.3))
    for path in (a, b, c, late):
        fingerprint(path, FFMPEG)
    conn = connect(config.db_path)
    try:
        first = _db_clip(config, "alpha", T0, 5.0, a)
        assert resolve_duplicates(conn, first, settings).duplicate_of is None
        unrelated = _db_clip(config, "gamma", T0 + 2, 9.0, c)
        assert resolve_duplicates(conn, unrelated, settings).duplicate_of is None

        better = _db_clip(config, "beta", T0 + 3, 8.0, b)  # higher score: replaces first
        decision = resolve_duplicates(conn, better, settings)
        assert decision.duplicate_of is None
        assert decision.replaced == (first,)
        dup = conn.execute("SELECT duplicate_of FROM clips WHERE id = ?", (first,)).fetchone()
        assert dup[0] == better

        # Far apart in time: not compared, even with the same audio.
        later = _db_clip(config, "delta", T0 + 500, 1.0, late)
        assert resolve_duplicates(conn, later, settings).duplicate_of is None
        conn.execute("UPDATE clips SET start_ts = ? WHERE id = ?", (T0 + 2, later))
        conn.commit()
        # Same moment with a lower score: it is the duplicate.
        assert resolve_duplicates(conn, later, settings).duplicate_of == better

        # An approved clip always stays the kept one, whatever the scores.
        conn.execute("UPDATE clips SET duplicate_of = NULL")
        conn.execute("UPDATE clips SET state = 'approved' WHERE id = ?", (first,))
        conn.commit()
        assert resolve_duplicates(conn, better, settings).duplicate_of == first
    finally:
        conn.close()


# -- clipper integration ----------------------------------------------------------------------


@pytest.fixture
def two_channel_config(write_config: WriteConfig) -> Config:
    path = write_config(
        {
            "channels": [{"name": "alpha"}, {"name": "beta"}],
            "clip": {"pre_roll_s": 20, "post_roll_s": 10, "max_wait_s": 0.3},
            "paths": {"safety_words": "safety.yaml"},
            "safety": {"languages": ["en"]},
        }
    )
    config = load_config(path)
    init_db(config.db_path)
    config.word_lists_dir.mkdir(parents=True)
    (config.word_lists_dir / "en.txt").write_text("darn\n", encoding="utf-8")
    return config


def audio_cutter(audio: dict[str, np.ndarray[Any, np.dtype[np.float32]]]) -> object:
    """Cut a real (audio-only) mp4 from the samples assigned to the window's channel."""

    def cut(window: Window, out: Path, ffmpeg: Path) -> None:
        source = write_wav(out.with_suffix(".src.wav"), audio[window.channel])
        subprocess.run(
            [str(ffmpeg), "-v", "error", "-y", "-i", str(source),
             "-c:a", "aac", "-f", "mp4", str(out)],
            check=True,
        )  # fmt: skip
        source.unlink()

    return cut


def spike_on(channel: str, score: float) -> Spike:
    return Spike(
        channel=channel, start_ts=T0 + 100, end_ts=T0 + 120, peak_ts=T0 + 100, peak_count=40,
        messages=80, unique_chatters=60, baseline_median=6.0, baseline_mad=1.0, score=score,
        top_messages=(("darn it", 9), ("gg", 3)), receive_delay_s=0.5,
    )  # fmt: skip


@needs_chromaprint
async def test_double_spike_produces_one_clip_with_flags(two_channel_config: Config) -> None:
    config = two_channel_config
    assert FFMPEG is not None
    for channel in ("alpha", "beta"):
        make_session(config.buffer_dir / channel, "s1", T0, 100)
    music = (0.5 * beat(30) + 0.5 * program(30)).astype(np.float32)
    voice = talk(30, seed=4)
    costreamed = 0.6 * music + voice * (0.6 * rms(music) / rms(voice))
    audio = {"alpha": music, "beta": costreamed.astype(np.float32)}
    rendered: list[Path] = []

    def renderer(_config: Config, clip: Path, _ffmpeg: Path, **_kw: object) -> RenderedClip:
        rendered.append(clip)
        raise RuntimeError("no video in this test")

    sub = notice("resub", ts=T0 + 95)
    contexts: list[tuple[str, float, float]] = []

    def context(channel: str, start: float, end: float) -> ClipContext:
        contexts.append((channel, start, end))
        return ClipContext(chat=(sub,), stream={"game": "Grand Theft Auto VI"})

    clipper = Clipper(
        config, FFMPEG, cutter=audio_cutter(audio), poll_s=0.05,  # type: ignore[arg-type]
        renderer=renderer, context=context,
    )  # fmt: skip
    first = await clipper.handle(spike_on("alpha", 12.0))
    second = await clipper.handle(spike_on("beta", 7.0))

    assert first.duplicate_of is None
    assert second.duplicate_of == first.clip_id
    assert rendered == [first.clip_path]  # the duplicate is not rendered
    assert contexts[0] == ("alpha", T0 + 80.0, T0 + 110.0)  # broadcast window + delay

    kinds = {flag.kind for flag in first.flags}
    assert kinds == {"blocked_words", "music", "alerts"}
    assert first.sidecar_path is not None
    sidecar = json.loads(first.sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["stream"] == {"game": "Grand Theft Auto VI"}
    assert {f["kind"] for f in sidecar["flags"]} == kinds
    conn = connect(config.db_path)
    try:
        stored = conn.execute(
            "SELECT id, flags, duplicate_of, match_strength FROM clips ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert {f["kind"] for f in json.loads(stored[0]["flags"])} == kinds
    assert stored[1]["duplicate_of"] == stored[0]["id"]
    assert stored[1]["match_strength"] >= DedupeConfig().min_strength
