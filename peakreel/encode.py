"""Video encoder arguments for ffmpeg: the GPU's encoder (NVENC) when it works.

libx264 on every core made the machine run hot while clipping (2026-11-19): each
clip is encoded twice (the cut, then the vertical render). NVENC does the same work
on the graphics card's dedicated encoder, leaving the CPU nearly idle.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from peakreel.log import get_logger

__all__ = ["video_args"]

log = get_logger(__name__)
_NVENC_CQ_OFFSET = 6


@functools.cache
def _encoder_works(ffmpeg: Path, codec: str) -> bool:
    """Whether ``codec`` encodes a tiny test video (a driver or card may lack it)."""
    try:
        subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x256:d=0.2", "-c:v", codec, "-f", "null", "-"],
            check=True, capture_output=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        log.warning("video encoder unavailable, using libx264 on the CPU", codec=codec)
        return False
    return True


def video_args(ffmpeg: Path, codec: str, preset: str, crf: int) -> list[str]:
    """Return the ffmpeg arguments that encode the video with ``codec``.

    Parameters
    ----------
    ffmpeg : pathlib.Path
        The ffmpeg executable (probed once per encoder).
    codec : str
        ``libx264``, or an NVENC encoder such as ``h264_nvenc``. An NVENC encoder
        that does not work on this machine falls back to ``libx264``.
    preset : str
        libx264 preset (``veryfast``); NVENC uses its own quality preset.
    crf : int
        Constant quality on libx264's scale (``-crf``); NVENC gets ``-cq`` 6 higher,
        which gave the same size and look.

    Returns
    -------
    list of str
        Starting with ``-c:v``.

    Examples
    --------
    >>> video_args(Path("ffmpeg"), "libx264", "veryfast", 20)
    ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20']
    """
    if codec.endswith("_nvenc"):
        if _encoder_works(ffmpeg, codec):
            # NVENC's -cq 26 matched libx264 -crf 20 in size and look (SSIM 0.992,
            # measured 2026-11-19 on a split-layout clip); -cq 20 doubled the file.
            return ["-c:v", codec, "-preset", "p5", "-tune", "hq",
                    "-rc", "vbr", "-cq", str(crf + _NVENC_CQ_OFFSET), "-b:v", "0"]  # fmt: skip
        codec = "libx264"
    return ["-c:v", codec, "-preset", preset, "-crf", str(crf)]
