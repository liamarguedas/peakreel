from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from peakreel.config import ChannelConfig, Config, Handles, Platform, PublishConfig, load_config
from peakreel.db import connect, ensure_channel, init_db, insert_clip
from peakreel.publish.metadata import (
    ClipFacts,
    PostMetadata,
    build_metadata,
    game_hashtag,
    measure,
    validate,
)
from peakreel.publish.publishers import (
    ClipPackage,
    ExportPublisher,
    PublishError,
    PublishMode,
    PublishResult,
)
from peakreel.publish.queue import Edit, cancel_clip, clip_route, process_due, queue_clip
from peakreel.review.app import create_app
from peakreel.review.store import block_channels, get_clip, update_clip, view_of
from tests.conftest import WriteConfig

T0 = 1_794_900_000.0
CREDIT = "Clip from twitch.tv/alpha"


def facts(**kwargs: Any) -> ClipFacts:
    channel = kwargs.pop("channel", None) or ChannelConfig(
        name="alpha", display_name="Alpha", handles=Handles(tiktok="alpha_tt", youtube="alphayt")
    )
    return ClipFacts(channel=channel, **{"title": "No way", **kwargs})


# -- metadata ---------------------------------------------------------------------------------


def test_measure_units() -> None:
    text = "gg 🎉 é"
    assert measure(text, "chars") == 6
    assert measure(text, "utf16") == 7  # the emoji is a surrogate pair
    assert measure(text, "bytes") == 10


def test_game_hashtag() -> None:
    assert game_hashtag("Grand Theft Auto VI") == "GrandTheftAutoVI"
    assert game_hashtag("ファイナルファンタジー XIV") == "ファイナルファンタジーXIV"
    assert game_hashtag("!!!") is None


def test_every_platform_credits_and_mentions_only_known_handles() -> None:
    settings = PublishConfig()
    for platform in ("youtube", "tiktok", "instagram", "facebook"):
        meta = build_metadata(facts(game="Grand Theft Auto VI"), platform, settings)
        assert CREDIT in meta.text
        assert validate(meta, settings.limits(platform)) == []
        assert "#GrandTheftAutoVI" in meta.text
    youtube = build_metadata(facts(), "youtube", settings)
    assert youtube.title == "No way"
    assert "@alphayt" in youtube.text
    assert "https://www.twitch.tv/alpha" in youtube.text
    assert youtube.tags[:2] == ("Alpha", "twitch")  # "alpha" is a duplicate of "Alpha"
    assert "@alpha_tt" in build_metadata(facts(), "tiktok", settings).text
    assert "@" not in build_metadata(facts(), "instagram", settings).text  # no handle: no guess


def test_instagram_keeps_five_hashtags() -> None:
    channel = ChannelConfig(name="alpha", hashtags=("a1", "a2", "a3", "a4"))
    meta = build_metadata(facts(channel=channel, game="Some Game"), "instagram", PublishConfig())
    assert meta.hashtags == ("a1", "a2", "a3", "a4", "SomeGame")
    assert "kept the first 5 of 7 hashtags" in meta.notes
    assert validate(meta, PublishConfig().instagram) == []


def test_shortening_order_description_then_hashtags_then_title() -> None:
    settings = PublishConfig.model_validate({"tiktok": {"text_max": 120, "text_unit": "utf16"}})
    long_description = " ".join(["word"] * 60)
    meta = build_metadata(facts(description=long_description), "tiktok", settings)
    assert measure(meta.text, "utf16") <= 120
    assert CREDIT in meta.text
    assert "description shortened" in meta.notes
    assert meta.hashtags == ("twitch", "clips")  # description went first

    tight = PublishConfig.model_validate({"tiktok": {"text_max": 60, "text_unit": "utf16"}})
    meta = build_metadata(facts(title="A fairly long title here"), "tiktok", tight)
    assert measure(meta.text, "utf16") <= 60
    assert CREDIT in meta.text
    assert any("hashtag" in note for note in meta.notes)


def test_utf16_counting_with_emoji() -> None:
    settings = PublishConfig.model_validate({"tiktok": {"text_max": 80, "text_unit": "utf16"}})
    meta = build_metadata(facts(description="🎉" * 30), "tiktok", settings)
    assert measure(meta.text, "utf16") <= 80
    assert len(meta.text) < measure(meta.text, "utf16")  # emoji counted double


def test_credit_is_never_dropped_to_fit() -> None:
    settings = PublishConfig.model_validate({"tiktok": {"text_max": 20, "text_unit": "utf16"}})
    meta = build_metadata(facts(), "tiktok", settings)
    assert CREDIT in meta.text
    assert any("limit 20" in p for p in validate(meta, settings.tiktok))


def test_youtube_title_limit_and_forbidden_characters() -> None:
    meta = build_metadata(facts(title="<b>" + "x" * 150), "youtube", PublishConfig())
    assert meta.title is not None
    assert len(meta.title) <= 100
    assert meta.title.endswith("...")
    assert "<" not in meta.title
    assert validate(meta, PublishConfig().youtube) == []


def test_youtube_tags_fit_500_characters() -> None:
    channel = ChannelConfig(name="alpha", hashtags=tuple(f"tag{i:03d}longword" for i in range(60)))
    meta = build_metadata(facts(channel=channel), "youtube", PublishConfig())
    assert sum(len(t) for t in meta.tags) + len(meta.tags) - 1 <= 500


def test_build_is_deterministic() -> None:
    first = build_metadata(facts(description="x " * 3000), "youtube", PublishConfig())
    second = build_metadata(facts(description="x " * 3000), "youtube", PublishConfig())
    assert first == second
    assert measure(first.text, "bytes") <= 5000


def test_validate_catches_reviewer_edits() -> None:
    limits = PublishConfig().instagram
    meta = build_metadata(facts(), "instagram", PublishConfig())
    no_credit = PostMetadata.from_json({**meta.to_json(), "text": "lol #a"})
    assert any("credit line is missing" in p for p in validate(no_credit, limits))
    tags = " ".join(f"#t{i}" for i in range(6))
    too_many = PostMetadata.from_json({**meta.to_json(), "text": f"{CREDIT} {tags}"})
    assert "6 hashtags, limit 5" in validate(too_many, limits)


def test_partial_platform_override_keeps_platform_defaults() -> None:
    settings = PublishConfig.model_validate({"tiktok": {"daily_cap": 3}})
    assert (settings.tiktok.daily_cap, settings.tiktok.text_unit) == (3, "utf16")
    assert settings.youtube.title_max == 100


def test_template_and_channel_validation(write_config: WriteConfig) -> None:
    with pytest.raises(ValidationError, match="unknown variables"):
        PublishConfig(templates={"tiktok": {"text": "{title} {credit} {oops}"}})
    with pytest.raises(ValidationError, match="must contain"):
        PublishConfig(templates={"tiktok": {"text": "{title}"}})
    with pytest.raises(ValidationError, match="not a valid handle"):
        ChannelConfig(name="alpha", handles=Handles(tiktok="bad handle"))
    with pytest.raises(ValidationError, match="hashtags may contain"):
        ChannelConfig(name="alpha", hashtags=("no spaces",))
    custom = PublishConfig(templates={"tiktok": {"text": "{streamer} - {title}\n{credit}"}})
    meta = build_metadata(facts(), "tiktok", custom, custom.templates["tiktok"])
    assert meta.text == f"Alpha - No way\n{CREDIT}"


# -- queue ------------------------------------------------------------------------------------


@pytest.fixture
def setup(write_config: WriteConfig) -> tuple[Config, sqlite3.Connection, int]:
    path = write_config(
        {
            "channels": [
                {"name": "alpha", "display_name": "Alpha", "handles": {"tiktok": "alpha_tt"},
                 "permission": "unknown"},
            ],
            "publish": {"tiktok": {"min_spacing_min": 60, "daily_cap": 2}},
        }
    )  # fmt: skip
    config = load_config(path)
    init_db(config.db_path)
    conn = connect(config.db_path)
    clip_id = add_clip(config, conn, "c1", T0)
    return config, conn, clip_id


def add_clip(config: Config, conn: sqlite3.Connection, name: str, start: float) -> int:
    clip_dir = config.outbox_dir / "alpha"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / f"{name}.mp4").write_bytes(b"h")
    (clip_dir / f"{name}.vertical.mp4").write_bytes(b"v")
    (clip_dir / f"{name}.json").write_text(
        json.dumps({"stream": {"game": "Grand Theft Auto VI"}}), encoding="utf-8"
    )
    clip_id = insert_clip(
        conn, channel_id=ensure_channel(conn, "alpha"), spike_id=None, start_ts=start,
        end_ts=start + 30, path=clip_dir / f"{name}.mp4", sidecar_path=clip_dir / f"{name}.json",
    )  # fmt: skip
    conn.execute(
        "UPDATE clips SET vertical_path = ?, title = 'Huge play' WHERE id = ?",
        (str(clip_dir / f"{name}.vertical.mp4"), clip_id),
    )
    conn.commit()
    return clip_id


def pubs(conn: sqlite3.Connection, clip_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM publications WHERE clip_id = ?", (clip_id,)).fetchall()
    return {r["platform"]: r for r in rows}


def test_approving_exports_a_package_per_platform(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = setup
    view = update_clip(config, conn, clip_id, state="approved")
    rows = pubs(conn, clip_id)
    assert {p: r["state"] for p, r in rows.items()} == dict.fromkeys(
        ("youtube", "tiktok", "instagram", "facebook"), "exported"
    )
    assert {p["platform"] for p in view.publications} == {
        "youtube",
        "tiktok",
        "instagram",
        "facebook",
    }
    tiktok = Path(rows["tiktok"]["url"])
    assert tiktok.parent.parent == config.publish_dir / "main" / "tiktok"
    assert sorted(p.name for p in tiktok.iterdir()) == [
        "checklist.md", "description.txt", "metadata.json", "video.mp4",
    ]  # fmt: skip
    caption = (tiktok / "description.txt").read_text(encoding="utf-8")
    assert caption.startswith("Huge play")
    assert "Clip from twitch.tv/alpha @alpha_tt" in caption
    assert "#GrandTheftAutoVI" in caption
    assert (tiktok / "video.mp4").read_bytes() == b"v"
    assert "UNKNOWN" in (tiktok / "checklist.md").read_text(encoding="utf-8")
    youtube = Path(rows["youtube"]["url"])
    assert (youtube / "title.txt").read_text(encoding="utf-8") == "Huge play\n"
    assert (youtube / "tags.txt").exists()

    # Approving again re-exports in place: still one row and one package per platform.
    update_clip(config, conn, clip_id, state="approved", title="Even better")
    assert len(conn.execute("SELECT * FROM publications").fetchall()) == 4
    assert (tiktok / "description.txt").read_text(encoding="utf-8").startswith("Even better")
    assert len(list((config.publish_dir / "main" / "tiktok").glob("*/*"))) == 1


def test_platform_choice_edits_and_rejected_edits(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = setup
    from peakreel.review.store import ReviewError

    with pytest.raises(ReviewError, match="credit line is missing"):
        update_clip(config, conn, clip_id, state="approved", posts={"tiktok": Edit(text="lol")})
    assert pubs(conn, clip_id) == {}  # nothing queued
    assert get_clip(conn, clip_id)["state"] == "new"  # type: ignore[index]

    edited = f"My caption\n\n{CREDIT}\n\n#gg"
    update_clip(config, conn, clip_id, state="approved", platforms=["tiktok"],
                posts={"tiktok": Edit(text=edited)})  # fmt: skip
    rows = pubs(conn, clip_id)
    assert set(rows) == {"tiktok"}
    caption = Path(rows["tiktok"]["url"]) / "description.txt"
    assert caption.read_text(encoding="utf-8") == edited + "\n"
    # A later re-export keeps the reviewer's edit even when the title changes.
    update_clip(config, conn, clip_id, state="approved", title="New", platforms=["tiktok"])
    assert caption.read_text(encoding="utf-8") == edited + "\n"


def test_unapprove_and_takedown_cancel_packages(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = setup
    update_clip(config, conn, clip_id, state="approved")
    packages = [Path(r["url"]) for r in pubs(conn, clip_id).values()]
    assert all(p.is_dir() for p in packages)
    update_clip(config, conn, clip_id, state="rejected")
    assert not any(p.exists() for p in packages)
    assert {r["state"] for r in pubs(conn, clip_id).values()} == {"cancelled"}

    update_clip(config, conn, clip_id, state="approved")
    assert all(p.is_dir() for p in packages)
    block_channels(config, conn, ["alpha"])
    assert not any(p.exists() for p in packages)
    assert {r["state"] for r in pubs(conn, clip_id).values()} == {"cancelled"}


def test_schedule_keeps_spacing_and_daily_cap(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, first = setup
    now = 1_795_000_000.0  # 2026-11-18 09:46:40 UTC
    ids = [first] + [add_clip(config, conn, f"c{i}", T0 + i) for i in range(2, 5)]
    for clip_id in ids:
        row = get_clip(conn, clip_id)
        assert row is not None
        queue_clip(config, conn, view_of(row), row, ["tiktok"], now=now)
    times = [pubs(conn, i)["tiktok"]["scheduled_at"] for i in ids]
    assert times[0] == now
    assert times[1] == now + 3600  # 60 min spacing
    # The cap is 2 per UTC day, so the third and fourth move to the next day.
    assert times[2] == 1_795_046_400.0  # 2026-11-19 00:00 UTC
    assert times[3] == 1_795_046_400.0 + 3600


class FlakyNetwork:
    """A network-style publisher: fails, then succeeds; remembers every upload."""

    modes: frozenset[PublishMode] = frozenset({"private", "public"})

    def __init__(self, platform: Platform, failures: int = 0) -> None:
        self.platform: Platform = platform
        self.failures = failures
        self.uploads: list[int] = []
        self.known: dict[int, PublishResult] = {}

    def validate(self, clip: ClipPackage, meta: PostMetadata) -> list[str]:
        return []

    def publish(self, clip: ClipPackage, meta: PostMetadata, mode: PublishMode) -> PublishResult:
        if self.failures:
            self.failures -= 1
            raise PublishError("HTTP 503")
        self.uploads.append(clip.clip_id)
        result = PublishResult("published", remote_id=f"v{clip.clip_id}", url="https://x/v")
        self.known[clip.clip_id] = result
        return result

    def poll_status(self, clip: ClipPackage, remote_id: str | None) -> PublishResult | None:
        return self.known.get(clip.clip_id)

    def remove(self, clip: ClipPackage, url: str | None) -> bool:
        return False


def network_config(config: Config, mode: str = "private") -> Config:
    publish = config.publish.model_copy(
        update={"mode": mode, "platforms": ("youtube",), "max_attempts": 3, "retry_initial_s": 10}
    )
    return config.model_copy(update={"publish": publish})


def test_retries_with_backoff_then_fails(setup: tuple[Config, sqlite3.Connection, int]) -> None:
    config, conn, clip_id = setup
    config = network_config(config)
    row = get_clip(conn, clip_id)
    assert row is not None
    queue_clip(config, conn, view_of(row), row, ["youtube"], now=T0)
    flaky = FlakyNetwork("youtube", failures=5)
    publishers = {"youtube": flaky}
    process_due(config, conn, publishers, now=T0)  # type: ignore[arg-type]
    pub = pubs(conn, clip_id)["youtube"]
    assert (pub["state"], pub["attempts"], pub["next_attempt_at"]) == ("approved", 1, T0 + 10)
    assert process_due(config, conn, publishers, now=T0 + 5) == []  # type: ignore[arg-type]
    process_due(config, conn, publishers, now=T0 + 10)  # type: ignore[arg-type]
    assert pubs(conn, clip_id)["youtube"]["next_attempt_at"] == T0 + 10 + 20  # doubled
    process_due(config, conn, publishers, now=T0 + 30)  # type: ignore[arg-type]
    pub = pubs(conn, clip_id)["youtube"]
    assert (pub["state"], pub["attempts"], pub["error"]) == ("failed", 3, "HTTP 503")


def test_crash_mid_upload_never_posts_twice(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = setup
    config = network_config(config)
    other = add_clip(config, conn, "c2", T0 + 1)
    for cid in (clip_id, other):
        row = get_clip(conn, cid)
        assert row is not None
        queue_clip(config, conn, view_of(row), row, ["youtube"], now=T0)
    network = FlakyNetwork("youtube")
    # Simulate a kill after the upload of clip 1 went through but before the result was
    # recorded, and a kill before clip 2's upload started: both rows are 'uploading'.
    network.known[clip_id] = PublishResult("published", remote_id="v1", url="https://x/v1")
    conn.execute("UPDATE publications SET state = 'uploading', attempts = 1")
    conn.commit()

    results = process_due(config, conn, {"youtube": network}, now=T0 + 99999)  # type: ignore[arg-type]
    assert network.uploads == []  # nothing sent again
    assert {r.clip_id: r.state for r in results} == {clip_id: "published", other: "failed"}
    assert "check the platform" in pubs(conn, other)["youtube"]["error"]
    # A published clip is never re-queued.
    row = get_clip(conn, clip_id)
    assert row is not None
    notes = queue_clip(config, conn, view_of(row), row, ["youtube"], now=T0 + 99999)
    assert notes == ["youtube: already published, left as is"]
    assert process_due(config, conn, {"youtube": network}, now=T0 + 99999) == []  # type: ignore[arg-type]
    # Cancelling cannot remove a published post: it is reported instead.
    assert cancel_clip(config, conn, view_of(row), row) == ["main/youtube: published https://x/v1"]


def test_interrupted_export_is_written_again(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = setup
    row = get_clip(conn, clip_id)
    assert row is not None
    queue_clip(config, conn, view_of(row), row, ["tiktok"], now=T0)
    conn.execute("UPDATE publications SET state = 'uploading'")
    conn.commit()
    (result,) = process_due(config, conn, now=T0)
    assert result.state == "exported"
    assert Path(pubs(conn, clip_id)["tiktok"]["url"]).is_dir()


def test_dry_run_changes_nothing(setup: tuple[Config, sqlite3.Connection, int]) -> None:
    config, conn, clip_id = setup
    row = get_clip(conn, clip_id)
    assert row is not None
    queue_clip(config, conn, view_of(row), row, ["tiktok"], now=T0)
    (result,) = process_due(config, conn, now=T0, dry_run=True)
    assert (result.state, result.detail) == ("approved", "would export")
    assert pubs(conn, clip_id)["tiktok"]["state"] == "approved"
    assert not config.publish_dir.exists()


def test_export_publisher_refuses_other_modes(tmp_path: Path) -> None:
    clip = ClipPackage(1, "c1", tmp_path / "missing.mp4", None, ChannelConfig(name="a"), None, [])
    publisher = ExportPublisher("tiktok", tmp_path)
    meta = build_metadata(facts(), "tiktok", PublishConfig())
    with pytest.raises(PublishError, match="only export"):
        publisher.publish(clip, meta, "public")
    with pytest.raises(PublishError, match="video file missing"):
        publisher.publish(clip, meta, "export")


# -- review API -------------------------------------------------------------------------------


def test_review_api_posts_preview_and_approve(
    setup: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = setup
    conn.close()
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    settings = client.get("/api/settings").json()
    assert settings["mode"] == "export"
    assert settings["enabled"] == ["youtube", "tiktok", "instagram", "facebook"]
    routed = client.get(f"/api/clips/{clip_id}/posts").json()
    assert routed["route"]["account"]["id"] == "main"
    preview = routed["posts"]
    assert set(preview) == {"youtube", "tiktok", "instagram", "facebook"}
    assert preview["tiktok"]["problems"] == []
    assert preview["tiktok"]["limits"] == {
        "title_max": None,
        "text_max": 2200,
        "text_unit": "utf16",
    }
    assert preview["youtube"]["meta"]["title"] == "Huge play"
    assert preview["tiktok"]["publication"] is None

    bad = client.post(
        f"/api/clips/{clip_id}", json={"state": "approved", "posts": {"tiktok": {"text": "x"}}}
    )
    assert bad.status_code == 409
    assert "credit line is missing" in bad.json()["detail"]
    assert (
        client.post(
            f"/api/clips/{clip_id}", json={"state": "approved", "platforms": ["myspace"]}
        ).status_code
        == 422
    )

    ok = client.post(f"/api/clips/{clip_id}", json={"state": "approved", "platforms": ["tiktok"]})
    assert ok.status_code == 200
    assert [p["platform"] for p in ok.json()["publications"]] == ["tiktok"]
    assert ok.json()["publications"][0]["state"] == "exported"
    after = client.get(f"/api/clips/{clip_id}/posts").json()["posts"]
    assert after["tiktok"]["publication"]["state"] == "exported"


# -- accounts per language --------------------------------------------------------------------

ACCOUNTS = [
    {"id": "en", "name": "Clips EN", "languages": ["en"]},
    {"id": "es", "name": "Clips ES", "languages": ["es"], "credit": "Clip de twitch.tv/{channel}",
     "hashtags": ["clipsenespanol"]},
    {"id": "ja", "languages": ["ja"], "platforms": ["tiktok"],
     "credit": "クリップ元: twitch.tv/{channel}"},
]  # fmt: skip


@pytest.fixture
def accounts(write_config: WriteConfig) -> tuple[Config, sqlite3.Connection, int]:
    path = write_config(
        {
            "channels": [{"name": "alpha", "language": "pt"}],
            "publish": {"accounts": ACCOUNTS},
        }
    )
    config = load_config(path)
    init_db(config.db_path)
    conn = connect(config.db_path)
    return config, conn, add_clip(config, conn, "c1", T0)


def test_account_config_validation() -> None:
    with pytest.raises(ValidationError, match="language 'en' is on accounts a and b"):
        PublishConfig.model_validate(
            {"accounts": [{"id": "a", "languages": ["en"]}, {"id": "b", "languages": ["en"]}]}
        )
    with pytest.raises(ValidationError, match="at most one account"):
        PublishConfig.model_validate({"accounts": [{"id": "a"}, {"id": "b"}]})
    with pytest.raises(ValidationError, match=r"must contain \{channel_url\}"):
        PublishConfig.model_validate({"accounts": [{"id": "a", "credit": "Clip by {streamer}"}]})
    with pytest.raises(ValidationError, match="ISO 639-1"):
        ChannelConfig(name="alpha", language="english")
    settings = PublishConfig.model_validate({"accounts": [*ACCOUNTS, {"id": "rest"}]})
    assert settings.account_for("es").id == "es"  # type: ignore[union-attr]
    assert settings.account_for("de").id == "rest"  # type: ignore[union-attr]
    assert PublishConfig.model_validate({"accounts": ACCOUNTS}).account_for("de") is None


def test_language_precedence(accounts: tuple[Config, sqlite3.Connection, int]) -> None:
    config, conn, clip_id = accounts
    sidecar = config.outbox_dir / "alpha" / "c1.json"
    sidecar.write_text(
        json.dumps({"stream": {"language": "es"}, "captions": {"language": "ja"}}),
        encoding="utf-8",
    )
    row = get_clip(conn, clip_id)
    assert row is not None
    view = view_of(row)
    route = clip_route(config, view, row)
    assert (route.language, route.source, route.account) == ("pt", "channel", None)
    assert clip_route(config, view, row, "en").account.id == "en"  # type: ignore[union-attr]
    no_channel = config.model_copy(update={"channels": ()})
    assert clip_route(no_channel, view, row).source == "twitch"
    sidecar.write_text(json.dumps({"captions": {"language": "ja"}}), encoding="utf-8")
    route = clip_route(no_channel, view, row)
    assert (route.language, route.source, route.account.id) == ("ja", "speech", "ja")  # type: ignore[union-attr]


def test_clip_without_account_is_held_until_a_language_is_chosen(
    accounts: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = accounts
    update_clip(config, conn, clip_id, state="approved")
    assert pubs(conn, clip_id) == {}  # pt has no account: held, nothing exported
    assert get_clip(conn, clip_id)["state"] == "approved"  # type: ignore[index]

    update_clip(config, conn, clip_id, state="approved", language="es")
    rows = pubs(conn, clip_id)
    assert set(rows) == {"youtube", "tiktok", "instagram", "facebook"}
    assert {r["account"] for r in rows.values()} == {"es"}
    package = Path(rows["tiktok"]["url"])
    assert package.parent.parent.parent == config.publish_dir / "es"
    caption = (package / "description.txt").read_text(encoding="utf-8")
    assert "Clip de twitch.tv/alpha" in caption
    assert "#clipsenespanol" in caption

    # Changing the language moves the clip: the ES packages go, the JA one (TikTok only) comes.
    update_clip(config, conn, clip_id, state="approved", language="ja")
    assert not package.exists()
    live = {
        (r["account"], r["platform"])
        for r in conn.execute("SELECT * FROM publications WHERE state = 'exported'")
    }
    assert live == {("ja", "tiktok")}
    ja = conn.execute("SELECT url FROM publications WHERE account = 'ja'").fetchone()[0]
    assert "クリップ元: twitch.tv/alpha" in (Path(ja) / "description.txt").read_text(
        encoding="utf-8"
    )
    from peakreel.review.store import ReviewError

    with pytest.raises(ReviewError, match="not a language code"):
        update_clip(config, conn, clip_id, language="english")
    update_clip(config, conn, clip_id, language="")  # clear: back to the channel's (pt)
    assert get_clip(conn, clip_id)["language"] is None  # type: ignore[index]


def test_pacing_is_per_account(accounts: tuple[Config, sqlite3.Connection, int]) -> None:
    config, conn, first = accounts
    second = add_clip(config, conn, "c2", T0 + 1)
    now = 1_795_000_000.0
    for clip_id, language in ((first, "en"), (second, "es")):
        conn.execute("UPDATE clips SET language = ? WHERE id = ?", (language, clip_id))
        conn.commit()
        row = get_clip(conn, clip_id)
        assert row is not None
        queue_clip(config, conn, view_of(row), row, ["tiktok"], now=now)
    assert pubs(conn, first)["tiktok"]["scheduled_at"] == now
    assert pubs(conn, second)["tiktok"]["scheduled_at"] == now  # another account: no wait


def test_review_api_routes_and_previews_languages(
    accounts: tuple[Config, sqlite3.Connection, int],
) -> None:
    config, conn, clip_id = accounts
    conn.close()
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    (clip,) = client.get("/api/clips").json()
    assert clip["route"] == {"language": "pt", "source": "channel", "account": None}
    held = client.get(f"/api/clips/{clip_id}/posts").json()
    assert held["posts"] == {}
    preview = client.get(f"/api/clips/{clip_id}/posts?language=ja").json()
    assert preview["route"]["account"]["id"] == "ja"
    assert set(preview["posts"]) == {"tiktok"}
    assert client.get(f"/api/clips/{clip_id}/posts?language=x1").status_code == 422
    saved = client.post(f"/api/clips/{clip_id}", json={"language": "en"}).json()
    assert saved["route"]["account"]["id"] == "en"
    assert saved["route"]["source"] == "chosen"
    accounts_json = client.get("/api/settings").json()["accounts"]
    assert [a["id"] for a in accounts_json] == ["en", "es", "ja"]
