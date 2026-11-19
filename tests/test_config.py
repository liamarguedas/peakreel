from __future__ import annotations

import sys
from pathlib import Path

import pytest

from peakreel.blocklist import load_blocklist
from peakreel.config import ConfigError, deep_merge, load_config, resolve_tool
from tests.conftest import WriteConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_config_is_valid_and_matches_milestone_defaults() -> None:
    config = load_config(REPO_ROOT / "config.yaml", use_local_overlay=False)
    out = config.output
    assert (out.width, out.height, out.fps) == (1080, 1920, 30)
    assert (out.video_codec, out.pixel_format, out.audio_codec) == ("h264_nvenc", "yuv420p", "aac")
    assert (out.clip_min_s, out.clip_max_s) == (20, 45)
    assert (out.safe_zone_bottom, out.safe_zone_right) == (0.20, 0.12)
    assert "nightbot" in config.chat.bots
    assert load_blocklist(config.blocklist_path) == frozenset()


def test_defaults_and_paths_resolve_against_config_dir(write_config: WriteConfig) -> None:
    path = write_config()
    config = load_config(path)
    assert config.root == path.parent.resolve()
    assert config.data_dir == config.root / "data"
    assert config.chat_log_dir == config.root / "data" / "chat"
    assert config.db_path == config.root / "data" / "peakreel.db"
    assert config.log_file == config.root / "data" / "logs" / "peakreel.jsonl"


def test_local_overlay_merges_mappings_and_replaces_lists(write_config: WriteConfig) -> None:
    path = write_config(
        {"tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}, "chat": {"bots": ["a", "b"]}},
        local={"tools": {"ffmpeg": "C:/tools/ffmpeg.exe"}, "chat": {"bots": ["C"]}},
    )
    config = load_config(path)
    assert config.tools.ffmpeg == "C:/tools/ffmpeg.exe"
    assert config.tools.ffprobe == "ffprobe"
    assert config.chat.bots == frozenset({"c"})
    assert load_config(path, use_local_overlay=False).tools.ffmpeg == "ffmpeg"


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"x": 1}}
    overlay = {"a": {"y": 2}}
    assert deep_merge(base, overlay) == {"a": {"x": 1, "y": 2}}
    assert base == {"a": {"x": 1}}
    assert overlay == {"a": {"y": 2}}


def test_channel_names_are_normalised_with_default_credit(write_config: WriteConfig) -> None:
    config = load_config(
        write_config({"channels": [{"name": "#SomeStreamer"}, {"name": "b", "credit": "x"}]})
    )
    assert [c.name for c in config.channels] == ["somestreamer", "b"]
    assert config.channels[0].credit_text == "Clip from twitch.tv/somestreamer"
    assert config.channels[1].credit_text == "x"
    assert config.channel("SOMESTREAMER") is config.channels[0]
    assert config.channel("missing") is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"output": {"width": "wide"}}, "output.width"),
        ({"outptu": {}}, "outptu"),
        ({"chat": {"keepalive_interval_s": 0}}, "chat.keepalive_interval_s"),
        ({"output": {"safe_zone_bottom": 1.5}}, "output.safe_zone_bottom"),
        ({"channels": [{"name": "bad name!"}]}, "channels.0.name"),
        ({"channels": [{"name": "a"}, {"name": "A"}]}, "duplicate channels: a"),
        ({"root": "/elsewhere"}, "root"),
    ],
)
def test_invalid_config_names_the_field(
    write_config: WriteConfig, data: dict[str, object], expected: str
) -> None:
    with pytest.raises(ConfigError, match="invalid configuration") as excinfo:
        load_config(write_config(data))
    assert expected in str(excinfo.value)


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "config.yaml"
    bad.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad)


def test_resolve_tool(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "tool.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert resolve_tool(str(exe), tmp_path) == exe
    assert resolve_tool("bin/tool.exe", tmp_path) == exe
    assert resolve_tool("bin/missing.exe", tmp_path) is None
    assert resolve_tool("", tmp_path) is None
    assert resolve_tool("definitely-not-a-real-tool-xyz", tmp_path) is None
    # A bare name is found next to the interpreter (the virtualenv scripts directory).
    found = resolve_tool(Path(sys.executable).stem, tmp_path)
    assert found is not None
    assert found.parent == Path(sys.executable).parent


def test_blocklist_formats(tmp_path: Path) -> None:
    path = tmp_path / "blocklist.yaml"
    path.write_text(
        "channels:\n  - name: '#Alpha'\n    reason: asked\n  - beta\n", encoding="utf-8"
    )
    assert load_blocklist(path) == frozenset({"alpha", "beta"})
    path.write_text("", encoding="utf-8")
    assert load_blocklist(path) == frozenset()


@pytest.mark.parametrize("content", ["- a\n", "channels: nope\n", "channels:\n  - reason: x\n"])
def test_blocklist_malformed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "blocklist.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_blocklist(path)


def test_blocklist_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read blocklist"):
        load_blocklist(tmp_path / "missing.yaml")
