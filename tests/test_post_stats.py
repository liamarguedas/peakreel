from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from peakreel.config import Config
from peakreel.db import connect
from peakreel.publish.stats import Stats, read_stats_once, report
from tests.test_supervisor_review import review  # noqa: F401  # pyright: ignore[reportUnusedImport]  - fixture


def test_numbers_are_read_at_each_checkpoint_and_reported(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, client, clip_id = review
    conn = connect(config.db_path)
    now = time.time()
    conn.execute(
        "UPDATE clips SET auto = ? WHERE id = ?", (json.dumps({"gemini": {"score": 8}}), clip_id)
    )
    conn.execute(
        "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, remote_id, "
        "url, attempts, created_at, updated_at, published_at) VALUES "
        "(?, 'en', 'youtube', 'public', 'published', '{}', 'vid1', 'https://youtu.be/vid1', "
        "0, ?, ?, ?)",
        (clip_id, now, now, now - 30 * 3600),  # posted 30 h ago: 12 h and 24 h are due
    )
    conn.commit()
    reads: list[tuple[str, str]] = []

    def youtube(account: str, remote_id: str) -> Stats:
        reads.append((account, remote_id))
        return Stats(views=1500, likes=90, comments=4)

    assert read_stats_once(conn, {"youtube": youtube}, now=now) == 2  # one read, two checkpoints
    assert read_stats_once(conn, {"youtube": youtube}, now=now) == 0  # nothing new due
    assert reads == [("en", "vid1")]
    summary = report(conn, 24)
    assert summary["groups"] == {("en", "youtube"): (1, 1500)}
    assert summary["top"][0][:3] == (1500, "en", "youtube")
    assert summary["gemini"] == (None, 1)  # too few posts to tell
    # The card's platform link shows the latest views.
    (pub,) = client.get("/api/clips?state=published").json()[0]["publications"]
    assert pub["views"] == 1500
    conn.close()
