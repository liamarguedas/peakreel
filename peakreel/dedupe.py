"""Find clips of the same moment: co-streams, rebroadcasts and repeated cuts.

Two streamers watching the same event (a tournament co-stream, a shared watch
party) spike on the same moment. Their clips differ in facecam and commentary but
share the program audio, so they are matched by audio fingerprint rather than by
time alone: two streamers who both react to a game launch at the same time are not
duplicates, and are not matched.

Each clip gets a Chromaprint fingerprint (``<clip>.fp``, made by ffmpeg's
``chromaprint`` muxer): one 32-bit item per 0.124 s of audio. Clips that start
within ``dedupe.window_s`` of each other are compared at every offset up to
``dedupe.max_offset_s`` (streams reach viewers with different delays). At each
offset, the fraction of items that nearly match (at most 10 of 32 bits differ) is
measured. Shared audio makes that fraction jump at exactly one offset, the delay
between the streams; unrelated clips can also score high (quiet passages and a
channel's recurring sounds look alike) but at every offset. So the match
strength is how far the peak stands out: ``(peak - median) / MAD`` over offsets.

Measured on the M7 run's 46 clips (1035 unrelated pairs, many from the same
channel): strength at most 13.2. Synthetic co-streams (one real clip delayed 3 s,
plus a whole other clip, game and voice, mixed in): 18 to 45 when the other audio
was at most half as loud, 14 to 40 at equal loudness. The default threshold, 16,
favours precision: a false duplicate hides a good clip, a missed one only shows
two clips of the same moment in review.

Of a matching pair the better clip is kept: one already approved wins, then the
higher spike score, then the earlier clip. The other gets ``duplicate_of`` set; its
state is unchanged, and the reviewer can still approve it.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from peakreel.config import DedupeConfig
from peakreel.log import get_logger

__all__ = [
    "FINGERPRINT_ITEM_S",
    "DedupeDecision",
    "Match",
    "fingerprint",
    "fingerprint_path",
    "load_fingerprint",
    "resolve_duplicates",
    "similarity",
]

log = get_logger(__name__)

FINGERPRINT_ITEM_S = 4096 / 3 / 11025
"""Seconds of audio per fingerprint item (Chromaprint: 4096-sample frames, step 1/3, 11025 Hz)."""

_MIN_OVERLAP_S = 10.0
_NEAR_BITS = 10
_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def fingerprint_path(clip: Path) -> Path:
    """Where the fingerprint of ``clip`` is stored (``<clip>.fp``)."""
    return clip.with_suffix(".fp")


def fingerprint(clip: Path, ffmpeg: Path) -> Path:
    """Fingerprint the audio of ``clip`` and store it next to the clip.

    Parameters
    ----------
    clip : pathlib.Path
        Any media file with an audio stream.
    ffmpeg : pathlib.Path
        ffmpeg built with Chromaprint (gyan.dev and BtbN builds include it).

    Returns
    -------
    pathlib.Path
        The fingerprint file: raw little-endian uint32 items.

    Raises
    ------
    RuntimeError
        If ffmpeg fails (no audio, or no Chromaprint support).
    """
    out = fingerprint_path(clip)
    temp = out.with_suffix(".fp.part")
    result = subprocess.run(
        [str(ffmpeg), "-v", "error", "-y", "-i", str(clip), "-map", "0:a:0",
         "-f", "chromaprint", "-fp_format", "raw", str(temp)],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"fingerprint failed for {clip.name}: {result.stderr.strip()[-300:]}")
    temp.replace(out)
    return out


def load_fingerprint(path: Path) -> np.ndarray[Any, np.dtype[np.uint32]]:
    """Read a fingerprint written by :func:`fingerprint`."""
    return np.frombuffer(path.read_bytes(), dtype="<u4").astype(np.uint32)


@dataclass(frozen=True, slots=True)
class Match:
    """How well two fingerprints match at their best alignment.

    Attributes
    ----------
    strength : float
        How far the best offset stands out from all others (robust z-score of the
        near-match fraction). Unrelated clips stay below about 13.
    fraction : float
        Fraction of items that nearly match at the best offset.
    offset_s : float
        Best offset in seconds; positive when the moment is later in the first clip.
    """

    strength: float
    fraction: float
    offset_s: float


def similarity(
    a: np.ndarray[Any, np.dtype[np.uint32]],
    b: np.ndarray[Any, np.dtype[np.uint32]],
    *,
    max_offset_s: float = 12.0,
) -> Match:
    """Compare two fingerprints at every alignment.

    Parameters
    ----------
    a, b : numpy.ndarray of uint32
        Fingerprints from :func:`load_fingerprint`.
    max_offset_s : float, default 12.0
        Largest shift tried in either direction.

    Returns
    -------
    Match
        All zero when the fingerprints overlap by less than 10 s at every offset.

    Examples
    --------
    >>> import numpy as np
    >>> rng = np.random.default_rng(0)
    >>> fp = rng.integers(0, 2**32, 300, dtype=np.uint32)
    >>> match = similarity(fp[20:], fp)
    >>> match.fraction, round(match.offset_s, 2), match.strength > 16
    (1.0, -2.48, True)
    >>> similarity(fp, rng.integers(0, 2**32, 300, dtype=np.uint32)).strength < 16
    True
    """
    max_offset = round(max_offset_s / FINGERPRINT_ITEM_S)
    min_overlap = round(_MIN_OVERLAP_S / FINGERPRINT_ITEM_S)
    offsets: list[int] = []
    fractions: list[float] = []
    for offset in range(-max_offset, max_offset + 1):
        x = a[max(0, offset) :]
        y = b[max(0, -offset) :]
        n = min(len(x), len(y))
        if n < min_overlap:
            continue
        differing = _POPCOUNT[(x[:n] ^ y[:n]).view(np.uint8)].reshape(n, 4).sum(axis=1)
        offsets.append(offset)
        fractions.append(float((differing <= _NEAR_BITS).mean()))
    if not fractions:
        return Match(0.0, 0.0, 0.0)
    values = np.array(fractions)
    best = int(values.argmax())
    median = float(np.median(values))
    spread = float(np.median(np.abs(values - median))) + 1e-3
    return Match(
        strength=round(float(values[best] - median) / spread, 2),
        fraction=round(float(values[best]), 4),
        offset_s=round(offsets[best] * FINGERPRINT_ITEM_S, 2),
    )


@dataclass(frozen=True, slots=True)
class DedupeDecision:
    """Outcome of comparing a new clip with clips of the same moment.

    Attributes
    ----------
    duplicate_of : int or None
        Set when the new clip is the duplicate: the clip it duplicates.
    replaced : tuple of int
        Clips that the new clip replaced as the kept one.
    strength : float
        Match strength of the best match (0.0 when nothing matched).
    """

    duplicate_of: int | None = None
    replaced: tuple[int, ...] = ()
    strength: float = 0.0


_CANDIDATES = """
SELECT c.id, c.path, c.state, c.start_ts, coalesce(s.score, 0) AS score
FROM clips c LEFT JOIN spikes s ON s.id = c.spike_id
WHERE c.id != ? AND c.duplicate_of IS NULL AND c.state != 'blocked'
  AND abs(c.start_ts - ?) <= ?
"""


def _rank(row: sqlite3.Row) -> tuple[bool, float, float]:
    # Approved beats everything, then score, then the earlier clip.
    return (row["state"] == "approved", float(row["score"]), -float(row["start_ts"]))


def resolve_duplicates(
    conn: sqlite3.Connection, clip_id: int, settings: DedupeConfig
) -> DedupeDecision:
    """Compare clip ``clip_id`` with kept clips of the same moment and record the result.

    The caller must serialise calls (the live pipeline holds a lock), so that two
    clips of one moment never both stay kept.

    Parameters
    ----------
    conn : sqlite3.Connection
        The change is committed.
    clip_id : int
        A clip whose fingerprint (``<clip>.fp``) exists.
    settings : DedupeConfig

    Returns
    -------
    DedupeDecision
    """
    new = conn.execute(
        "SELECT c.id, c.path, c.state, c.start_ts, coalesce(s.score, 0) AS score "
        "FROM clips c LEFT JOIN spikes s ON s.id = c.spike_id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    if new is None:
        raise ValueError(f"no clip {clip_id}")
    mine = load_fingerprint(fingerprint_path(Path(new["path"])))
    matches: list[tuple[sqlite3.Row, float]] = []
    for row in conn.execute(_CANDIDATES, (clip_id, new["start_ts"], settings.window_s)):
        path = fingerprint_path(Path(row["path"]))
        if not path.exists():
            continue
        match = similarity(mine, load_fingerprint(path), max_offset_s=settings.max_offset_s)
        log.debug("clips compared", clip_id=clip_id, other=row["id"], strength=match.strength)
        if match.strength >= settings.min_strength:
            matches.append((row, match.strength))
            log.info(
                "same moment found",
                clip_id=clip_id,
                other=row["id"],
                strength=match.strength,
                offset_s=match.offset_s,
            )
    if not matches:
        return DedupeDecision()
    best_other, best_score = max(matches, key=lambda m: _rank(m[0]))
    if _rank(best_other) >= _rank(new):
        conn.execute(
            "UPDATE clips SET duplicate_of = ?, similarity = ? WHERE id = ?",
            (best_other["id"], best_score, clip_id),
        )
        conn.commit()
        return DedupeDecision(duplicate_of=int(best_other["id"]), strength=best_score)
    replaced = tuple(int(row["id"]) for row, _ in matches)
    for row, score in matches:
        conn.execute(
            "UPDATE clips SET duplicate_of = ?, similarity = ? WHERE id = ? OR duplicate_of = ?",
            (clip_id, score, row["id"], row["id"]),
        )
    conn.commit()
    return DedupeDecision(replaced=replaced, strength=max(score for _, score in matches))
