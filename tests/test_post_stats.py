from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from peakreel.config import Config
from peakreel.db import connect, ensure_channel, insert_clip
from peakreel.publish.stats import Stats, read_stats_once, report
from tests.conftest import MemoryKeyring
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

    assert read_stats_once(conn, {"youtube": youtube}, now=now) == 3  # one read, three checkpoints
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


def test_a_dead_post_never_blocks_the_others_and_late_reads_are_left_out(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, _client, clip_id = review
    conn = connect(config.db_path)
    now = time.time()
    for platform, remote, posted_h in (("instagram", "gone", 20.0), ("facebook", "live", 13.0)):
        conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, "
            "remote_id, attempts, created_at, updated_at, published_at) "
            "VALUES (?, 'en', ?, 'public', 'published', '{}', ?, 0, ?, ?, ?)",
            (clip_id, platform, remote, now, now, now - posted_h * 3600),
        )
    conn.commit()

    def fetch(account: str, remote_id: str) -> Stats:
        if remote_id == "gone":
            raise KeyError("an odd answer for a deleted post")  # not a PublishError
        return Stats(views=300)

    # The dead post fails but the live one is still read; 20 h is past 12 + 6 h, so
    # the dead post's checkpoint is stored empty and it is not tried again.
    fetchers = {"instagram": fetch, "facebook": fetch}
    assert read_stats_once(conn, fetchers, now=now) == 4  # each: 3 h and 12 h
    assert read_stats_once(conn, fetchers, now=now) == 0
    gone = conn.execute(
        "SELECT views FROM post_stats s JOIN publications p ON p.id = s.publication_id "
        "WHERE p.remote_id = 'gone'"
    ).fetchone()
    assert gone["views"] is None
    # The live post was read at 13 h: within 25% of its 12 h checkpoint, so it counts.
    summary = report(conn, 12)
    assert summary["groups"] == {("en", "facebook"): (1, 300)}
    conn.execute("UPDATE post_stats SET age_h = 16")  # read 4 h late: left out
    conn.commit()
    assert report(conn, 12)["groups"] == {} and report(conn, 12)["late"] == 2
    conn.close()


def test_dashboard_counts_posts_views_and_the_hourly_chart(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, client, clip_id = review
    conn = connect(config.db_path)
    now = time.time()
    for platform, state, views in (
        ("youtube", "published", 1500),
        ("instagram", "published", None),  # not read yet: unknown, not zero
        ("facebook", "failed", None),
    ):
        cur = conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, "
            "url, attempts, created_at, updated_at, published_at) VALUES "
            "(?, 'en', ?, 'public', ?, '{\"title\": \"Post title\"}', 'https://x/1', 0, ?, ?, ?)",
            (clip_id, platform, state, now, now, now - 3600),
        )
        for checkpoint, n in ((12, 100), (24, views)) if views else ():
            conn.execute(
                "INSERT INTO post_stats (publication_id, checkpoint_h, age_h, read_at, views, "
                "likes) VALUES (?, ?, ?, ?, ?, 5)",
                (cur.lastrowid, checkpoint, checkpoint, now, n),
            )
    conn.commit()
    d = client.get("/api/dashboard").json()
    groups = {g["platform"]: g for g in d["posts"]}
    assert groups["youtube"]["views"] == 1500  # the latest reading, not the sum of both
    assert groups["instagram"]["views"] is None
    assert (groups["facebook"]["failed"], groups["facebook"]["live"]) == (1, 0)
    assert len(d["hourly"]) == 48
    assert sum(n for _, n in d["hourly"]) == 2  # the two live posts, an hour ago
    assert d["hourly"][-2][1] == 2  # the hour before the current one
    assert d["top"][0]["title"] == "Post title"
    conn.close()


def test_schedule_lists_the_queue_soonest_first_and_todays_posts(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    config, client, clip_id = review
    conn = connect(config.db_path)
    now = time.time()
    for platform, state, due, error in (
        ("facebook", "approved", now + 7200, "Meta blocked posting for a while (spam)"),
        ("youtube", "approved", now + 600, None),
        ("instagram", "published", None, None),
    ):
        conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, url, "
            "error, attempts, next_attempt_at, created_at, updated_at, published_at) VALUES "
            "(?, 'en', ?, 'public', ?, '{\"title\": \"T\"}', 'https://x/1', ?, 0, ?, ?, ?, ?)",
            (clip_id, platform, state, error, due, now, now, now - 3600),
        )
    conn.commit()
    d = client.get("/api/schedule").json()
    assert [p["platform"] for p in d["queued"]] == ["youtube", "facebook"]  # soonest first
    assert d["queued"][1]["note"].startswith("Meta blocked")  # why it waits
    assert [(p["platform"], p["title"]) for p in d["posted"]] == [("instagram", "T")]
    assert (d["stages"]["queued"], d["stages"]["posted"], d["stages"]["made"]) == (2, 1, 1)
    conn.close()


def test_uploads_count_the_last_24_hours_and_keep_every_day(
    review: tuple[Config, TestClient, int],  # noqa: F811
    memory_keyring: MemoryKeyring,
) -> None:
    from peakreel.publish import meta

    config, client, _clip_id = review
    conn = connect(config.db_path)
    now = time.time()
    cap = config.publish.instagram.daily_cap
    for i, hours_ago in enumerate([30.0] + [1.0 + j / 10 for j in range(cap)]):
        one = insert_clip(
            conn, channel_id=ensure_channel(conn, "alpha"), spike_id=None, start_ts=now - i,
            end_ts=now - i + 30, path=config.outbox_dir / f"u{i}.mp4",
            sidecar_path=config.outbox_dir / f"u{i}.json",
        )  # fmt: skip
        conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, "
            "attempts, created_at, updated_at, published_at) VALUES "
            "(?, ?, 'instagram', 'public', 'published', '{}', 0, ?, ?, ?)",
            (one, "main" if i else "gone", now, now, now - hours_ago * 3600),
        )
    conn.commit()
    d = client.get("/api/uploads").json()
    (ig,) = [a for a in d["accounts"] if a["platform"] == "instagram"]
    assert (ig["account"], ig["last_24h"], ig["connected"]) == ("main", cap, False)
    assert len(ig["posts"]) == cap and ig["posts"][0]["channel"] == "alpha"  # newest first
    assert ig["posts"][0]["at"] > ig["posts"][-1]["at"] and ig["posts"][0]["thumbnail"] is False
    assert ig["next_at"] is not None  # at the cap: the oldest post in the window must age out
    assert d["labels"]["gone/instagram"] == "gone (removed)"  # history keeps old accounts
    assert sum(sum(day["counts"].values()) for day in d["days"]) == cap + 1
    meta.save_page(meta.PageToken("main", "", meta.UNIFIED_PAGE_NAME, "t", "IG1", "sode.x"))
    (ig,) = [
        a for a in client.get("/api/uploads").json()["accounts"] if a["platform"] == "instagram"
    ]
    assert (ig["connected"], ig["label"]) == (True, "@sode.x")
    conn.close()


def test_a_deleted_post_is_asked_once_and_a_rate_limit_ends_the_pass(
    review: tuple[Config, TestClient, int],  # noqa: F811
) -> None:
    from peakreel.publish.publishers import PublishError

    config, _client, clip_id = review
    conn = connect(config.db_path)
    now = time.time()
    for account, remote in (("en", "deleted"), ("en2", "busy"), ("en3", "live")):
        conn.execute(
            "INSERT INTO publications (clip_id, account, platform, mode, state, metadata, "
            "remote_id, attempts, created_at, updated_at, published_at) "
            "VALUES (?, ?, 'instagram', 'public', 'published', '{}', ?, 0, ?, ?, ?)",
            (clip_id, account, remote, now, now, now - 4 * 3600),
        )
    conn.commit()
    asked: list[str] = []
    limited = True

    def fetch(account: str, remote_id: str) -> Stats:
        asked.append(remote_id)
        if remote_id == "deleted":
            raise PublishError("Meta: Unsupported get request. Object with ID '1' does not exist")
        if remote_id == "busy" and limited:
            raise PublishError("Meta rate limit", retry_after_s=3600)
        return Stats(likes=5)

    read_stats_once(conn, {"instagram": fetch}, now=now)
    assert asked == ["deleted", "busy"]  # the rate limit ended the pass before "live"
    limited = False
    asked.clear()
    read_stats_once(conn, {"instagram": fetch}, now=now)
    assert asked == ["busy", "live"]  # the deleted post is not asked again
    later = now + 80 * 3600  # every checkpoint has passed: still never asked
    asked.clear()
    read_stats_once(conn, {"instagram": fetch}, now=later)
    assert "deleted" not in asked
    conn.close()
