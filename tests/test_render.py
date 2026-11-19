from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from peakreel.config import (
    Config,
    ConfigError,
    FacecamRegion,
    OutputConfig,
    RenderConfig,
    load_config,
    resolve_tool,
)
from peakreel.render.vertical import (
    RenderError,
    build_filter,
    credit_text,
    escape_filter_value,
    name_band,
    render_clip,
    resolve_font,
    text_width_em,
)
from tests.conftest import WriteConfig

FFMPEG = resolve_tool("ffmpeg", Path.cwd())
FFPROBE = resolve_tool("ffprobe", Path.cwd())
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg not installed")


@pytest.mark.parametrize(
    "channel",
    [
        "a",
        "averyveryverylongchannelname_2026",
        "kick-westcol",
        "加藤純一加藤純一加藤純一加藤純一",
        "W" * 200,
    ],
)
@pytest.mark.parametrize("output", [OutputConfig(), OutputConfig(safe_zone_right=0.3)])
def test_name_band_is_centred_between_the_side_safe_zones(
    channel: str, output: OutputConfig
) -> None:
    band = name_band(channel, output, RenderConfig())
    ink = int(text_width_em(band.text) * band.font_px)  # an upper bound of the measured width
    placed = band.centred(ink, 2, output)
    left, right = placed.icon_x, placed.text_x + 2 + ink
    assert left >= output.width * output.safe_zone_right
    assert right <= output.width * (1 - output.safe_zone_right)
    assert abs((left + right) - output.width) <= 1  # icon and name centred together
    assert 20 <= band.font_px <= RenderConfig().credit_max_font_px
    assert "twitch.tv" not in band.text and "kick" not in band.text


def test_long_name_is_truncated_not_overflowing() -> None:
    band = name_band("W" * 200, OutputConfig(), RenderConfig())
    assert band.text.endswith("…")
    assert band.font_px == 20


def test_credit_text_and_width_estimate() -> None:
    assert credit_text("alpha", None) == "Clip from twitch.tv/alpha"
    assert credit_text("alpha", "by ALPHA") == "by ALPHA"
    assert credit_text("alpha", "Thanks!") == "Thanks! · twitch.tv/alpha"
    assert text_width_em("ab") == pytest.approx(1.3)
    assert text_width_em("加藤") == 2.0


def test_escape_filter_value() -> None:
    assert escape_filter_value("C:/Windows/Fonts/a.ttf") == r"C\:/Windows/Fonts/a.ttf"
    assert escape_filter_value(r"C:\Fonts\it's.ttf") == r"C\:/Fonts/it\'s.ttf"


def test_build_filter_layouts() -> None:
    output, settings = OutputConfig(), RenderConfig()
    band = name_band("kick-westcol", output, settings).centred(200, 0, output)
    blur = build_filter(output, settings, band, Path("C:/f.ttf"), None)
    assert "gblur=sigma=30" in blur
    assert "overlay=(W-w)/2:(H-h)/2" in blur
    assert r"fontfile='C\:/f.ttf'" in blur
    assert "color=0x53FC18" in blur and "text=K" in blur  # Kick icon
    assert f"y={(1920 - 608) // 2 - band.height}:" in blur  # the band sits right above the game
    assert blur.endswith(",fps=30,format=yuv420p[v]")
    left_cam = FacecamRegion(x=0.0, y=0.4, w=0.2, h=0.3)
    split = build_filter(output, settings, band, Path("/f.ttf"), left_cam)
    assert "crop=iw*0.2:ih*0.3:iw*0:ih*0.4" in split
    assert "scale=1080:672" in split  # 35% of 1920, even
    assert "scale=1512:-2,crop=1080:850:432:" in split  # zoomed 1.4, cut on the webcam side
    assert f"overlay=0:{672 + band.height}" in split
    right_cam = FacecamRegion(x=0.8, y=0.1, w=0.2, h=0.3)
    assert "crop=1080:850:0:" in build_filter(output, settings, band, Path("/f.ttf"), right_cam)


@pytest.mark.parametrize(
    "region", [{"x": 0.9, "y": 0, "w": 0.2, "h": 0.2}, {"x": 0, "y": 0.9, "w": 0.2, "h": 0.2}]
)
def test_facecam_region_must_fit_in_frame(
    write_config: WriteConfig, region: dict[str, float]
) -> None:
    path = write_config({"channels": [{"name": "a", "facecam": region}]})
    with pytest.raises(ConfigError, match="extends past"):
        load_config(path)


def test_resolve_font(tmp_path: Path) -> None:
    font = tmp_path / "f.ttf"
    font.write_bytes(b"")
    assert resolve_font(font) == font
    with pytest.raises(RenderError, match="not found"):
        resolve_font(tmp_path / "missing.ttf")


# -- real renders --------------------------------------------------------------------


def make_clip(path: Path) -> None:
    assert FFMPEG is not None
    subprocess.run(
        [
            str(FFMPEG), "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path),
        ],
        check=True,
    )  # fmt: skip


def probe(path: Path) -> dict[str, Any]:
    assert FFPROBE is not None
    out = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)


def render_config(write_config: WriteConfig, facecam: dict[str, float] | None) -> Config:
    channel: dict[str, object] = {"name": "alpha", "credit": "アルファ / twitch.tv/alpha"}
    if facecam:
        channel["facecam"] = facecam
    return load_config(write_config({"channels": [channel], "render": {"preset": "ultrafast"}}))


@needs_ffmpeg
@pytest.mark.parametrize("facecam", [None, {"x": 0.8, "y": 0.5, "w": 0.2, "h": 0.4}])
def test_render_clip_produces_vertical_video_thumbnail_and_sidecar(
    write_config: WriteConfig, tmp_path: Path, facecam: dict[str, float] | None
) -> None:
    assert FFMPEG is not None
    config = render_config(write_config, facecam)
    clip = tmp_path / "c1.mp4"
    make_clip(clip)
    sidecar = clip.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "channel": "alpha",
                "window": {"start_ts": 100.0},
                "spike": {"start_ts": 102.5, "receive_delay_s": 0.5},
            }
        ),
        encoding="utf-8",
    )
    result = render_clip(config, clip, FFMPEG)
    assert result.layout == ("split" if facecam else "blur")
    info = probe(result.vertical)
    streams = {s["codec_type"]: s for s in info["streams"]}
    video = streams["video"]
    assert (video["width"], video["height"], video["pix_fmt"], video["r_frame_rate"]) == (
        1080,
        1920,
        "yuv420p",
        "30/1",
    )
    assert streams["audio"]["codec_name"] == "aac"
    assert abs(float(info["format"]["duration"]) - 4.0) < 0.1
    assert result.thumbnail.stat().st_size > 0
    assert not list(tmp_path.glob("*.part.mp4"))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["vertical"] == {
        "file": "c1.vertical.mp4",
        "thumbnail": "c1.jpg",
        "layout": result.layout,
        "facecam": facecam,
    }


@needs_ffmpeg
def test_render_errors_are_raised_and_noted_in_sidecar(
    write_config: WriteConfig, tmp_path: Path
) -> None:
    assert FFMPEG is not None
    config = render_config(write_config, None)
    bare = tmp_path / "bare.mp4"
    bare.write_bytes(b"not a video")
    with pytest.raises(RenderError, match="pass the channel"):
        render_clip(config, bare, FFMPEG)
    bare.with_suffix(".json").write_text(json.dumps({"channel": "alpha"}), encoding="utf-8")
    with pytest.raises(RenderError, match="ffmpeg failed"):
        render_clip(config, bare, FFMPEG)
    assert (
        "ffmpeg failed"
        in json.loads(bare.with_suffix(".json").read_text(encoding="utf-8"))["render_error"]
    )
