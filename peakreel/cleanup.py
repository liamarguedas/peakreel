"""Retention: delete what is no longer needed so an unattended run never fills the disk.

Rules (``retention:`` in the config):

- Approved clips are purged ``posted_days`` after their last post finished, once
  at least one is on a platform and none is still waiting, uploading or being
  retried: the platforms hold the video, and posting needs the file no more.
- Rejected and taken-down clips are purged after ``rejected_days``; clips never
  reviewed (including duplicates) after ``new_days``. Purging deletes the clip's
  media (horizontal and vertical video, thumbnail, subtitles, fingerprint) and
  sets ``clips.purged_at``; the row and the JSON sidecar stay, so every decision
  remains traceable.
- Export packages are deleted ``publish_days`` after they were written.
- Chat logs older than ``chat_days`` are deleted.
- If free disk is still below ``min_free_gb``, the oldest clips that are not
  approved are purged early, oldest first, never one younger than an hour (it
  may still be in review or in flight). Approved clips wait for their posts.

The rolling video buffer has its own limit (``record.buffer_minutes``).
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from peakreel.config import Config
from peakreel.log import get_logger

__all__ = ["CleanupReport", "clip_media", "free_gb", "run_cleanup"]

log = get_logger(__name__)

_MEDIA_SUFFIXES = (".mp4", ".vertical.mp4", ".jpg", ".ass", ".fp")
_EMERGENCY_MIN_AGE_S = 3600.0


@dataclass
class CleanupReport:
    """What one cleanup pass did (or, in a dry run, would do).

    Attributes
    ----------
    purged : list of int
        Clip ids whose media was deleted.
    emergency : list of int
        Of those, clips purged early because disk was low.
    packages : int
        Export packages deleted.
    chat_files : int
        Chat log files deleted.
    freed_mb : float
    free_gb : float
        Free disk after the pass.
    low_disk : bool
        Free disk is still below ``retention.min_free_gb``.
    """

    purged: list[int] = field(default_factory=list[int])
    emergency: list[int] = field(default_factory=list[int])
    packages: int = 0
    chat_files: int = 0
    freed_mb: float = 0.0
    free_gb: float = 0.0
    low_disk: bool = False


def free_gb(path: Path) -> float:
    """Free disk space, in GB, of the volume holding ``path`` (or its nearest parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1e9


def clip_media(row: sqlite3.Row) -> list[Path]:
    """Every media file of a clip that exists (the sidecar is not media: it stays)."""
    base = Path(row["path"])
    stem = base.with_suffix("")
    candidates = {base, Path(f"{stem}.vertical.mp4")}
    candidates |= {Path(f"{stem}{suffix}") for suffix in _MEDIA_SUFFIXES}
    for column in ("vertical_path", "thumbnail_path"):
        if row[column]:
            candidates.add(Path(row[column]))
    return sorted(p for p in candidates if p.is_file())


def _purge(conn: sqlite3.Connection, row: sqlite3.Row, now: float, dry_run: bool) -> int:
    size = 0
    for path in clip_media(row):
        size += path.stat().st_size
        if not dry_run:
            path.unlink(missing_ok=True)
    if not dry_run:
        conn.execute("UPDATE clips SET purged_at = ? WHERE id = ?", (now, row["id"]))
        conn.commit()
    return size


def _expired_packages(root: Path, cutoff: float) -> list[Path]:
    # data/publish/<account>/<platform>/<day>/<clip>/
    if not root.is_dir():
        return []
    return [p for p in root.glob("*/*/*/*") if p.is_dir() and p.stat().st_mtime < cutoff]


def _expired_chat(root: Path, keep_from: str) -> list[Path]:
    # data/chat/<channel>/<YYYY-MM-DD>.jsonl; the name is the UTC day.
    if not root.is_dir():
        return []
    return [p for p in root.glob("*/*.jsonl") if p.stem < keep_from]


def run_cleanup(
    config: Config,
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    dry_run: bool = False,
    disk_free: object = None,
) -> CleanupReport:
    """Apply the retention rules once.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    now : float, optional
        Epoch seconds; defaults to the current time.
    dry_run : bool, default False
        Report what would be deleted; delete nothing.
    disk_free : callable, optional
        ``disk_free() -> float`` GB; injectable for tests (defaults to the data
        directory's volume).

    Returns
    -------
    CleanupReport
    """
    settings = config.retention
    now = time.time() if now is None else now
    measure = disk_free if callable(disk_free) else (lambda: free_gb(config.data_dir))
    report = CleanupReport()
    freed = 0

    rows = conn.execute(
        """
        SELECT * FROM clips WHERE purged_at IS NULL AND (
            (state IN ('rejected', 'blocked') AND created_at < ?)
            OR (state = 'new' AND created_at < ?)
            OR (state = 'approved'
                AND EXISTS (SELECT 1 FROM publications p WHERE p.clip_id = clips.id
                            AND p.state IN ('published', 'processing', 'private_only'))
                AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.clip_id = clips.id
                                AND p.state IN ('pending', 'approved', 'uploading'))
                AND (SELECT max(p.updated_at) FROM publications p
                     WHERE p.clip_id = clips.id AND p.state != 'cancelled') < ?)
        ) ORDER BY created_at
        """,
        (
            now - settings.rejected_days * 86400,
            now - settings.new_days * 86400,
            now - settings.posted_days * 86400,
        ),
    ).fetchall()
    for row in rows:
        freed += _purge(conn, row, now, dry_run)
        report.purged.append(int(row["id"]))

    for package in _expired_packages(config.publish_dir, now - settings.publish_days * 86400):
        freed += sum(f.stat().st_size for f in package.rglob("*") if f.is_file())
        if not dry_run:
            shutil.rmtree(package, ignore_errors=True)
        report.packages += 1

    keep_from = (datetime.fromtimestamp(now, UTC) - timedelta(days=settings.chat_days)).strftime(
        "%Y-%m-%d"
    )
    for path in _expired_chat(config.chat_log_dir, keep_from):
        freed += path.stat().st_size
        if not dry_run:
            path.unlink(missing_ok=True)
        report.chat_files += 1

    free = float(measure())  # type: ignore[operator]
    if free < settings.min_free_gb:
        candidates = conn.execute(
            "SELECT * FROM clips WHERE purged_at IS NULL AND state != 'approved' "
            "AND created_at < ? ORDER BY created_at",
            (now - _EMERGENCY_MIN_AGE_S,),
        ).fetchall()
        needed = (settings.min_free_gb - free) * 1e9
        reclaimed = 0
        for row in candidates:
            if int(row["id"]) in report.purged:
                continue
            if reclaimed >= needed:
                break
            size = _purge(conn, row, now, dry_run)
            reclaimed += size
            freed += size
            report.purged.append(int(row["id"]))
            report.emergency.append(int(row["id"]))
        free = float(measure()) if not dry_run else free + reclaimed / 1e9  # type: ignore[operator]

    report.freed_mb = round(freed / 1e6, 3)
    report.free_gb = round(free, 1)
    report.low_disk = free < settings.min_free_gb
    if report.emergency:
        log.error(
            "disk low: clips purged early",
            clips=len(report.emergency),
            free_gb=report.free_gb,
            min_free_gb=settings.min_free_gb,
        )
    if report.low_disk:
        log.error("disk still low after cleanup", free_gb=report.free_gb)
    if report.purged or report.packages or report.chat_files:
        log.info(
            "cleanup",
            clips=len(report.purged),
            packages=report.packages,
            chat_files=report.chat_files,
            freed_mb=report.freed_mb,
            dry_run=dry_run,
        )
    return report
