"""Find the streamer's webcam overlay in a clip, for the ``split`` layout.

Every keyframe of the clip is decoded at 640x360 and searched for faces with YuNet
(``face_detection_yunet_2023mar.onnx``, from the OpenCV model zoo, MIT licence).

- **The webcam is the face that stays put.** An overlay sits at a fixed place on the
  stream, while game characters move. The face found at the same place in most
  keyframes (:data:`STABLE_SHARE`) is taken as the webcam.
- **A face in the middle is not an overlay.** A webcam overlay sits towards the left or
  right edge; a face mid-frame is the whole stream (just chatting, IRL), which keeps the
  ``blur`` layout.
- **The overlay's border stays put too.** Edges are averaged over time with a median,
  which keeps only lines present in every keyframe: the overlay frame. The crop snaps
  to the strongest such line on each side of the face, or uses a default margin
  around it (a corner webcam has no border on the frame-edge side, a cut-out webcam
  none at all).

Measured on the launch-day clips of 16 channels: the webcam was found in 12; the
4 others (no webcam, a webcam mid-frame, a cut-out webcam, an IRL stream) keep the
``blur`` layout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from peakreel.config import FacecamRegion

__all__ = [
    "STABLE_SHARE",
    "detect_facecam",
    "overlay_box",
    "overlay_columns",
    "stable_face",
    "static_overlay",
]

_MODEL = Path(__file__).with_name("face_detection_yunet_2023mar.onnx")
_W, _H = 640, 360
STABLE_SHARE = 0.6
"""A face must be found at the same place in this share of the keyframes."""

Box = tuple[float, float, float, float]  # x, y, w, h in 640x360 pixels


def _keyframes(clip: Path, ffmpeg: Path) -> list[NDArray[np.uint8]]:
    """Decode the clip's keyframes (every 2 s on Twitch) as 640x360 BGR images."""
    proc = subprocess.run(
        [str(ffmpeg), "-v", "error", "-skip_frame", "nokey", "-i", str(clip),
         "-fps_mode", "vfr", "-vf", f"scale={_W}:{_H}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    size = _W * _H * 3
    data = proc.stdout
    return [
        np.frombuffer(data, np.uint8, size, offset).reshape(_H, _W, 3)
        for offset in range(0, len(data) - size + 1, size)
    ]


def stable_face(faces: list[list[Box]]) -> Box | None:
    """Return a face found at the same place in :data:`STABLE_SHARE` of the frames.

    Parameters
    ----------
    faces : list of list of (x, y, w, h)
        The faces found in each frame.

    Returns
    -------
    tuple or None
        The face (from the frame where it was first seen) or ``None``.

    Examples
    --------
    >>> cam = (40.0, 180.0, 36.0, 48.0)
    >>> stable_face([[cam], [(41.0, 181.0, 35.0, 47.0)], [], [cam, (300.0, 100.0, 40.0, 50.0)]])
    (40.0, 180.0, 36.0, 48.0)
    >>> stable_face([[cam], [], [], []]) is None
    True
    """
    best: tuple[int, Box | None] = (0, None)
    for x, y, w, h in (box for frame in faces for box in frame):
        cx, cy = x + w / 2, y + h / 2
        seen = sum(
            any(
                abs(x2 + w2 / 2 - cx) < 0.04 * _W
                and abs(y2 + h2 / 2 - cy) < 0.06 * _H
                and 0.7 < w2 / w < 1.4
                for x2, y2, w2, h2 in frame
            )
            for frame in faces
        )
        if seen > best[0]:
            best = (seen, (x, y, w, h))
    return best[1] if faces and best[0] >= STABLE_SHARE * len(faces) else None


def _strongest(profile: NDArray[np.float32], start: float, stop: float, default: float) -> float:
    """Return the strongest line between ``start`` and ``stop``, else ``default``."""
    last = len(profile) - 1
    lo, hi = sorted((int(min(max(start, 0), last)), int(min(max(stop, 0), last))))
    if hi - lo < 2:
        return default
    segment = profile[lo : hi + 1]
    i = int(np.argmax(segment))
    # A real border stands far above the typical edge strength of the picture.
    return float(lo + i) if segment[i] > 4 * (float(np.median(profile)) + 1) else default


def overlay_box(face: Box, frames: list[NDArray[np.uint8]]) -> Box:
    """Return the webcam overlay around ``face``: snapped to its border where it has one.

    Parameters
    ----------
    face : tuple of (x, y, w, h)
        The webcam face, in 640x360 pixels.
    frames : list of numpy.ndarray
        640x360 BGR keyframes.

    Returns
    -------
    tuple of (x, y, w, h)
        In 640x360 pixels, inside the frame.
    """
    gx: list[NDArray[np.float32]] = []
    gy: list[NDArray[np.float32]] = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx.append(np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)))
        gy.append(np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)))
    ex = np.median(np.stack(gx), axis=0)  # lines in every frame survive the median
    ey = np.median(np.stack(gy), axis=0)
    x, y, w, h = face
    rows = slice(int(max(0, y - 0.3 * h)), int(min(_H, y + 1.6 * h)))
    cols = slice(int(max(0, x - 0.3 * w)), int(min(_W, x + 1.3 * w)))
    columns: NDArray[np.float32] = ex[rows].mean(axis=0)
    lines: NDArray[np.float32] = ey[:, cols].mean(axis=1)
    left = _strongest(columns, x - 0.4 * w, x - 3.0 * w, max(0, x - 1.6 * w))
    right = _strongest(columns, x + 1.4 * w, x + 4.0 * w, min(_W, x + 2.6 * w))
    top = _strongest(lines, y - 0.3 * h, y - 2.0 * h, max(0, y - 1.0 * h))
    bottom = _strongest(lines, y + 1.4 * h, y + 3.5 * h, min(_H, y + 2.4 * h))
    return left, top, right - left, bottom - top


def detect_facecam(clip: Path, ffmpeg: Path) -> FacecamRegion | None:
    """Find the webcam overlay of ``clip``.

    Parameters
    ----------
    clip : pathlib.Path
        The horizontal clip.
    ffmpeg : pathlib.Path

    Returns
    -------
    FacecamRegion or None
        Fractions of the frame; ``None`` when there is no webcam overlay (none found,
        found in too few keyframes, or a face mid-frame).

    Raises
    ------
    subprocess.CalledProcessError
        If ffmpeg cannot decode the clip.
    """
    frames = _keyframes(clip, ffmpeg)
    if len(frames) < 3:
        return None
    detector: Any = cv2.FaceDetectorYN.create(str(_MODEL), "", (_W, _H), 0.7)
    faces: list[list[Box]] = []
    for frame in frames:
        _, found = detector.detect(frame)
        rows: list[Any] = [] if found is None else list(found)
        faces.append([(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in rows])
    face = stable_face(faces)
    if face is None:
        return None
    x, _, w, _ = face
    if 0.3 * _W < x + w / 2 < 0.7 * _W:  # mid-frame: the stream is the camera
        return None
    left, top, width, height = overlay_box(face, frames)
    return FacecamRegion(
        x=round(left / _W, 4),
        y=round(top / _H, 4),
        w=round(min(width / _W, 1 - left / _W), 4),
        h=round(min(height / _H, 1 - top / _H), 4),
    )


def overlay_columns(frames: list[NDArray[np.uint8]]) -> NDArray[np.float32]:
    """Return, per column of the frame, the share of it covered by a fixed overlay.

    Two kinds count. Fixed graphics (a counter, a goal bar, the webcam's frame) keep
    their pixels while the game moves under it and have sharp edges: still pixels on
    an edge of the median frame. A chat box changes all the time, but its text puts
    edges in the same place in every keyframe (seen on stableronaldo and cellbit).

    Parameters
    ----------
    frames : list of numpy.ndarray
        640x360 BGR keyframes (at least three).

    Returns
    -------
    numpy.ndarray
        640 values from 0 (nothing fixed) to 1 (fixed from top to bottom).
    """
    grays = [np.asarray(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), dtype=np.uint8) for f in frames]
    gray = np.stack(grays)
    still = gray.astype(np.float32).std(axis=0) < 6
    median: NDArray[np.uint8] = np.median(gray, axis=0).astype(np.uint8)
    edges = cv2.dilate(cv2.Canny(median, 60, 160), np.ones((7, 7), np.uint8)) > 0
    kernel = np.ones((5, 5), np.uint8)
    per_frame: list[NDArray[np.bool_]] = [
        cv2.dilate(cv2.Canny(g, 80, 200), kernel) > 0 for g in grays
    ]
    edgy = np.stack(per_frame).mean(axis=0)
    fixed = (still & edges) | (edgy >= 0.9)
    columns: NDArray[np.float32] = fixed.mean(axis=0).astype(np.float32)
    return columns


def static_overlay(clip: Path, ffmpeg: Path) -> NDArray[np.float32] | None:
    """Return :func:`overlay_columns` of ``clip``, or ``None`` with too few keyframes."""
    frames = _keyframes(clip, ffmpeg)
    return overlay_columns(frames) if len(frames) >= 3 else None
