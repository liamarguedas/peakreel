"""Render a horizontal clip as a 1080x1920 vertical video with a credit overlay.

Layouts
-------
``blur`` (default)
    The gameplay, scaled to the full output width, centred over a blurred and
    darkened copy of itself that fills the frame.
``split``
    Used when the channel has a ``facecam`` region: the facecam crop on top
    (``render.facecam_height_fraction`` of the height), the centre of the gameplay
    below.

Safe zones
----------
Platform UI (captions, buttons) covers the bottom ``output.safe_zone_bottom`` and
the right ``output.safe_zone_right`` of the frame. The credit is drawn lower-left,
just above the bottom zone, and its font size is chosen so the text (with its
background box) cannot reach the right zone. :func:`credit_layout` computes the
placement and an upper bound of the text box, and tests assert that bound is inside
the safe area.

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
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from peakreel.config import ChannelConfig, Config, FacecamRegion, OutputConfig, RenderConfig
from peakreel.log import get_logger

__all__ = [
    "FONT_CANDIDATES",
    "CreditLayout",
    "RenderError",
    "RenderedClip",
    "build_filter",
    "credit_layout",
    "credit_text",
    "escape_filter_value",
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

_BOX_BORDER_PX = 14
_MIN_FONT_PX = 20


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


def credit_text(channel: str, credit: str | None) -> str:
    """Return the one-line credit drawn on the video.

    Parameters
    ----------
    channel : str
        Twitch login.
    credit : str or None
        The channel's configured credit.

    Returns
    -------
    str
        The credit, with ``twitch.tv/<channel>`` appended when it does not already
        name the channel.

    Examples
    --------
    >>> credit_text("alpha", None)
    'Clip from twitch.tv/alpha'
    >>> credit_text("alpha", "Thanks Alpha!")
    'Thanks Alpha! · twitch.tv/alpha'
    """
    text = (credit or f"Clip from twitch.tv/{channel}").strip()
    if channel.lower() not in text.lower():
        text = f"{text} · twitch.tv/{channel}"
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
class CreditLayout:
    """Where and how large the credit is drawn.

    Attributes
    ----------
    text : str
        Possibly truncated with an ellipsis.
    font_px : int
    x : int
        Left edge of the text.
    bottom : int
        Bottom edge of the text (ffmpeg ``y = bottom - th``).
    box : tuple of (int, int, int, int)
        Upper bound of the drawn box including its border: ``(x0, y0, x1, y1)``.
    """

    text: str
    font_px: int
    x: int
    bottom: int
    box: tuple[int, int, int, int]


def credit_layout(text: str, output: OutputConfig, settings: RenderConfig) -> CreditLayout:
    """Place the credit inside the safe area and size it to fit.

    Parameters
    ----------
    text : str
        From :func:`credit_text`.
    output : OutputConfig
        Frame size and safe zones.
    settings : RenderConfig

    Returns
    -------
    CreditLayout
        ``box`` never enters the bottom or right unsafe zone.
    """
    border = _BOX_BORDER_PX
    margin = settings.margin_px
    safe_right = math.floor(output.width * (1 - output.safe_zone_right))
    safe_bottom = math.floor(output.height * (1 - output.safe_zone_bottom))
    x = margin + border
    bottom = safe_bottom - margin - border
    available = safe_right - x - border - margin // 2
    font_px = min(settings.credit_max_font_px, math.floor(available / max(text_width_em(text), 1)))
    if font_px < _MIN_FONT_PX:
        font_px = _MIN_FONT_PX
        while text and text_width_em(text + "…") * font_px > available:
            text = text[:-1]
        text = text.rstrip() + "…"
    width = math.ceil(text_width_em(text) * font_px)
    line_height = math.ceil(font_px * 1.3)  # ascender plus descender upper bound
    box = (x - border, bottom - line_height - border, x + width + border, bottom + border)
    return CreditLayout(text=text, font_px=font_px, x=x, bottom=bottom, box=box)


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
    credit: CreditLayout,
    font: Path,
    facecam: FacecamRegion | None,
    *,
    text_file: str = "credit.txt",
) -> str:
    """Return the ``-filter_complex`` graph producing ``[v]``.

    Parameters
    ----------
    output : OutputConfig
    settings : RenderConfig
    credit : CreditLayout
    font : pathlib.Path
    facecam : FacecamRegion or None
        ``None`` selects the ``blur`` layout, a region the ``split`` layout.
    text_file : str, default "credit.txt"
        Relative path of the file holding the credit text.

    Returns
    -------
    str
    """
    w, h = output.width, output.height
    if facecam is None:
        base = (
            "[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"gblur=sigma={settings.blur_sigma:g},eq=brightness=-0.12[bg];"
            f"[fgsrc]scale={w}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        )
    else:
        cam_h = 2 * round(h * settings.facecam_height_fraction / 2)
        game_h = h - cam_h
        base = (
            "[0:v]split=2[camsrc][gamesrc];"
            f"[camsrc]crop=iw*{facecam.w:g}:ih*{facecam.h:g}:iw*{facecam.x:g}:ih*{facecam.y:g},"
            f"scale={w}:{cam_h}:force_original_aspect_ratio=increase,crop={w}:{cam_h}[cam];"
            f"[gamesrc]scale=-2:{game_h},crop={w}:{game_h}[game];"
            "[cam][game]vstack=inputs=2[base];"
        )
    draw = (
        f"drawtext=fontfile='{escape_filter_value(font.as_posix())}':textfile={text_file}"
        f":fontsize={credit.font_px}:fontcolor=white:x={credit.x}:y={credit.bottom}-th"
        f":box=1:boxcolor=black@0.5:boxborderw={_BOX_BORDER_PX}"
    )
    return f"{base}[base]{draw},fps={output.fps},format={output.pixel_format}[v]"


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
    credit: str | None,
    facecam: FacecamRegion | None,
    output: OutputConfig,
    settings: RenderConfig,
    ffmpeg: Path,
    thumbnail_at_s: float,
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
    credit : str or None
        Configured credit for the channel.
    facecam : FacecamRegion or None
        Selects the layout (see the module docstring).
    output : OutputConfig
    settings : RenderConfig
    ffmpeg : pathlib.Path
    thumbnail_at_s : float
        Position of the thumbnail frame, clamped into the video.

    Raises
    ------
    RenderError
        If no font is available or ffmpeg fails.
    """
    font = resolve_font(settings.font_file)
    layout = credit_layout(credit_text(channel, credit), output, settings)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp_out = out.with_name(out.stem + ".part.mp4")
    with tempfile.TemporaryDirectory(prefix="peakreel-render-") as temp:
        workdir = Path(temp)
        (workdir / "credit.txt").write_text(layout.text, encoding="utf-8")
        graph = build_filter(output, settings, layout, font, facecam)
        _run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(clip.resolve()),
                "-filter_complex", graph,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", output.video_codec, "-preset", settings.preset, "-crf", str(settings.crf),
                "-c:a", output.audio_codec, "-b:a", settings.audio_bitrate, "-ar", "48000",
                "-movflags", "+faststart",
                str(temp_out.resolve()),
            ],
            workdir,
        )  # fmt: skip
        temp_out.replace(out)
        _run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{max(thumbnail_at_s, 0.0):.3f}", "-i", str(out.resolve()),
                "-frames:v", "1", "-q:v", "3", str(thumbnail.resolve()),
            ],
            workdir,
        )  # fmt: skip


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
    config: Config, clip: Path, ffmpeg: Path, *, channel: str | None = None
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
            credit=channel_config.credit_text if channel_config else None,
            facecam=facecam,
            output=config.output,
            settings=config.render,
            ffmpeg=ffmpeg,
            thumbnail_at_s=thumbnail_at,
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
        }
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    log.info("rendered vertical", clip=clip.name, layout=result.layout)
    return result
