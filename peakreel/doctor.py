"""Environment checks behind ``peakreel doctor``.

Each check is independent and returns a :class:`CheckResult`, so one failure never
hides another. The CLI prints one line per check and exits non-zero if any failed.
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from peakreel.blocklist import load_blocklist
from peakreel.config import Config, ConfigError, resolve_tool
from peakreel.db import init_db

__all__ = ["MIN_PYTHON", "CheckResult", "check_tool", "run_checks"]

MIN_PYTHON = (3, 12)
_TOOL_TIMEOUT_S = 20


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one environment check.

    Attributes
    ----------
    name : str
        Short label, for example ``"ffmpeg"``.
    ok : bool
        Whether the check passed.
    detail : str
        On success, what was found (version, path). On failure, what is wrong and
        how to fix it.
    """

    name: str
    ok: bool
    detail: str


def _check_python() -> CheckResult:
    found = ".".join(str(p) for p in sys.version_info[:3])
    wanted = ".".join(str(p) for p in MIN_PYTHON)
    if sys.version_info[:2] >= MIN_PYTHON:
        return CheckResult("python", True, f"{found} ({sys.executable})")
    return CheckResult("python", False, f"{found} found, {wanted} or newer required")


def check_tool(name: str, configured: str, root: Path, version_arg: str) -> CheckResult:
    """Check that an external tool can be located and executed.

    Parameters
    ----------
    name : str
        Label for the result, also the key under ``tools:`` in the config.
    configured : str
        The configured value (path or bare command name).
    root : pathlib.Path
        Config directory, used to resolve relative paths.
    version_arg : str
        Argument that makes the tool print its version and exit 0, for example
        ``"-version"`` for ffmpeg.

    Returns
    -------
    CheckResult
        Passing results carry the first line of the version output and the resolved
        path. Failing results say whether the tool was not found, could not be
        started, timed out, or exited non-zero.

    Notes
    -----
    The tool is started with an argument list and never through a shell.
    """
    path = resolve_tool(configured, root)
    if path is None:
        return CheckResult(
            name,
            False,
            f"not found (tools.{name} = {configured!r}). "
            f"Install it or set an absolute path in config.local.yaml",
        )
    try:
        proc = subprocess.run(
            [str(path), version_arg],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TOOL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        return CheckResult(name, False, f"cannot execute {path}: {exc.strerror or exc}")
    except subprocess.TimeoutExpired:
        return CheckResult(name, False, f"{path} {version_arg} timed out after {_TOOL_TIMEOUT_S}s")
    output = (proc.stdout or proc.stderr).strip()
    first_line = output.splitlines()[0] if output else ""
    if proc.returncode != 0:
        return CheckResult(
            name, False, f"{path} {version_arg} exited with {proc.returncode}: {first_line}"
        )
    return CheckResult(name, True, f"{first_line} ({path})")


def _check_data_dir(config: Config) -> CheckResult:
    data_dir = config.data_dir
    probe = data_dir / f".doctor-{uuid.uuid4().hex}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return CheckResult("data dir", False, f"{data_dir} is not writable: {exc.strerror or exc}")
    finally:
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)
    return CheckResult("data dir", True, f"writable ({data_dir})")


def _check_database(config: Config) -> CheckResult:
    try:
        version = init_db(config.db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return CheckResult("database", False, f"{config.db_path}: {exc}")
    return CheckResult("database", True, f"schema v{version} ({config.db_path})")


def _check_blocklist(config: Config) -> CheckResult:
    try:
        blocked = load_blocklist(config.blocklist_path)
    except ConfigError as exc:
        return CheckResult("blocklist", False, str(exc))
    watched_and_blocked = sorted(c.name for c in config.channels if c.name in blocked)
    if watched_and_blocked:
        return CheckResult(
            "blocklist",
            False,
            f"configured channels are on the blocklist: {', '.join(watched_and_blocked)}. "
            f"Remove them from `channels`",
        )
    return CheckResult(
        "blocklist", True, f"{len(blocked)} blocked channel(s) ({config.blocklist_path})"
    )


def run_checks(config: Config) -> list[CheckResult]:
    """Run every environment check.

    Parameters
    ----------
    config : Config
        A loaded configuration. (Config loading itself is reported by the CLI, since
        no other check can run without it.)

    Returns
    -------
    list of CheckResult
        One result per check, in display order: Python, ffmpeg, ffprobe, streamlink,
        data directory, database, blocklist.

    Notes
    -----
    Side effects: creates the data directory and the SQLite database if missing, and
    upgrades the database schema to the latest version.
    """
    tools = config.tools
    return [
        _check_python(),
        check_tool("ffmpeg", tools.ffmpeg, config.root, "-version"),
        check_tool("ffprobe", tools.ffprobe, config.root, "-version"),
        check_tool("streamlink", tools.streamlink, config.root, "--version"),
        _check_data_dir(config),
        _check_database(config),
        _check_blocklist(config),
    ]
