"""SQLite schema and connection helpers.

The schema is versioned with ``PRAGMA user_version``. Each entry in ``MIGRATIONS``
moves the database forward by one version, and :func:`init_db` applies whatever is
missing, so it is idempotent and safe to call at every start.

Tables
------
channels
    One row per watched channel.
chat_events
    Chat rows keyed by channel and time. Raw chat lives in JSONL logs (the source of
    truth, see :mod:`peakreel.chat.writer`); what is materialised here is decided by
    the spike detector in M2.
spikes
    Detected chat spikes with the statistics that triggered them.
clips
    Clips cut from spikes, with their review state.

All timestamps are UTC wall-clock epoch seconds stored as ``REAL``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["MIGRATIONS", "connect", "init_db", "schema_version"]

MIGRATIONS: tuple[str, ...] = (
    # v1: initial schema.
    """
    CREATE TABLE channels (
        id           INTEGER PRIMARY KEY,
        platform     TEXT NOT NULL DEFAULT 'twitch',
        name         TEXT NOT NULL,
        display_name TEXT,
        credit       TEXT,
        created_at   REAL NOT NULL DEFAULT (unixepoch('subsec')),
        UNIQUE (platform, name)
    );

    CREATE TABLE chat_events (
        id         INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
        ts         REAL NOT NULL,
        server_ts  REAL,
        user       TEXT NOT NULL,
        kind       TEXT NOT NULL,
        text       TEXT NOT NULL,
        filtered   TEXT
    );
    CREATE INDEX chat_events_channel_ts ON chat_events (channel_id, ts);

    CREATE TABLE spikes (
        id              INTEGER PRIMARY KEY,
        channel_id      INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
        start_ts        REAL NOT NULL,
        end_ts          REAL NOT NULL,
        peak_count      INTEGER NOT NULL,
        unique_chatters INTEGER NOT NULL,
        baseline_median REAL NOT NULL,
        baseline_mad    REAL NOT NULL,
        score           REAL NOT NULL,
        top_messages    TEXT NOT NULL DEFAULT '[]',
        created_at      REAL NOT NULL DEFAULT (unixepoch('subsec')),
        CHECK (end_ts >= start_ts)
    );
    CREATE INDEX spikes_channel_start ON spikes (channel_id, start_ts);

    CREATE TABLE clips (
        id           INTEGER PRIMARY KEY,
        channel_id   INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
        spike_id     INTEGER REFERENCES spikes (id) ON DELETE SET NULL,
        start_ts     REAL NOT NULL,
        end_ts       REAL NOT NULL,
        path         TEXT NOT NULL UNIQUE,
        sidecar_path TEXT,
        state        TEXT NOT NULL DEFAULT 'new'
                     CHECK (state IN ('new', 'approved', 'rejected', 'blocked')),
        created_at   REAL NOT NULL DEFAULT (unixepoch('subsec')),
        CHECK (end_ts > start_ts)
    );
    CREATE INDEX clips_channel_start ON clips (channel_id, start_ts);
    CREATE INDEX clips_state ON clips (state);
    """,
)


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with Peakreel's pragmas applied.

    Parameters
    ----------
    path : pathlib.Path
        Database file. Parent directories are created if missing.

    Returns
    -------
    sqlite3.Connection
        Connection with foreign keys enforced, WAL journaling (so the review tool
        can read while the pipeline writes) and ``sqlite3.Row`` rows.

    Notes
    -----
    This does not create the schema; use :func:`init_db` for that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stored in the database.

    Parameters
    ----------
    conn : sqlite3.Connection

    Returns
    -------
    int
        ``0`` for a fresh database, otherwise the number of applied migrations.
    """
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_db(path: Path) -> int:
    """Create or upgrade the database to the latest schema.

    Parameters
    ----------
    path : pathlib.Path
        Database file, created if missing.

    Returns
    -------
    int
        The schema version after the call.

    Raises
    ------
    RuntimeError
        If the database was written by a newer Peakreel (its version is higher than
        the migrations this code knows). Nothing is changed in that case.
    sqlite3.Error
        If a migration fails. The failing migration is rolled back and the version
        is left at the last one that succeeded.

    Examples
    --------
    >>> init_db(Path("data/peakreel.db"))  # doctest: +SKIP
    1
    """
    conn = connect(path)
    try:
        current = schema_version(conn)
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"{path} has schema version {current}, but this Peakreel only knows "
                f"up to {len(MIGRATIONS)}. Upgrade Peakreel."
            )
        for version in range(current + 1, len(MIGRATIONS) + 1):
            script = MIGRATIONS[version - 1]
            # executescript() commits implicitly, so wrap each migration and its
            # version bump in one explicit transaction to keep them atomic.
            try:
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return schema_version(conn)
    finally:
        conn.close()
