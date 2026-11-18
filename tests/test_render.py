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
    credit_layout,
    credit_text,
    escape_filter_value,
    render_clip,
    resolve_font,
    text_width_em,
)
from tests.conftest import WriteConfig

FFMPEG = resolve_tool("ffmpeg", Path.cwd())
FFPROBE = resolve_tool("ffprobe", Path.cwd())
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg not installed")


@pytest.mark.parametrize(
    "credit",
    [
        "Clip from twitch.tv/a",
        "Clip from twitch.tv/averyveryverylongchannelname_2026",
        "加藤純一 / twitch.tv/kato_junichi0817",
        "加藤純一加藤純一加藤純一加藤純一加藤純一加藤純一加藤純一加藤純一",
        "W" * 200,
    ],
)
@pytest.mark.parametrize(
    "output", [OutputConfig(), OutputConfig(safe_zone_right=0.3, safe_zone_bottom=0.35)]
)
def test_credit_box_never_enters_unsafe_zones(credit: str, output: OutputConfig) -> None:
    layout = credit_layout(credit, output, RenderConfig())
    x0, y0, x1, y1 = layout.box
    assert x0 >= 0
    assert y0 >= 0
    assert x1 <= output.width * (1 - output.safe_zone_right)
    assert y1 <= output.height * (1 - output.safe_zone_bottom)
    assert 20 <= layout.font_px <= RenderConfig().credit_max_font_px


def test_long_credit_is_truncated_not_overflowing() -> None:
    layout = credit_layout("W" * 200, OutputConfig(), RenderConfig())
    assert layout.text.endswith("…")
    assert layout.font_px == 20


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
    layout = credit_layout("x", output, settings)
    blur = build_filter(output, settings, layout, Path("C:/f.ttf"), None)
    assert "gblur=sigma=30" in blur
    assert "overlay=(W-w)/2:(H-h)/2" in blur
    assert r"fontfile='C\:/f.ttf'" in blur
    assert blur.endswith(",fps=30,format=yuv420p[v]")
    split = build_filter(
        output, settings, layout, Path("/f.ttf"), FacecamRegion(x=0.8, y=0.5, w=0.2, h=0.4)
    )
    assert "crop=iw*0.2:ih*0.4:iw*0.8:ih*0.5" in split
    assert "scale=1080:672" in split  # 35% of 1920, even
    assert "crop=1080:1248" in split
    assert "vstack=inputs=2" in split


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
