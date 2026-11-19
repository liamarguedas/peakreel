from __future__ import annotations

from pathlib import Path

from peakreel.encode import video_args


def test_nvenc_falls_back_to_libx264_without_the_encoder(tmp_path: Path) -> None:
    missing = tmp_path / "no-ffmpeg.exe"
    assert video_args(missing, "h264_nvenc", "veryfast", 20) == [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    ]  # fmt: skip
