from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from peakreel.render.facecam import overlay_box, stable_face


def test_overlay_box_snaps_to_a_border_that_stays_put() -> None:
    rng = np.random.default_rng(0)
    frames: list[NDArray[np.uint8]] = []
    for _ in range(6):
        frame = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)  # moving gameplay
        frame[150:270, 20:130] = 60  # the webcam overlay...
        frame[150:270, 20:130][[0, -1]] = 255  # ...with a bright frame
        frame[150:270, 20:130][:, [0, -1]] = 255
        frames.append(frame)
    x, y, w, h = overlay_box((60.0, 180.0, 30.0, 40.0), frames)
    assert abs(x - 20) <= 2 and abs(y - 150) <= 2
    assert abs(x + w - 129) <= 2 and abs(y + h - 269) <= 2


def test_a_face_seen_in_too_few_frames_is_not_a_webcam() -> None:
    cam = (40.0, 180.0, 36.0, 48.0)
    assert stable_face([[cam], [cam], [cam], []]) == cam
    assert stable_face([[cam], [], [], [(300.0, 90.0, 40.0, 50.0)]]) is None
    assert stable_face([]) is None


def test_overlay_columns_find_fixed_graphics_and_chat_boxes() -> None:
    from peakreel.render.facecam import overlay_columns

    rng = np.random.default_rng(1)
    frames: list[NDArray[np.uint8]] = []
    for i in range(6):
        frame = np.full((360, 640, 3), 90, np.uint8)
        frame[:, :300] = rng.integers(0, 255, (360, 300, 3), dtype=np.uint8)  # moving game
        frame[20:60, 400:520] = 255  # a fixed counter...
        frame[30:50, 420:500] = 0  # ...with sharp edges
        for row in range(80, 200, 12):  # a chat box: text lines changing every frame
            frame[row : row + 4, 560 + (i % 3) * 4 : 630] = 255
        frames.append(frame)
    columns = overlay_columns(frames)
    assert columns[400:520].max() > 0.08  # the counter
    assert columns[565:625].max() > 0.08  # the chat box
    assert columns[340:390].max() < 0.02  # plain background: nothing there
