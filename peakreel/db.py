"""SQLite schema and connection helpers.

The schema is versioned with ``PRAGMA user_version``. Each entry in ``MIGRATIONS``
moves the database forward by one version, and :func:`init_db` applies whatever is
missing, so it is idempotent and safe to call at every start.

Raw chat is not stored here: the JSONL logs written by :mod:`peakreel.chat.writer`
are the single source of truth.

Tables
------
channels
    One row per watched channel.
spikes
    Detected chat spikes with the statistics that triggered them.
clips
    Clips cut from spikes, with their review state.

All timestamps are UTC wall-clock epoch seconds stored as ``REAL``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from peakreel.detect.spikes import Spike

__all__ = [
    "MIGRATIONS",
    "connect",
    "ensure_channel",
    "init_db",
    "insert_clip",
    "insert_spike",
    "schema_version",
]

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
    # v2: raw chat lives only in the JSONL logs (single source of truth); nothing
    # reads chat_events, so it goes.
    """
    DROP INDEX chat_events_channel_ts;
    DROP TABLE chat_events;
    """,
    # v3: the live clipper records suppressed spikes too (they explain why no clip
    # was made), and the receive delay that converts chat time to broadcast time.
    """
    ALTER TABLE spikes ADD COLUMN suppressed_by TEXT;
    ALTER TABLE spikes ADD COLUMN receive_delay_s REAL;
    """,
    # v4: the vertical render and thumbnail of each clip (M5).
    """
    ALTER TABLE clips ADD COLUMN vertical_path TEXT;
    ALTER TABLE clips ADD COLUMN thumbnail_path TEXT;
    """,
    # v5: review tool (M7): editable metadata, review time, export location.
    """
    ALTER TABLE clips ADD COLUMN title TEXT;
    ALTER TABLE clips ADD COLUMN description TEXT;
    ALTER TABLE clips ADD COLUMN reviewed_at REAL;
    ALTER TABLE clips ADD COLUMN export_path TEXT;
    """,
    # v6: safety flags for the reviewer, and cross-channel duplicates (M7a).
    # A clip with duplicate_of set is a duplicate of that clip; its state is unchanged.
    """
    ALTER TABLE clips ADD COLUMN flags TEXT;
    ALTER TABLE clips ADD COLUMN duplicate_of INTEGER REFERENCES clips(id);
    ALTER TABLE clips ADD COLUMN match_strength REAL;
    """,
    # v7: one row per clip and platform (M9). UNIQUE(clip_id, platform) is what makes
    # a clip impossible to post twice to the same platform, across restarts.
    """
    CREATE TABLE publications (
        id INTEGER PRIMARY KEY,
        clip_id INTEGER NOT NULL REFERENCES clips(id),
        platform TEXT NOT NULL CHECK (platform IN ('youtube', 'tiktok', 'instagram')),
        mode TEXT NOT NULL CHECK (mode IN ('export', 'private', 'public')),
        state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'uploading', 'processing',
            'published', 'exported', 'failed', 'private_only', 'cancelled')),
        metadata TEXT NOT NULL,
        remote_id TEXT,
        url TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        scheduled_at REAL,
        next_attempt_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (clip_id, platform)
    );
    CREATE INDEX publications_due ON publications (state, next_attempt_at);
    """,
    # v8: posting accounts per language. A clip is posted at most once per account and
    # platform; clips.language is the reviewer's choice of language (routing).
    """
    CREATE TABLE publications_v8 (
        id INTEGER PRIMARY KEY,
        clip_id INTEGER NOT NULL REFERENCES clips(id),
        account TEXT NOT NULL,
        platform TEXT NOT NULL CHECK (platform IN ('youtube', 'tiktok', 'instagram')),
        mode TEXT NOT NULL CHECK (mode IN ('export', 'private', 'public')),
        state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'uploading', 'processing',
            'published', 'exported', 'failed', 'private_only', 'cancelled')),
        metadata TEXT NOT NULL,
        remote_id TEXT,
        url TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        scheduled_at REAL,
        next_attempt_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (clip_id, account, platform)
    );
    INSERT INTO publications_v8 (id, clip_id, account, platform, mode, state, metadata,
        remote_id, url, error, attempts, scheduled_at, next_attempt_at, created_at, updated_at)
    SELECT id, clip_id, 'main', platform, mode, state, metadata, remote_id, url, error,
        attempts, scheduled_at, next_attempt_at, created_at, updated_at FROM publications;
    DROP INDEX publications_due;
    DROP TABLE publications;
    ALTER TABLE publications_v8 RENAME TO publications;
    CREATE INDEX publications_due ON publications (state, next_attempt_at);
    ALTER TABLE clips ADD COLUMN language TEXT;
    """,
    # v9: retention (M8). A purged clip keeps its row and sidecar; its media is deleted.
    """
    ALTER TABLE clips ADD COLUMN purged_at REAL;
    """,
    # v10: Facebook Page Reels as a fourth platform (M12).
    """
    CREATE TABLE publications_v10 (
        id INTEGER PRIMARY KEY,
        clip_id INTEGER NOT NULL REFERENCES clips(id),
        account TEXT NOT NULL,
        platform TEXT NOT NULL
            CHECK (platform IN ('youtube', 'tiktok', 'instagram', 'facebook')),
        mode TEXT NOT NULL CHECK (mode IN ('export', 'private', 'public')),
        state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'uploading', 'processing',
            'published', 'exported', 'failed', 'private_only', 'cancelled')),
        metadata TEXT NOT NULL,
        remote_id TEXT,
        url TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        scheduled_at REAL,
        next_attempt_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (clip_id, account, platform)
    );
    INSERT INTO publications_v10 SELECT * FROM publications;
    DROP INDEX publications_due;
    DROP TABLE publications;
    ALTER TABLE publications_v10 RENAME TO publications;
    CREATE INDEX publications_due ON publications (state, next_attempt_at);
    """,
    # v11: automatic review decision (JSON: mode, decision, reason, excitement, account).
    """
    ALTER TABLE clips ADD COLUMN auto TEXT;
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


def ensure_channel(conn: sqlite3.Connection, name: str, credit: str | None = None) -> int:
    """Return the id of channel ``name``, creating the row if needed.

    Parameters
    ----------
    conn : sqlite3.Connection
    name : str
        Twitch login.
    credit : str or None
        Credit text; updates the stored value when given.

    Returns
    -------
    int
    """
    conn.execute(
        "INSERT INTO channels (platform, name, credit) VALUES ('twitch', ?, ?) "
        "ON CONFLICT (platform, name) DO UPDATE SET credit = coalesce(excluded.credit, credit)",
        (name, credit),
    )
    row = conn.execute(
        "SELECT id FROM channels WHERE platform = 'twitch' AND name = ?", (name,)
    ).fetchone()
    return int(row["id"])


def insert_spike(conn: sqlite3.Connection, channel_id: int, spike: Spike) -> int:
    """Store a detected spike and return its id.

    Parameters
    ----------
    conn : sqlite3.Connection
    channel_id : int
    spike : Spike

    Returns
    -------
    int
    """
    cursor = conn.execute(
        "INSERT INTO spikes (channel_id, start_ts, end_ts, peak_count, unique_chatters, "
        "baseline_median, baseline_mad, score, top_messages, suppressed_by, receive_delay_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            spike.start_ts,
            spike.end_ts,
            spike.peak_count,
            spike.unique_chatters,
            spike.baseline_median,
            spike.baseline_mad,
            spike.score,
            json.dumps(spike.top_messages, ensure_ascii=False),
            spike.suppressed_by,
            spike.receive_delay_s,
        ),
    )
    return int(cursor.lastrowid or 0)


def insert_clip(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    spike_id: int | None,
    start_ts: float,
    end_ts: float,
    path: Path,
    sidecar_path: Path,
    state: str = "new",
) -> int:
    """Store a clip and return its id.

    Parameters
    ----------
    conn : sqlite3.Connection
    channel_id : int
    spike_id : int or None
    start_ts, end_ts : float
        Broadcast-clock range of the clip.
    path, sidecar_path : pathlib.Path
    state : {"new", "approved", "rejected", "blocked"}, default "new"

    Returns
    -------
    int
    """
    cursor = conn.execute(
        "INSERT INTO clips (channel_id, spike_id, start_ts, end_ts, path, sidecar_path, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (channel_id, spike_id, start_ts, end_ts, str(path), str(sidecar_path), state),
    )
    return int(cursor.lastrowid or 0)
