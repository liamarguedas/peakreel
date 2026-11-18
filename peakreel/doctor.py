"""Environment checks behind ``peakreel doctor``.

Each check is independent and returns a :class:`CheckResult`, so one failure never
hides another. The CLI prints one line per check and exits non-zero if any failed.
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from peakreel.blocklist import load_blocklist
from peakreel.config import Config, ConfigError, resolve_tool
from peakreel.db import init_db

__all__ = [
    "CLOCK_REFERENCE_URL",
    "MAX_CLOCK_SKEW_S",
    "MIN_PYTHON",
    "CheckResult",
    "check_clock",
    "check_tool",
    "run_checks",
]

MIN_PYTHON = (3, 12)
MAX_CLOCK_SKEW_S = 2.0
CLOCK_REFERENCE_URL = "https://api.twitch.tv"
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


def _fetch_server_time(url: str) -> float:
    """Return the time in a server's HTTP ``Date`` header, as UTC epoch seconds."""
    request = urllib.request.Request(url, method="HEAD")  # noqa: S310 (fixed https URL)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            header = response.headers.get("Date")
    except urllib.error.HTTPError as exc:  # an error status still carries a Date header
        header = exc.headers.get("Date")
    if not header:
        raise ValueError(f"{url} sent no Date header")
    return parsedate_to_datetime(header).timestamp()


def check_clock(
    url: str = CLOCK_REFERENCE_URL,
    *,
    fetch: Callable[[str], float] = _fetch_server_time,
    clock: Callable[[], float] = time.time,
) -> CheckResult:
    """Compare the local clock with a reference server's clock.

    Chat timestamps are local wall-clock time, so a skewed clock shifts every stored
    timestamp relative to Twitch (VOD offsets, ``server_ts``) and to other machines.

    Parameters
    ----------
    url : str, default ``CLOCK_REFERENCE_URL``
        HTTPS endpoint whose ``Date`` header is used as reference.
    fetch : callable, optional
        Returns the reference time for ``url``. Injectable for tests.
    clock : callable, optional
        Returns local UTC epoch seconds. Injectable for tests.

    Returns
    -------
    CheckResult
        Fails when the skew exceeds ``MAX_CLOCK_SKEW_S`` or the reference cannot be
        reached.

    Notes
    -----
    The ``Date`` header has one-second resolution, so the measurement is accurate to
    roughly one second. The midpoint of the request is used as the local reference
    to halve the network delay error.
    """
    before = clock()
    try:
        server = fetch(url)
    except (OSError, ValueError) as exc:
        return CheckResult("clock", False, f"cannot reach {url} to compare clocks: {exc}")
    local = (before + clock()) / 2
    # The header truncates to whole seconds, so the true server time is about server + 0.5.
    skew = local - (server + 0.5)
    direction = "ahead of" if skew > 0 else "behind"
    detail = f"local clock is {abs(skew):.1f}s {direction} {url}"
    if abs(skew) > MAX_CLOCK_SKEW_S:
        return CheckResult(
            "clock",
            False,
            f"{detail} (limit {MAX_CLOCK_SKEW_S}s). Resync it: run `w32tm /resync` as "
            f"administrator, or enable 'Set time automatically' in Windows settings",
        )
    return CheckResult("clock", True, detail)


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


def _check_whisper_model(config: Config) -> CheckResult:
    if not config.captions.enabled:
        return CheckResult("captions", True, "disabled in config")
    model_dir = config.resolve(config.tools.whisper_model_dir)
    marker = f"faster-whisper-{config.captions.model}"
    cached = [p for p in model_dir.rglob("model.bin") if marker in p.as_posix()]
    if not cached:
        return CheckResult(
            "captions",
            False,
            f"model {config.captions.model!r} not in {model_dir}; run `peakreel captions download`",
        )
    return CheckResult("captions", True, f"model {config.captions.model!r} cached ({model_dir})")


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


def run_checks(config: Config, *, network: bool = True) -> list[CheckResult]:
    """Run every environment check.

    Parameters
    ----------
    config : Config
        A loaded configuration. (Config loading itself is reported by the CLI, since
        no other check can run without it.)
    network : bool, default True
        Whether to run checks that need the internet (currently the clock check).

    Returns
    -------
    list of CheckResult
        One result per check, in display order: Python, clock (when ``network``),
        ffmpeg, ffprobe, streamlink, data directory, database, captions model,
        blocklist.

    Notes
    -----
    Side effects: creates the data directory and the SQLite database if missing, and
    upgrades the database schema to the latest version.
    """
    tools = config.tools
    return [
        _check_python(),
        *([check_clock()] if network else []),
        check_tool("ffmpeg", tools.ffmpeg, config.root, "-version"),
        check_tool("ffprobe", tools.ffprobe, config.root, "-version"),
        check_tool("streamlink", tools.streamlink, config.root, "--version"),
        _check_data_dir(config),
        _check_database(config),
        _check_whisper_model(config),
        _check_blocklist(config),
    ]
