from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from peakreel import db


def _tables(path: Path) -> set[str]:
    conn = db.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row["name"] for row in rows}
    finally:
        conn.close()


def test_init_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "peakreel.db"
    assert db.init_db(path) == len(db.MIGRATIONS)
    tables = _tables(path)
    assert {"channels", "spikes", "clips"} <= tables
    assert "chat_events" not in tables  # dropped in v2

    conn = db.connect(path)
    conn.execute("INSERT INTO channels (name) VALUES ('a')")
    conn.commit()
    conn.close()

    assert db.init_db(path) == len(db.MIGRATIONS)
    conn = db.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM channels").fetchone()[0] == 1
    finally:
        conn.close()


def test_constraints_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "peakreel.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        conn.execute("INSERT INTO channels (name) VALUES ('a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO channels (name) VALUES ('a')")
        with pytest.raises(sqlite3.IntegrityError):  # foreign key
            conn.execute(
                "INSERT INTO clips (channel_id, start_ts, end_ts, path) VALUES (999, 1, 2, 'p')"
            )
        with pytest.raises(sqlite3.IntegrityError):  # state check
            conn.execute(
                "INSERT INTO clips (channel_id, start_ts, end_ts, path, state) "
                "VALUES (1, 1, 2, 'p', 'bogus')"
            )
    finally:
        conn.close()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "peakreel.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="Upgrade Peakreel"):
        db.init_db(path)


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "peakreel.db"
    db.init_db(path)
    broken = (
        *db.MIGRATIONS,
        "CREATE TABLE extra (id INTEGER); CREATE TABLE channels (id INTEGER);",
    )
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.Error):
        db.init_db(path)
    assert "extra" not in _tables(path)
    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == len(broken) - 1
    finally:
        conn.close()
