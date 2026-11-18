"""Music-likely score from beat periodicity.

Streamers often play music, and platforms mute or strike clips that contain
licensed tracks. Recognising a specific track needs a song catalogue, which a
local tool does not have, so this only answers "is there music with a steady
beat?": the reviewer then listens. Game soundtracks score high too.

Method: a spectral-flux onset envelope (how much new energy each 16 ms frame
adds), then its autocorrelation over the lags of 70 to 180 beats per minute. A
steady beat repeats at a fixed lag and gives a strong peak; speech and game
effects do not. On the M7 run's 46 clips (talk and gameplay) the score was 0.04
to 0.21; a synthetic 120 BPM track scored 0.85, and 0.66 with a streamer talking
over it at a louder level.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["music_score"]

_HOP = 256
_WINDOW = 1024


def music_score(samples: np.ndarray[Any, np.dtype[np.float32]], sample_rate: int = 16_000) -> float:
    """Score how strongly ``samples`` contain a steady musical beat.

    Parameters
    ----------
    samples : numpy.ndarray of float32
        Mono audio, for example from :func:`peakreel.captions.whisper.decode_audio`.
    sample_rate : int, default 16000

    Returns
    -------
    float
        0 (no periodic beat) to 1 (a perfectly regular beat). Below about 6 s of
        audio the score is 0.

    Examples
    --------
    >>> import numpy as np
    >>> t = np.arange(16_000 * 20) / 16_000
    >>> beat = (np.sin(2 * np.pi * 60 * t) * np.exp(-12 * np.mod(t, 0.5))).astype(np.float32)
    >>> music_score(beat) > 0.5
    True
    >>> noise = np.random.default_rng(0).standard_normal(t.size).astype(np.float32)
    >>> music_score(noise) < 0.2
    True
    """
    frames = (len(samples) - _WINDOW) // _HOP
    frame_rate = sample_rate / _HOP
    if frames < 6 * frame_rate:
        return 0.0
    index = np.arange(_WINDOW)[None, :] + _HOP * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(samples[index] * np.hanning(_WINDOW), axis=1))
    log_spectrum = np.log1p(100 * spectrum)
    flux = np.maximum(np.diff(log_spectrum, axis=0), 0).sum(axis=1)
    flux = np.maximum(flux - np.convolve(flux, np.ones(32) / 32, mode="same"), 0)
    spread = flux.std()
    if spread < 1e-9:
        return 0.0
    flux = (flux - flux.mean()) / spread
    correlation = np.correlate(flux, flux, "full")[len(flux) - 1 :]
    correlation /= correlation[0]
    shortest = int(frame_rate * 60 / 180)
    longest = int(frame_rate * 60 / 70)
    return round(max(0.0, float(correlation[shortest : longest + 1].max())), 3)
