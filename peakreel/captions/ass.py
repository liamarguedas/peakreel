"""Build ASS subtitles with a highlighted current word from word timestamps.

Words are grouped into short one-line *chunks* (the "few words at a time" style of
short-form video). While a chunk is on screen, the word being spoken is drawn in the
highlight colour and the rest in white. Each word gets its own subtitle event that
lasts until the next word starts, so the highlight moves without the line flickering.

The subtitle canvas is the output frame (``PlayResX`` x ``PlayResY``), so positions
are in output pixels. Captions sit above the credit overlay and clear of both
unsafe zones (see :func:`caption_margins`).
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from peakreel.config import CaptionsConfig, OutputConfig

__all__ = [
    "NO_SPACE_LANGUAGES",
    "Chunk",
    "Word",
    "build_ass",
    "caption_margins",
    "chunk_words",
    "format_time",
]

NO_SPACE_LANGUAGES = frozenset({"ja", "zh", "th", "lo", "my", "km", "yue"})
"""Languages written without spaces between words."""

# Full-width marks are intentional: Japanese and Chinese sentence endings.
_SENTENCE_END = (".", "!", "?", "。", "！", "？", "…")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class Word:
    """A transcribed word with its timing in clip seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """Consecutive words shown together on one line."""

    words: tuple[Word, ...]

    @property
    def start(self) -> float:
        """Time the first word starts."""
        return self.words[0].start

    @property
    def end(self) -> float:
        """Time the last word ends."""
        return self.words[-1].end


def _width(text: str) -> float:
    """Display width in "Latin character" units: wide (CJK) characters count 2."""
    return sum(2.0 if unicodedata.east_asian_width(c) in ("W", "F") else 1.0 for c in text)


def chunk_words(
    words: Sequence[Word], *, max_width: float, max_gap_s: float = 0.8, joiner: str = " "
) -> list[Chunk]:
    """Group words into chunks that fit one caption line.

    A new chunk starts when adding the next word would exceed ``max_width``, when
    there is a pause longer than ``max_gap_s``, or after a word that ends a sentence.

    Parameters
    ----------
    words : sequence of Word
        In time order. Empty words are dropped.
    max_width : float
        Line width limit in Latin-character units (CJK characters count 2).
    max_gap_s : float, default 0.8
        Pause that always starts a new chunk.
    joiner : str, default " "
        ``""`` for languages written without spaces.

    Returns
    -------
    list of Chunk

    Examples
    --------
    >>> ws = [Word(0.0, 0.2, "no"), Word(0.2, 0.4, "way"), Word(2.0, 2.3, "what")]
    >>> [[w.text for w in c.words] for c in chunk_words(ws, max_width=20)]
    [['no', 'way'], ['what']]
    """
    chunks: list[Chunk] = []
    current: list[Word] = []
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        word = Word(word.start, max(word.end, word.start), text)
        if current:
            line = joiner.join(w.text for w in [*current, word])
            if (
                _width(line) > max_width
                or word.start - current[-1].end > max_gap_s
                or current[-1].text.endswith(_SENTENCE_END)
            ):
                chunks.append(Chunk(tuple(current)))
                current = []
        current.append(word)
    if current:
        chunks.append(Chunk(tuple(current)))
    return chunks


def format_time(seconds: float) -> str:
    """Format seconds as an ASS timestamp ``H:MM:SS.cc``.

    Examples
    --------
    >>> format_time(3725.456)
    '1:02:05.46'
    """
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _escape(text: str) -> str:
    # Braces start override blocks and a backslash starts an escape in ASS.
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _ass_colour(hex_rgb: str) -> str:
    value = hex_rgb.lstrip("#")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H00{b}{g}{r}".upper()


def caption_margins(output: OutputConfig, settings: CaptionsConfig) -> tuple[int, int, int]:
    """Return the ASS ``(MarginL, MarginR, MarginV)`` for bottom-centred captions.

    The right margin keeps text out of the right unsafe zone; the vertical margin
    puts the caption baseline ``settings.bottom_fraction`` of the way down the frame,
    which must be above the bottom unsafe zone and the credit.

    Parameters
    ----------
    output : OutputConfig
    settings : CaptionsConfig

    Returns
    -------
    tuple of (int, int, int)
    """
    side = settings.side_margin_px
    right = math.ceil(output.width * output.safe_zone_right) + side
    vertical = output.height - math.floor(output.height * settings.bottom_fraction)
    return side, right, vertical


def build_ass(
    chunks: Sequence[Chunk],
    *,
    font_name: str,
    output: OutputConfig,
    settings: CaptionsConfig,
    joiner: str = " ",
) -> str:
    """Render chunks as an ASS subtitle document.

    Parameters
    ----------
    chunks : sequence of Chunk
    font_name : str
        Font family as libass knows it (for example ``"Yu Gothic"``).
    output : OutputConfig
        Canvas size and safe zones.
    settings : CaptionsConfig
        Size, colours and placement.
    joiner : str, default " "

    Returns
    -------
    str
        The ``.ass`` file content.
    """
    left, right, vertical = caption_margins(output, settings)
    white = _ass_colour("#FFFFFF")
    highlight = _ass_colour(settings.highlight_color)
    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {output.width}",
            f"PlayResY: {output.height}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Caption,{font_name},{settings.font_px},{white},{white},&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,{settings.outline_px},2,2,{left},{right},{vertical},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    events: list[str] = []
    for index, chunk in enumerate(chunks):
        next_start = chunks[index + 1].start if index + 1 < len(chunks) else math.inf
        chunk_end = min(chunk.end + settings.linger_s, next_start)
        for position, word in enumerate(chunk.words):
            start = word.start
            following = (
                chunk.words[position + 1].start if position + 1 < len(chunk.words) else chunk_end
            )
            end = max(following, start + 0.05)
            parts = [
                f"{{\\c{highlight}}}{_escape(w.text)}{{\\c{white}}}"
                if i == position
                else _escape(w.text)
                for i, w in enumerate(chunk.words)
            ]
            text = joiner.join(parts)
            events.append(
                f"Dialogue: 0,{format_time(start)},{format_time(end)},Caption,,0,0,0,,{text}"
            )
    return header + "\n" + "\n".join(events) + "\n"
