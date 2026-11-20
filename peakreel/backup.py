"""Daily copies of the database, the one place the whole history lives.

``data/peakreel.db`` holds every clip, post and statistic, and the per-day upload
history that is never deleted. A copy is made once a day with SQLite's online backup
(consistent while the pipeline and the review page write) into ``data/backups/``, and
the newest ``retention.backup_days`` copies are kept. They sit on the same disk: they
guard against a damaged file or a bad write, not a lost disk (copy them elsewhere for
that).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from peakreel.config import Config
from peakreel.log import get_logger

__all__ = ["backup_db", "backup_dir"]

log = get_logger(__name__)


def backup_dir(config: Config) -> Path:
    """Return the folder that holds the daily copies (``data/backups``)."""
    return config.data_dir / "backups"


def backup_db(config: Config, *, force: bool = False, now: float | None = None) -> Path | None:
    """Copy the database once a day and prune old copies.

    Parameters
    ----------
    config : Config
        ``db_path``, ``data_dir`` and ``retention.backup_days`` (0 turns backups off).
    force : bool, default False
        Copy even when today's copy exists (it is replaced).
    now : float, optional
        Current Unix time; defaults to the clock. The day is UTC.

    Returns
    -------
    pathlib.Path or None
        The copy written, or ``None`` when today's exists, backups are off, or there
        is no database yet.

    Notes
    -----
    The copy is written to a ``.part`` file and renamed, so a crash never leaves a
    half copy under a backup's name.
    """
    keep = config.retention.backup_days
    if keep <= 0 or not config.db_path.exists():
        return None
    now = time.time() if now is None else now
    folder = backup_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    target = folder / f"peakreel-{day}.db"
    if target.exists() and not force:
        return None
    part = target.with_suffix(".db.part")
    part.unlink(missing_ok=True)
    source = sqlite3.connect(config.db_path, timeout=30.0)
    try:
        copy = sqlite3.connect(part)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    part.replace(target)
    copies = sorted(folder.glob("peakreel-*.db"))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    log.info("database backed up", path=str(target), kept=min(len(copies), keep))
    return target
