"""Transcribe clips with faster-whisper on a single worker thread.

Why one worker
--------------
Transcription is CPU-heavy and the model holds several hundred MB of memory, so
:class:`Captioner` owns exactly one model and one worker thread. Clips queue up in
front of it; the event loop that runs chat ingest and detection only awaits the
result and is never blocked. The current backlog is logged with every clip.

Audio is decoded with the configured ffmpeg into 16 kHz mono samples, the format
Whisper expects, instead of faster-whisper's own decoder (PyAV), whose API changed
under it (``metadata_errors`` was removed in PyAV 19).

The model is downloaded once (``peakreel captions download``) into
``tools.whisper_model_dir``; afterwards it is loaded with ``local_files_only`` so the
pipeline never needs the network for captions.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from peakreel.captions.ass import NO_SPACE_LANGUAGES, Word, build_ass, chunk_words
from peakreel.config import CaptionsConfig, OutputConfig
from peakreel.log import get_logger

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

__all__ = ["CaptionResult", "Captioner", "decode_audio", "load_model"]

log = get_logger(__name__)

SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class CaptionResult:
    """Outcome of captioning one clip.

    Attributes
    ----------
    status : str
        ``"ok"`` (subtitles written), ``"no_speech"`` (too few words) or ``"failed"``.
    ass_path : pathlib.Path or None
        The subtitle file when ``status == "ok"``.
    language : str or None
        Detected (or configured) language code.
    words : int
        Words transcribed.
    seconds : float
        Time spent transcribing.
    error : str or None
        Why captioning failed.
    text : str
        The transcript (also for ``no_speech``), used by the safety checks.
    """

    status: str
    ass_path: Path | None = None
    language: str | None = None
    words: int = 0
    seconds: float = 0.0
    error: str | None = None
    text: str = ""

    def to_sidecar(self) -> dict[str, Any]:
        """Summary stored in the clip's sidecar under ``captions``."""
        data: dict[str, Any] = {
            "status": self.status,
            "language": self.language,
            "words": self.words,
            "seconds": round(self.seconds, 2),
        }
        if self.ass_path is not None:
            data["file"] = self.ass_path.name
        if self.error:
            data["error"] = self.error
        if self.text:
            data["text"] = self.text
        return data


def decode_audio(clip: Path, ffmpeg: Path) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Decode a clip's audio to 16 kHz mono float32 samples.

    Parameters
    ----------
    clip : pathlib.Path
    ffmpeg : pathlib.Path

    Returns
    -------
    numpy.ndarray
        One-dimensional; empty if the clip has no audio.

    Raises
    ------
    RuntimeError
        If ffmpeg fails.
    """
    try:
        raw = subprocess.run(
            [str(ffmpeg), "-v", "error", "-i", str(clip), "-vn", "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
            capture_output=True,
            check=True,
        ).stdout  # fmt: skip
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"audio decode failed: {exc.stderr.decode('utf-8', 'replace')[-300:]}"
        ) from exc
    return np.frombuffer(raw, dtype=np.float32)


def _cuda_libraries() -> None:
    """Make the pip-installed cuBLAS and cuDNN DLLs loadable (Windows has no CUDA install)."""
    import importlib.util
    import os

    for module in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(module)
        if spec is None or not spec.submodule_search_locations:
            continue
        folder = Path(next(iter(spec.submodule_search_locations))) / "bin"
        if folder.is_dir():
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(folder))
            # ctranslate2 loads them with a plain LoadLibrary, which searches PATH.
            os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"


def load_model(settings: CaptionsConfig, model_dir: Path, *, allow_download: bool) -> WhisperModel:
    """Load the configured Whisper model.

    Parameters
    ----------
    settings : CaptionsConfig
    model_dir : pathlib.Path
        Cache directory (``tools.whisper_model_dir``).
    allow_download : bool
        When false the model must already be cached (no network access).

    Returns
    -------
    faster_whisper.WhisperModel

    Raises
    ------
    RuntimeError
        If the model is not cached and downloading is not allowed (or fails).
    """
    from faster_whisper import WhisperModel

    model_dir.mkdir(parents=True, exist_ok=True)
    if settings.device == "cuda":
        # The graphics card: 2.4 s instead of 5.1 s a clip, and no CPU cores taken from
        # recording (2026-11-20). cuDNN fails only when first used, so a second of
        # silence is transcribed now; any failure falls back to the CPU.
        try:
            _cuda_libraries()
            model = WhisperModel(
                settings.model,
                device="cuda",
                compute_type=settings.compute_type,
                download_root=str(model_dir),
                local_files_only=not allow_download,
            )
            check: Any = model  # faster-whisper is untyped
            segments, _info = check.transcribe(np.zeros(16_000, dtype=np.float32))
            list(segments)
            return model
        except Exception as exc:
            log.warning("captions: graphics card unavailable, using the CPU", error=str(exc)[:200])
    try:
        return WhisperModel(
            settings.model,
            device="cpu",
            compute_type="int8" if settings.device == "cuda" else settings.compute_type,
            cpu_threads=settings.cpu_threads,
            download_root=str(model_dir),
            local_files_only=not allow_download,
        )
    except Exception as exc:
        hint = "" if allow_download else " (run `peakreel captions download` first)"
        raise RuntimeError(f"cannot load whisper model {settings.model!r}{hint}: {exc}") from exc


@dataclass
class Captioner:
    """Caption clips one at a time on a dedicated worker thread.

    Parameters
    ----------
    settings : CaptionsConfig
    output : OutputConfig
        Frame size and safe zones for subtitle placement.
    model_dir : pathlib.Path
    ffmpeg : pathlib.Path
    font_name : str
        Font family for the subtitles (see :func:`peakreel.render.vertical.font_family`).
    """

    settings: CaptionsConfig
    output: OutputConfig
    model_dir: Path
    ffmpeg: Path
    font_name: str
    _executor: ThreadPoolExecutor = field(init=False, repr=False)
    _model: WhisperModel | None = field(init=False, default=None, repr=False)
    _model_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)
    backlog: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        """Start the single worker thread."""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="captions")

    def _get_model(self) -> WhisperModel:
        with self._model_lock:
            if self._model is None:
                started = time.monotonic()
                self._model = load_model(self.settings, self.model_dir, allow_download=False)
                log.info(
                    "whisper model loaded",
                    model=self.settings.model,
                    seconds=round(time.monotonic() - started, 1),
                )
            return self._model

    def transcribe(self, clip: Path) -> tuple[list[Word], str | None]:
        """Transcribe ``clip`` (blocking).

        Returns
        -------
        tuple of (list of Word, str or None)
            Words with clip-relative times, and the language.
        """
        audio = decode_audio(clip, self.ffmpeg)
        if audio.size == 0:
            return [], None
        model: Any = self._get_model()  # faster-whisper is untyped
        segments, info = model.transcribe(
            audio,
            language=self.settings.language,
            beam_size=self.settings.beam_size,
            word_timestamps=True,
            vad_filter=self.settings.vad_filter,
        )
        words: list[Word] = []
        segment: Any
        for segment in segments:
            segment_words: list[Any] = list(segment.words or [])
            for item in segment_words:
                words.append(Word(float(item.start), float(item.end), str(item.word)))
        return words, info.language

    def caption_sync(self, clip: Path) -> CaptionResult:
        """Transcribe ``clip`` and write ``<name>.ass`` next to it (blocking).

        Parameters
        ----------
        clip : pathlib.Path

        Returns
        -------
        CaptionResult
            ``failed`` results carry the error instead of raising.
        """
        started = time.monotonic()
        try:
            words, language = self.transcribe(clip)
        except Exception as exc:
            log.exception("transcription failed", clip=clip.name)
            return CaptionResult("failed", seconds=time.monotonic() - started, error=str(exc))
        elapsed = time.monotonic() - started
        joiner = "" if language in NO_SPACE_LANGUAGES else " "
        text = joiner.join(w.text.strip() for w in words).strip()
        if len(words) < self.settings.min_words:
            return CaptionResult(
                "no_speech", language=language, words=len(words), seconds=elapsed, text=text
            )
        max_width = self.settings.max_line_chars
        chunks = chunk_words(words, max_width=max_width, joiner=joiner)
        ass = build_ass(
            chunks,
            font_name=self.font_name,
            output=self.output,
            settings=self.settings,
            joiner=joiner,
        )
        path = clip.with_suffix(".ass")
        path.write_text(ass, encoding="utf-8")
        return CaptionResult("ok", path, language, len(words), elapsed, text=text)

    async def warm_up(self) -> None:
        """Load the model on the worker thread now, so problems show at start.

        Raises
        ------
        RuntimeError
            If the model cannot be loaded (for example, not downloaded yet).
        """
        await asyncio.get_running_loop().run_in_executor(self._executor, self._get_model)

    async def caption(self, clip: Path) -> CaptionResult:
        """Queue ``clip`` on the worker thread and await its captions.

        Parameters
        ----------
        clip : pathlib.Path

        Returns
        -------
        CaptionResult
        """
        self.backlog += 1
        log.info("captioning queued", clip=clip.name, backlog=self.backlog)
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(self._executor, self.caption_sync, clip)
        finally:
            self.backlog -= 1
        log.info(
            "captions done",
            clip=clip.name,
            status=result.status,
            language=result.language,
            words=result.words,
            seconds=round(result.seconds, 1),
            backlog=self.backlog,
        )
        return result

    def close(self) -> None:
        """Stop the worker thread after queued clips finish."""
        self._executor.shutdown(wait=True)
