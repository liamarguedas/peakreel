from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from peakreel.cli import app
from peakreel.config import load_config
from peakreel.db import MIGRATIONS
from peakreel.doctor import check_clock, check_tool, run_checks
from tests.conftest import WriteConfig

runner = CliRunner()

# The Python interpreter stands in for ffmpeg/ffprobe/streamlink: it is the one
# executable guaranteed to exist on every machine that runs the tests.
PYTHON = str(Path(sys.executable))


def _tools(**overrides: str) -> dict[str, dict[str, str]]:
    return {"tools": {"ffmpeg": PYTHON, "ffprobe": PYTHON, "streamlink": PYTHON, **overrides}}


def test_check_tool_reports_version_and_path(tmp_path: Path) -> None:
    result = check_tool("ffmpeg", PYTHON, tmp_path, "--version")
    assert result.ok
    assert result.detail.startswith("Python 3.")
    assert PYTHON in result.detail


def test_check_tool_not_found(tmp_path: Path) -> None:
    result = check_tool("ffmpeg", "C:/nope/ffmpeg.exe", tmp_path, "-version")
    assert not result.ok
    assert "not found" in result.detail
    assert "tools.ffmpeg" in result.detail


def test_check_tool_non_zero_exit(tmp_path: Path) -> None:
    result = check_tool("ffmpeg", PYTHON, tmp_path, "--definitely-not-an-option")
    assert not result.ok
    assert "exited with" in result.detail


def test_check_tool_not_executable(tmp_path: Path) -> None:
    fake = tmp_path / "ffmpeg.exe"
    fake.write_text("not a real binary", encoding="utf-8")
    result = check_tool("ffmpeg", str(fake), tmp_path, "-version")
    assert not result.ok
    assert "cannot execute" in result.detail


def test_run_checks_all_pass_and_create_database(write_config: WriteConfig) -> None:
    config = load_config(write_config(_tools()))
    results = run_checks(config, network=False)
    # ffmpeg/ffprobe are probed with "-version", which Python rejects; only assert the rest.
    by_name = {r.name: r for r in results}
    assert [r.name for r in results] == [
        "python",
        "ffmpeg",
        "ffprobe",
        "streamlink",
        "data dir",
        "database",
        "captions",
        "blocklist",
    ]
    for name in ("python", "streamlink", "data dir", "database", "blocklist"):
        assert by_name[name].ok, by_name[name].detail
    assert config.db_path.is_file()
    assert not list(config.data_dir.glob(".doctor-*"))


def test_blocklisted_configured_channel_fails(write_config: WriteConfig) -> None:
    path = write_config({**_tools(), "channels": [{"name": "alpha"}]})
    (path.parent / "blocklist.yaml").write_text("channels: [Alpha]\n", encoding="utf-8")
    by_name = {r.name: r for r in run_checks(load_config(path), network=False)}
    assert not by_name["blocklist"].ok
    assert "alpha" in by_name["blocklist"].detail


def test_cli_doctor_fails_with_one_line_per_check(write_config: WriteConfig) -> None:
    path = write_config(_tools(ffmpeg="C:/nope/ffmpeg.exe"))
    result = runner.invoke(app, ["--config", str(path), "doctor", "--offline"])
    assert result.exit_code == 1
    assert "[FAIL] ffmpeg" in result.output
    assert "[PASS] streamlink" in result.output
    assert "[PASS] database" in result.output


def test_cli_doctor_reports_invalid_config(write_config: WriteConfig) -> None:
    path = write_config({"output": {"width": -1}})
    result = runner.invoke(app, ["--config", str(path), "doctor"])
    assert result.exit_code == 1
    assert "[FAIL] config" in result.output
    assert "output.width" in result.output


def test_cli_db_init_and_version(write_config: WriteConfig) -> None:
    path = write_config()
    result = runner.invoke(app, ["--config", str(path), "db", "init"])
    assert result.exit_code == 0, result.output
    assert f"schema v{len(MIGRATIONS)}" in result.output
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("peakreel ")


def test_check_clock_within_limit() -> None:
    result = check_clock("https://ref", fetch=lambda _: 1000.0, clock=lambda: 1000.9)
    assert result.ok
    assert "0.4s ahead of https://ref" in result.detail


def test_check_clock_skewed_names_the_fix() -> None:
    result = check_clock("https://ref", fetch=lambda _: 1000.0, clock=lambda: 877.5)
    assert not result.ok
    assert "123.0s behind" in result.detail
    assert "w32tm /resync" in result.detail


def test_check_clock_unreachable() -> None:
    def fail(_: str) -> float:
        raise OSError("network down")

    result = check_clock("https://ref", fetch=fail)
    assert not result.ok
    assert "cannot reach" in result.detail


def test_captions_check_reports_missing_model_and_can_be_disabled(
    write_config: WriteConfig,
) -> None:
    config = load_config(write_config(_tools()))
    by_name = {r.name: r for r in run_checks(config, network=False)}
    assert not by_name["captions"].ok
    assert "peakreel captions download" in by_name["captions"].detail
    config = load_config(write_config({**_tools(), "captions": {"enabled": False}}))
    by_name = {r.name: r for r in run_checks(config, network=False)}
    assert by_name["captions"].ok
