from __future__ import annotations

import sqlite3
from pathlib import Path

from peakreel.backup import backup_db, backup_dir
from peakreel.config import load_config
from peakreel.db import init_db
from tests.conftest import WriteConfig

DAY = 86400.0


def test_one_copy_a_day_and_only_the_newest_kept(write_config: WriteConfig) -> None:
    config = load_config(write_config({"retention": {"backup_days": 2}}))
    init_db(config.db_path)
    conn = sqlite3.connect(config.db_path)
    conn.execute("INSERT INTO channels (platform, name, created_at) VALUES ('twitch', 'a', 0)")
    conn.commit()
    conn.close()
    t0 = 1_795_000_000.0
    first = backup_db(config, now=t0)
    assert first is not None
    assert backup_db(config, now=t0 + 60) is None  # today's copy exists
    copy = sqlite3.connect(first)  # a full, readable copy
    assert copy.execute("SELECT name FROM channels").fetchall() == [("a",)]
    copy.close()  # Windows cannot prune an open file
    backup_db(config, now=t0 + DAY)
    backup_db(config, now=t0 + 2 * DAY)
    kept = sorted(p.name for p in backup_dir(config).glob("*.db"))
    assert len(kept) == 2 and first.name not in kept  # the oldest is pruned
    assert not list(backup_dir(config).glob("*.part"))


def test_backups_off(write_config: WriteConfig, tmp_path: Path) -> None:
    config = load_config(write_config({"retention": {"backup_days": 0}}))
    init_db(config.db_path)
    assert backup_db(config) is None
