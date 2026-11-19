"""Render a horizontal clip as a 1080x1920 vertical video with the channel's name.

Layouts
-------
``split`` (the streamer's webcam was found)
    The webcam on top (``render.facecam_height_fraction`` of the height), the
    channel's name with its platform icon (Twitch or Kick) in a band below it, then
    the gameplay zoomed in by ``render.game_zoom`` (centre kept), over a blurred copy
    of the stream. The webcam is a channel's ``facecam`` region, else found
    automatically (:mod:`peakreel.render.facecam`).
``blur`` (no webcam overlay)
    The gameplay at full width, centred over a blurred and darkened copy of itself,
    with the name band right above it.

The name band is centred and kept clear of the side safe zones
(``output.safe_zone_right`` on both sides): :func:`name_band` sizes it, and the
name's real width is measured with ffmpeg so icon and name are centred together.

Windows notes
-------------
ffmpeg's filter syntax treats ``:`` as a separator, so a drive-letter path such as
``C:/Windows/Fonts/x.ttf`` breaks it. The credit text goes through ``textfile=`` with
a relative name (ffmpeg runs with ``cwd`` set to a temporary directory), and the font
path is escaped with :func:`escape_filter_value`.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from peakreel.config import ChannelConfig, Config, FacecamRegion, OutputConfig, RenderConfig
from peakreel.encode import video_args
from peakreel.platforms import channel_name, channel_url, split_key
from peakreel.render.facecam import detect_facecam, static_overlay

if TYPE_CHECKING:
    from peakreel.captions.whisper import CaptionResult
from peakreel.log import get_logger

__all__ = [
    "FONT_CANDIDATES",
    "FONT_FAMILIES",
    "NameBand",
    "RenderError",
    "RenderedClip",
    "build_filter",
    "credit_text",
    "escape_filter_value",
    "font_family",
    "game_offset",
    "name_band",
    "render_clip",
    "render_vertical",
    "resolve_font",
    "text_width_em",
]

log = get_logger(__name__)

FONT_CANDIDATES: tuple[Path, ...] = (
    # Windows: bold Japanese/Latin first, then Korean, then Latin only.
    Path("C:/Windows/Fonts/YuGothB.ttc"),
    Path("C:/Windows/Fonts/malgunbd.ttf"),
    Path("C:/Windows/Fonts/msgothic.ttc"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    # Linux (for a future VM deployment).
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
"""Fonts tried in order when ``render.font_file`` is not set."""

FONT_FAMILIES: dict[str, str] = {
    "YuGothB.ttc": "Yu Gothic",
    "malgunbd.ttf": "Malgun Gothic",
    "msgothic.ttc": "MS Gothic",
    "arialbd.ttf": "Arial",
    "NotoSansCJK-Bold.ttc": "Noto Sans CJK JP",
    "DejaVuSans-Bold.ttf": "DejaVu Sans",
}
"""Family names of the :data:`FONT_CANDIDATES`, as libass looks them up."""

_MIN_FONT_PX = 20
_MIN_TITLE_PX = 40
_ICONS = {"twitch": "0x9146FF", "kick": "0x53FC18"}  # the platforms' brand colours


class RenderError(RuntimeError):
    """Raised when a clip cannot be rendered; the message includes ffmpeg's error."""


def resolve_font(configured: Path | None) -> Path:
    """Return the font file for the credit overlay.

    Parameters
    ----------
    configured : pathlib.Path or None
        ``render.font_file``. Used as is when set.

    Returns
    -------
    pathlib.Path

    Raises
    ------
    RenderError
        If the configured font is missing, or none of :data:`FONT_CANDIDATES` exists.
    """
    if configured is not None:
        if not configured.is_file():
            raise RenderError(f"render.font_file not found: {configured}")
        return configured
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RenderError("no font found; set render.font_file in config.local.yaml")


def font_family(font: Path, configured: str | None = None) -> str:
    """Return the family name libass needs to find ``font``.

    Parameters
    ----------
    font : pathlib.Path
    configured : str or None
        ``render.font_name``; wins when set.

    Returns
    -------
    str

    Raises
    ------
    RenderError
        If the font is not one of the known candidates and no name is configured.
    """
    if configured:
        return configured
    family = FONT_FAMILIES.get(font.name)
    if family is None:
        raise RenderError(f"unknown family for {font.name}; set render.font_name")
    return family


def credit_text(channel: str, credit: str | None) -> str:
    """Return the one-line credit drawn on the video.

    Parameters
    ----------
    channel : str
        Channel key (Twitch login, or ``kick-<slug>``).
    credit : str or None
        The channel's configured credit.

    Returns
    -------
    str
        The credit, with the channel's address (``twitch.tv/<login>`` or
        ``kick.com/<slug>``) appended when it does not already name the channel.

    Examples
    --------
    >>> credit_text("alpha", None)
    'Clip from twitch.tv/alpha'
    >>> credit_text("alpha", "Thanks for watching!")
    'Thanks for watching! · twitch.tv/alpha'
    >>> credit_text("kick-westcol", None)
    'Clip from kick.com/westcol'
    """
    address = channel_url(channel)
    text = (credit or f"Clip from {address}").strip()
    if channel_name(channel).lower() not in text.lower():
        text = f"{text} · {address}"
    return text


def text_width_em(text: str) -> float:
    """Upper-bound estimate of the rendered width of ``text`` in ems.

    Wide (CJK) characters count 1.0 em, everything else 0.65 em, which is wider than
    any Latin glyph of a bold sans-serif except ``M`` and ``W``. Overestimating is
    the safe direction: the text then gets a smaller font, never a wider box.

    Parameters
    ----------
    text : str

    Returns
    -------
    float
    """
    return sum(1.0 if unicodedata.east_asian_width(c) in ("W", "F") else 0.65 for c in text)


@dataclass(frozen=True, slots=True)
class NameBand:
    """The channel's name with its platform icon, in a dark band across the frame.

    Attributes
    ----------
    text : str
        The channel name (Twitch login or Kick slug), possibly cut with an ellipsis.
    platform : str
        ``"twitch"`` or ``"kick"``, which picks the icon.
    font_px, icon_px, gap_px, height : int
        Sizes: the name, the square icon, the space between them, the band.
    icon_x, text_x : int
        Left edges of the icon and of the name's ink (set by :meth:`centred`).
    title_lines : tuple of str
        The clip's title (the hook), wrapped; empty for a name-only band.
    title_px : int
        Font size of the title lines.
    """

    text: str
    platform: str
    font_px: int
    icon_px: int
    gap_px: int
    height: int
    icon_x: int = 0
    text_x: int = 0
    title_lines: tuple[str, ...] = ()
    title_px: int = 0

    @property
    def padding(self) -> int:
        """Space above the title and below the name row."""
        return round(self.title_px * 0.35) if self.title_lines else 0

    @property
    def line_px(self) -> int:
        """Height of one title line."""
        return round(self.title_px * 1.2)

    @property
    def row_top(self) -> int:
        """Offset of the icon-and-name row from the top of the band."""
        return self.padding + len(self.title_lines) * self.line_px

    @property
    def row_height(self) -> int:
        """Height of the icon-and-name row."""
        return self.height - self.row_top - self.padding

    def centred(self, ink_width: int, ink_offset: int, output: OutputConfig) -> NameBand:
        """Return the band with icon and name centred together on the frame.

        ``ink_width`` is the name's measured width and ``ink_offset`` how far its ink
        starts right of the drawtext ``x`` (the first glyph's bearing).
        """
        group = self.icon_px + self.gap_px + ink_width
        icon_x = (output.width - group) // 2
        return replace(self, icon_x=icon_x, text_x=icon_x + self.icon_px + self.gap_px - ink_offset)


def name_band(
    channel: str, output: OutputConfig, settings: RenderConfig, title: str | None = None
) -> NameBand:
    """Size the band so the title, the icon and the name fit between the side safe zones.

    Parameters
    ----------
    channel : str
        Channel key (``kick-<slug>`` for Kick).
    output : OutputConfig
    settings : RenderConfig
    title : str, optional
        The clip's hook (Gemini's title). Drawn big, on up to three lines, above a
        smaller icon and name; emoji are left out (the font has none). Without it
        the band holds the name alone, at ``render.credit_max_font_px``.

    Returns
    -------
    NameBand
        Not yet centred (see :meth:`NameBand.centred`). A name or title too long at
        the smallest font size is cut with an ellipsis.

    Examples
    --------
    >>> band = name_band("kick-westcol", OutputConfig(), RenderConfig())
    >>> band.text, band.platform, band.font_px, band.title_lines
    ('westcol', 'kick', 52, ())
    >>> band = name_band("kaicenat", OutputConfig(), RenderConfig(), "bro had enough 💀")
    >>> band.title_lines, band.title_px, band.font_px
    (('bro had enough',), 64, 36)
    """
    platform = split_key(channel)[0]
    text = channel_name(channel)
    side = math.ceil(output.width * output.safe_zone_right) + settings.margin_px
    lines, title_px = _wrap_title(_drawable(title or ""), output.width - 2 * side, settings)
    font_px = settings.credit_max_font_px if not lines else round(title_px * 0.56)

    def room(px: int) -> float:  # width left for the name next to its icon
        return output.width - 2 * side - round(px * 1.25) - round(px * 0.4)

    fit = math.floor(room(font_px) / max(text_width_em(text), 1))
    if fit < font_px:
        font_px = max(fit, _MIN_FONT_PX)
    if text_width_em(text) * font_px > room(font_px):
        while text and text_width_em(text + "…") * font_px > room(font_px):
            text = text[:-1]
        text = text.rstrip() + "…"
    band = NameBand(
        text=text,
        platform=platform,
        font_px=font_px,
        icon_px=2 * round(font_px * 1.25 / 2),
        gap_px=round(font_px * 0.4),
        height=2 * round(font_px * 2.2 / 2),
        title_lines=lines,
        title_px=title_px,
    )
    if lines:
        row = round(font_px * 1.7)
        band = replace(band, height=2 * round((band.row_top + row + band.padding) / 2))
    return band


def _drawable(title: str) -> str:
    """Drop what the font cannot draw (emoji, joiners): it would show as boxes."""
    kept = (
        c for c in title
        if unicodedata.category(c) not in ("So", "Sk", "Cs", "Co", "Cf") and ord(c) < 0x1F000
    )  # fmt: skip
    return " ".join("".join(kept).split())


def _title_em(text: str) -> float:
    """Estimated width of ``text`` in ems for a bold sans-serif, per character.

    Closer than :func:`text_width_em` (an upper bound for names), so titles do not
    break early; 8% of slack keeps the widest glyphs inside the line.
    """
    narrow, wide = set("iljtfrI.,;:!'|() "), set("mwMWOQGDH@%")
    widths = (
        1.0 if unicodedata.east_asian_width(c) in ("W", "F")
        else 0.32 if c in narrow
        else 0.82 if c in wide
        else 0.7 if c.isupper()
        else 0.57
        for c in text
    )  # fmt: skip
    return sum(widths) * 1.08


def _greedy(tokens: list[str], joiner: str, width: float, font_px: int) -> list[str]:
    lines: list[str] = []
    for token in tokens:
        candidate = f"{lines[-1]}{joiner}{token}" if lines else token
        if lines and _title_em(candidate) * font_px <= width:
            lines[-1] = candidate
        else:
            lines.append(token)
    return lines


def _wrap_title(
    title: str, width: float, settings: RenderConfig, max_lines: int = 3
) -> tuple[tuple[str, ...], int]:
    """Wrap ``title`` into at most ``max_lines`` balanced lines of ``width`` pixels.

    Starts at ``render.title_max_font_px`` and shrinks to fit; at the smallest size
    the last line is cut with an ellipsis. Lines are balanced (about equally long)
    rather than filled greedily, so no single word is left alone on the last line.
    Scripts without spaces (Japanese) wrap per character.

    Examples
    --------
    >>> _wrap_title("he did NOT see that truck coming lol", 820, RenderConfig())
    (('he did NOT see that', 'truck coming lol'), 64)
    """
    if not title:
        return (), 0
    tokens = title.split(" ") if " " in title else list(title)
    joiner = " " if " " in title else ""
    font_px = settings.title_max_font_px
    lines = _greedy(tokens, joiner, width, font_px)
    while len(lines) > max_lines and font_px > _MIN_TITLE_PX:
        font_px -= 4
        lines = _greedy(tokens, joiner, width, font_px)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _title_em(last + "…") * font_px > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    else:
        # Balance: the narrowest width that still needs no more lines.
        lo, hi = max(_title_em(t) * font_px for t in tokens), width
        while hi - lo > 4:
            mid = (lo + hi) / 2
            if len(_greedy(tokens, joiner, mid, font_px)) <= len(lines):
                hi = mid
            else:
                lo = mid
        lines = _greedy(tokens, joiner, hi, font_px)
    # A single word wider than the line (rare) is cut too.
    fitted: list[str] = []
    for line in lines:
        while _title_em(line) * font_px > width and len(line) > 1:
            line = line[:-2] + "…"
        fitted.append(line)
    return tuple(fitted), font_px


def _icon(band: NameBand, y: int, font: str) -> str:
    """Return drawbox/drawtext filters drawing the platform icon at ``(icon_x, y)``."""
    s, x = band.icon_px, band.icon_x

    def box(dx: float, dy: float, w: float, h: float, color: str) -> str:
        return (
            f"drawbox=x={x + round(dx * s)}:y={y + round(dy * s)}:w={round(w * s)}"
            f":h={round(h * s)}:color={color}:t=fill"
        )

    if band.platform == "kick":
        return ",".join([
            box(0, 0, 1, 1, _ICONS["kick"]),
            f"drawtext=fontfile='{font}':text=K:fontsize={round(s * 0.8)}:fontcolor=black"
            f":x={x}+({s}-text_w)/2:y={y}+({s}-text_h)/2",
        ])  # fmt: skip
    purple = _ICONS["twitch"]
    return ",".join([  # Twitch's speech bubble with two eyes
        box(0, 0, 1, 1, purple),
        box(0.2, 0.16, 0.6, 0.52, "white"),
        box(0.2, 0.68, 0.2, 0.16, "white"),
        box(0.4, 0.28, 0.08, 0.2, purple),
        box(0.6, 0.28, 0.08, 0.2, purple),
    ])  # fmt: skip


def escape_filter_value(value: str) -> str:
    r"""Escape a value for use inside a single-quoted ffmpeg filter option.

    Parameters
    ----------
    value : str

    Returns
    -------
    str

    Examples
    --------
    >>> escape_filter_value("C:/Windows/Fonts/YuGothB.ttc")
    'C\\:/Windows/Fonts/YuGothB.ttc'
    """
    return value.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_filter(
    output: OutputConfig,
    settings: RenderConfig,
    band: NameBand,
    font: Path,
    facecam: FacecamRegion | None,
    *,
    text_file: str = "name.txt",
    subtitles: str | None = None,
    overlay: Sequence[float] | None = None,
) -> str:
    """Return the ``-filter_complex`` graph producing ``[v]``.

    Parameters
    ----------
    output : OutputConfig
    settings : RenderConfig
    band : NameBand
        Centred (:meth:`NameBand.centred`).
    font : pathlib.Path
    facecam : FacecamRegion or None
        ``None`` selects the ``blur`` layout, a region the ``split`` layout.
    text_file : str, default "name.txt"
        Relative path of the file holding the channel name.
    subtitles : str or None
        Relative path of an ASS file to burn in (captions), with its font in the
        relative directory ``fonts``. Drawn over everything else.
    overlay : sequence of float, optional
        Share of each column of the source covered by a fixed overlay
        (:func:`peakreel.render.facecam.overlay_columns`). The zoomed gameplay of the
        ``split`` layout then avoids them (see :func:`game_offset`).

    Returns
    -------
    str
    """
    w, h = output.width, output.height
    background = (
        f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur=sigma={settings.blur_sigma:g},eq=brightness=-0.12[bg];"
    )
    if facecam is None:
        game_h = 2 * round(w * 9 / 16 / 2)  # 16:9 stream at full width
        band_y = (h - game_h) // 2 - band.height
        base = (
            "[0:v]split=2[bgsrc][fgsrc];" + background + f"[fgsrc]scale={w}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        )
    else:
        cam_h = 2 * round(h * settings.facecam_height_fraction / 2)
        band_y = cam_h
        game_y = cam_h + band.height
        game_w = 2 * round(w * settings.game_zoom / 2)
        game_h = min(2 * round(game_w * 9 / 16 / 2), h - game_y)
        game_x = game_offset(facecam, game_w, w, overlay)
        base = (
            "[0:v]split=3[bgsrc][camsrc][gamesrc];" + background +
            f"[camsrc]crop=iw*{facecam.w:g}:ih*{facecam.h:g}:iw*{facecam.x:g}:ih*{facecam.y:g},"
            f"scale={w}:{cam_h}:force_original_aspect_ratio=increase,crop={w}:{cam_h}[cam];"
            f"[gamesrc]scale={game_w}:-2,crop={w}:{game_h}:{game_x}:(ih-{game_h})/2[game];"
            f"[bg][cam]overlay=0:0[top];[top][game]overlay=0:{game_y}[base];"
        )  # fmt: skip
    font_value = escape_filter_value(font.as_posix())
    row_y = band_y + band.row_top
    icon_y = row_y + (band.row_height - band.icon_px) // 2
    titles = "".join(
        f"drawtext=fontfile='{font_value}':textfile=title{i}.txt:fontsize={band.title_px}"
        f":fontcolor=white:x=(w-text_w)/2:y={band_y + band.padding + i * band.line_px},"
        for i in range(len(band.title_lines))
    )
    named = (
        f"[base]drawbox=x=0:y={band_y}:w={w}:h={band.height}:color=black@0.62:t=fill,"
        f"{titles}{_icon(band, icon_y, font_value)},"
        f"drawtext=fontfile='{font_value}':textfile={text_file}:fontsize={band.font_px}"
        f":fontcolor=white@0.85:x={band.text_x}:y={row_y}+({band.row_height}-text_h)/2"
    )
    if subtitles is not None:
        named += f",subtitles=filename={subtitles}:fontsdir=fonts"
    return f"{base}{named},fps={output.fps},format={output.pixel_format}[v]"


def game_offset(
    facecam: FacecamRegion, game_w: int, width: int, overlay: Sequence[float] | None = None
) -> int:
    """Return the left edge of the ``width``-wide window into the zoomed gameplay.

    The window never shows the webcam a second time: it stays on the other side of
    it. Within that, it stays near the centre (the action and the game's subtitles
    are there) and moves off it only when that clearly hides fixed overlays (chat
    boxes, counters, alerts) given by ``overlay``.

    Parameters
    ----------
    facecam : FacecamRegion
    game_w : int
        Width of the zoomed gameplay.
    width : int
        Width of the window (the output width).
    overlay : sequence of float, optional
        Overlay share per source column (:func:`peakreel.render.facecam.overlay_columns`).

    Returns
    -------
    int

    Examples
    --------
    >>> cam = FacecamRegion(x=0.0, y=0.4, w=0.2, h=0.3)
    >>> game_offset(cam, 1512, 1080)  # pushed right of the webcam
    432
    >>> chat = [0.0] * 500 + [0.6] * 140  # a chat box on the right
    >>> game_offset(FacecamRegion(x=0.0, y=0.0, w=0.05, h=0.2), 1512, 1080, chat)
    137
    >>> game_offset(FacecamRegion(x=0.0, y=0.0, w=0.05, h=0.2), 1512, 1080, [0.0] * 640)
    216
    """
    slack = game_w - width
    centre = slack // 2
    # A found webcam box can sit inside the overlay's outer frame (seen: 7% of the width).
    pad = 0.02 + 0.4 * facecam.w
    lo, hi = 0, slack
    if facecam.x + facecam.w / 2 < 0.5:
        lo = min(math.ceil((facecam.x + facecam.w + pad) * game_w), slack)
    else:
        hi = max(math.floor((facecam.x - pad) * game_w) - width, 0)
    preferred = min(max(centre, lo), hi)
    if not overlay or slack <= 0:
        return preferred
    n = len(overlay)
    sums = [0]
    for share in overlay:  # a column with any overlay in it (a chat box covers only its top)
        sums.append(sums[-1] + (share > 0.08))

    def covered(x: int) -> float:  # share of the window's columns showing an overlay
        a, b = round(x / game_w * n), round((x + width) / game_w * n)
        return (sums[b] - sums[a]) / max(b - a, 1)

    def cost(x: int) -> float:
        return covered(x) + 0.1 * abs(x - preferred) / slack

    return min({preferred, *range(lo, hi + 1, 8)}, key=lambda x: (cost(x), x))


def _ink(ffmpeg: Path, font: Path, band: NameBand, workdir: Path) -> tuple[int, int]:
    """Return the name's ink ``(width, offset from x)``, measured by drawing it once."""
    x0 = 20
    graph = (
        f"drawtext=fontfile='{escape_filter_value(font.as_posix())}':textfile=name.txt"
        f":fontsize={band.font_px}:fontcolor=white:x={x0}:y=20,bbox"
    )
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=3000x400:d=0.1",
         "-vf", graph, "-frames:v", "1", "-f", "null", "-"],
        cwd=workdir, capture_output=True, text=True,
    )  # fmt: skip
    found = re.search(r"x1:(\d+) x2:(\d+)", proc.stderr)
    if found is None:  # estimate: centring is then only approximate
        log.warning("could not measure the channel name; estimating", text=band.text)
        return round(text_width_em(band.text) * band.font_px * 0.85), 0
    x1, x2 = int(found[1]), int(found[2])
    return x2 - x1 + 1, x1 - x0


def _loudnorm(ffmpeg: Path, clip: Path, target: float) -> str | None:
    """Return a two-pass ``loudnorm`` filter for ``clip``, or ``None`` to leave it as is.

    The first pass measures the audio; normalising with the measured values is exact
    and linear. Silent (or missing) audio is left alone: loudnorm turns digital
    silence into NaN samples that the AAC encoder refuses.
    """
    head = f"loudnorm=I={target:g}:TP=-1.5:LRA=11"
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostats", "-i", str(clip), "-vn",
         "-af", f"{head}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    start, end = proc.stderr.rfind("{"), proc.stderr.rfind("}")
    try:
        found: dict[str, Any] = json.loads(proc.stderr[start : end + 1]) if 0 <= start < end else {}
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        values = [float(found[k]) for k in keys]
    except (ValueError, KeyError, TypeError):
        return None
    if not all(math.isfinite(v) for v in values) or values[0] < -60:  # silence
        return None
    i, tp, lra, thresh, offset = values
    return (
        f"{head}:measured_I={i:g}:measured_TP={tp:g}:measured_LRA={lra:g}"
        f":measured_thresh={thresh:g}:offset={offset:g}:linear=true"
    )


def _run(args: list[str], cwd: Path) -> None:
    try:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RenderError(
            f"ffmpeg failed ({exc.returncode}): {(exc.stderr or '').strip()[-800:]}"
        ) from exc
    except OSError as exc:
        raise RenderError(f"cannot run ffmpeg: {exc}") from exc


def render_vertical(
    clip: Path,
    out: Path,
    thumbnail: Path,
    *,
    channel: str,
    facecam: FacecamRegion | None,
    output: OutputConfig,
    settings: RenderConfig,
    ffmpeg: Path,
    thumbnail_at_s: float,
    subtitles: Path | None = None,
    title: str | None = None,
    overlay: Sequence[float] | None = None,
) -> None:
    """Render ``clip`` vertically and export a thumbnail.

    Parameters
    ----------
    clip : pathlib.Path
        Horizontal source clip.
    out : pathlib.Path
        Vertical MP4, overwritten. Written to a temporary name and renamed.
    thumbnail : pathlib.Path
        JPEG of the vertical video at ``thumbnail_at_s``.
    channel : str
        Its name and platform are drawn on the video.
    facecam : FacecamRegion or None
        Selects the layout (see the module docstring).
    output : OutputConfig
    settings : RenderConfig
    ffmpeg : pathlib.Path
    thumbnail_at_s : float
        Position of the thumbnail frame, clamped into the video.
    subtitles : pathlib.Path or None
        ASS captions to burn in.
    title : str, optional
        The hook drawn above the channel's name (see :func:`name_band`).
    overlay : sequence of float, optional
        Fixed overlays per source column, kept out of the gameplay (see
        :func:`game_offset`).

    Raises
    ------
    RenderError
        If no font is available or ffmpeg fails.
    """
    font = resolve_font(settings.font_file)
    band = name_band(channel, output, settings, title)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp_out = out.with_name(out.stem + ".part.mp4")
    with tempfile.TemporaryDirectory(prefix="peakreel-render-") as temp:
        workdir = Path(temp)
        (workdir / "name.txt").write_text(band.text, encoding="utf-8")
        for i, line in enumerate(band.title_lines):
            (workdir / f"title{i}.txt").write_text(line, encoding="utf-8")
        band = band.centred(*_ink(ffmpeg, font, band, workdir), output)
        subtitle_name: str | None = None
        if subtitles is not None:
            # libass gets the font through a relative fontsdir: no drive-letter
            # escaping, and no scan of the whole system font directory.
            (workdir / "fonts").mkdir()
            shutil.copyfile(font, workdir / "fonts" / font.name)
            shutil.copyfile(subtitles, workdir / "captions.ass")
            subtitle_name = "captions.ass"
        graph = build_filter(
            output, settings, band, font, facecam, subtitles=subtitle_name, overlay=overlay
        )
        # Every clip as loud as the platforms play: a quiet one gets scrolled past.
        loud = _loudnorm(ffmpeg, clip, settings.loudness_lufs)
        audio = ["-af", loud] if loud else []
        _run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(clip.resolve()),
                "-filter_complex", graph,
                "-map", "[v]", "-map", "0:a?",
                *video_args(ffmpeg, output.video_codec, settings.preset, settings.crf),
                *audio,
                "-c:a", output.audio_codec, "-b:a", settings.audio_bitrate, "-ar", "48000",
                "-movflags", "+faststart",
                str(temp_out.resolve()),
            ],
            workdir,
        )  # fmt: skip
        temp_out.replace(out)
        # A seek past the end yields no frame (short clips, or no spike timing):
        # fall back to the first frame rather than failing the whole render.
        for at in dict.fromkeys((max(thumbnail_at_s, 0.0), 0.0)):
            try:
                _run(
                    [
                        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{at:.3f}", "-i", str(out.resolve()),
                        "-frames:v", "1", "-q:v", "3", str(thumbnail.resolve()),
                    ],
                    workdir,
                )  # fmt: skip
                break
            except RenderError:
                if at == 0.0:
                    raise
                log.warning("thumbnail time past the end; using the first frame", at=at)


def _overlay(clip: Path, ffmpeg: Path) -> list[float] | None:
    """Return the fixed overlays of ``clip`` per column, ``None`` if not measurable."""
    try:
        columns = static_overlay(clip, ffmpeg)
    except Exception:  # the crop then just avoids the webcam, as before
        log.exception("overlay detection failed", clip=clip.name)
        return None
    return None if columns is None else [float(c) for c in columns]


def _captions_file(
    clip: Path, sidecar: Mapping[str, Any], captions: CaptionResult | None
) -> Path | None:
    """Return the ASS captions to burn in: ``captions``, else an earlier render's."""
    if captions is not None:
        return captions.ass_path if captions.status == "ok" else None
    recorded = cast("dict[str, Any]", sidecar.get("captions") or {})
    if recorded.get("status") == "ok" and recorded.get("file"):
        path = clip.with_name(str(recorded["file"]))
        return path if path.exists() else None
    return None


@dataclass(frozen=True, slots=True)
class RenderedClip:
    """Result of :func:`render_clip`.

    Attributes
    ----------
    vertical, thumbnail : pathlib.Path
    layout : str
        ``"blur"`` or ``"split"``.
    """

    vertical: Path
    thumbnail: Path
    layout: str


def render_clip(
    config: Config,
    clip: Path,
    ffmpeg: Path,
    *,
    channel: str | None = None,
    captions: CaptionResult | None = None,
    title: str | None = None,
) -> RenderedClip:
    """Render a clip from the outbox (or any horizontal clip) and update its sidecar.

    Outputs go next to the clip: ``<name>.vertical.mp4`` and ``<name>.jpg``.

    Parameters
    ----------
    config : Config
    clip : pathlib.Path
        Horizontal clip. Its sidecar ``<name>.json`` supplies the channel and the
        spike moment (thumbnail position) when present.
    ffmpeg : pathlib.Path
    channel : str, optional
        Required when the clip has no sidecar.
    captions : CaptionResult, optional
        Burned in when its status is ``ok``; recorded in the sidecar either way.
        Without it, captions an earlier render recorded in the sidecar are burned
        in again (a re-render with a title).
    title : str, optional
        The hook drawn above the channel's name (Gemini's title). A re-render reuses
        the webcam region the first render found.

    Returns
    -------
    RenderedClip

    Raises
    ------
    RenderError
        If the channel is unknown or rendering fails. The sidecar records the error.
    """
    sidecar_path = clip.with_suffix(".json")
    sidecar: dict[str, Any] = {}
    if sidecar_path.exists():
        sidecar = cast("dict[str, Any]", json.loads(sidecar_path.read_text(encoding="utf-8")))
    name = channel or cast("str | None", sidecar.get("channel"))
    if not name:
        raise RenderError(f"{clip}: no sidecar, pass the channel explicitly")
    channel_config: ChannelConfig | None = config.channel(name)
    facecam = channel_config.facecam if channel_config else None
    earlier = cast("dict[str, Any]", sidecar.get("vertical") or {})
    if facecam is None and "facecam" in earlier:  # a re-render: same layout as before
        facecam = FacecamRegion.model_validate(earlier["facecam"]) if earlier["facecam"] else None
    elif facecam is None:
        try:
            facecam = detect_facecam(clip, ffmpeg)
        except Exception:  # a failed detection renders the blur layout, never fails the clip
            log.exception("webcam detection failed", clip=clip.name)
    thumbnail_at = config.clip.pre_roll_s
    window = cast("dict[str, Any]", sidecar.get("window", {}))
    spike = cast("dict[str, Any]", sidecar.get("spike", {}))
    if "start_ts" in window and "start_ts" in spike:
        moment = float(spike["start_ts"]) - float(spike.get("receive_delay_s") or 0.0)
        thumbnail_at = moment - float(window["start_ts"])
    vertical = clip.with_name(clip.stem + ".vertical.mp4")
    thumbnail = clip.with_suffix(".jpg")
    try:
        render_vertical(
            clip,
            vertical,
            thumbnail,
            channel=name,
            facecam=facecam,
            output=config.output,
            settings=config.render,
            ffmpeg=ffmpeg,
            thumbnail_at_s=thumbnail_at,
            subtitles=_captions_file(clip, sidecar, captions),
            title=title,
            overlay=_overlay(clip, ffmpeg) if facecam is not None else None,
        )
    except RenderError as exc:
        if sidecar:
            sidecar["render_error"] = str(exc)
            sidecar_path.write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        raise
    result = RenderedClip(vertical, thumbnail, "split" if facecam else "blur")
    if sidecar:
        sidecar.pop("render_error", None)
        sidecar["vertical"] = {
            "file": vertical.name,
            "thumbnail": thumbnail.name,
            "layout": result.layout,
            "facecam": None if facecam is None else facecam.model_dump(),
            "title": title or None,
        }
        if captions is not None:
            sidecar["captions"] = captions.to_sidecar()
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    log.info("rendered vertical", clip=clip.name, layout=result.layout)
    return result
