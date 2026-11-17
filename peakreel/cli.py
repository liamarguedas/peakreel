"""The ``peakreel`` command line interface.

Run ``peakreel --help`` for the command list. Every command reads ``config.yaml``
from the current directory unless ``--config`` points elsewhere.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from peakreel import __version__
from peakreel.blocklist import load_blocklist
from peakreel.chat.gaps import analyze_gaps
from peakreel.chat.ingest import record_chat
from peakreel.chat.replay import read_log
from peakreel.chat.twitch import TwitchChat
from peakreel.chat.writer import ChatLogWriter
from peakreel.config import Config, ConfigError, load_config
from peakreel.db import init_db
from peakreel.detect.filters import ChatFilter
from peakreel.doctor import CheckResult, run_checks
from peakreel.log import configure_logging, get_logger

__all__ = ["app"]

app = typer.Typer(
    name="peakreel",
    help="Detect Twitch chat spikes and turn them into captioned vertical clips.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")


class _State:
    """Options shared by all commands, filled in by the root callback."""

    config_path: Path = Path("config.yaml")


_state = _State()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"peakreel {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to config.yaml.", show_default=True),
    ] = Path("config.yaml"),
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    # Windows gives a redirected stream the ANSI code page, which cannot encode most
    # chat text (`peakreel chat replay > out.txt` would crash). Always emit UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    _state.config_path = config


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _load(*, log_to_file: bool = False) -> Config:
    """Load the config or exit with a readable error, then set up logging."""
    try:
        config = load_config(_state.config_path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    configure_logging(
        level=config.logging.level,
        file=config.log_file if log_to_file else None,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    return config


def _print_check(result: CheckResult) -> None:
    label = (
        typer.style("PASS", fg=typer.colors.GREEN)
        if result.ok
        else typer.style("FAIL", fg=typer.colors.RED)
    )
    typer.echo(f"[{label}] {result.name:<11} {result.detail}")


@app.command()
def doctor(
    offline: Annotated[
        bool, typer.Option("--offline", help="Skip checks that need the internet.")
    ] = False,
) -> None:
    """Check Python, clock, ffmpeg, ffprobe, streamlink, data directory and database."""
    try:
        config = load_config(_state.config_path)
    except ConfigError as exc:
        _print_check(CheckResult("config", False, str(exc)))
        raise typer.Exit(code=1) from exc
    _print_check(CheckResult("config", True, str(_state.config_path.resolve())))
    results = run_checks(config, network=not offline)
    for result in results:
        _print_check(result)
    failed = [r for r in results if not r.ok]
    if failed:
        typer.secho(f"\n{len(failed)} check(s) failed.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("\nAll checks passed.", fg=typer.colors.GREEN)


@db_app.command("init")
def db_init() -> None:
    """Create the SQLite database or upgrade it to the latest schema."""
    config = _load()
    version = init_db(config.db_path)
    typer.echo(f"{config.db_path}: schema v{version}")


chat_app = typer.Typer(help="Twitch chat ingest, replay and liveness checks.", no_args_is_help=True)
app.add_typer(chat_app, name="chat")


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _cancel_on_ctrl_break(task: asyncio.Task[None] | None) -> None:
    """Treat Ctrl+Break (``SIGBREAK``, Windows only) like Ctrl+C: cancel ``task``.

    ``asyncio.run`` already turns Ctrl+C into a clean cancellation. Ctrl+Break is the
    signal a parent process can send to a child started in its own process group
    (``CTRL_BREAK_EVENT``), so handling it gives supervisors a graceful stop too.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None or task is None:
        return
    loop = asyncio.get_running_loop()
    signal.signal(sigbreak, lambda _signum, _frame: loop.call_soon_threadsafe(task.cancel))


@chat_app.command("record")
def chat_record(
    channels: Annotated[
        list[str] | None,
        typer.Option(
            "--channel",
            "-C",
            help="Channel to record (repeatable). Defaults to `channels` in the config.",
        ),
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option("--duration", "-d", help="Stop after this many seconds.", min=1),
    ] = None,
) -> None:
    """Record live chat to data/chat/<channel>/<YYYY-MM-DD>.jsonl until Ctrl+C."""
    config = _load(log_to_file=True)
    names = (
        [n.strip().lstrip("#").lower() for n in channels]
        if channels
        else [c.name for c in config.channels]
    )
    if not names:
        raise _fail("no channels: pass --channel or add `channels` to config.yaml")
    try:
        blocked = load_blocklist(config.blocklist_path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    refused = sorted(set(names) & blocked)
    if refused:
        raise _fail(f"refusing to record blocklisted channel(s): {', '.join(refused)}")

    log = get_logger("peakreel.cli")
    source = TwitchChat(names, config.chat)
    chat_filter = ChatFilter.from_config(config.chat)

    async def main() -> None:
        _cancel_on_ctrl_break(asyncio.current_task())
        with ChatLogWriter(config.chat_log_dir) as writer:
            log.info("chat recording", channels=list(source.channels), out=str(config.chat_log_dir))
            try:
                async with asyncio.timeout(duration):
                    await record_chat(source, writer, chat_filter)
            except TimeoutError:
                log.info("chat recording finished", duration_s=duration, written=writer.written)

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        # The recorder was cancelled (Ctrl+C or Ctrl+Break). It wrote its `stopped`
        # records and closed every file before this point.
        log.info("chat recording stopped by user")


@chat_app.command("replay")
def chat_replay(
    paths: Annotated[list[Path], typer.Argument(help="Log files or directories.")],
    include_filtered: Annotated[
        bool, typer.Option("--include-filtered", help="Also print bot and command messages.")
    ] = False,
    reclassify: Annotated[
        bool,
        typer.Option("--reclassify", help="Re-run the current filters instead of stored flags."),
    ] = False,
) -> None:
    """Print recorded messages in deterministic order (the offline detector input)."""
    config = _load()
    chat_filter = ChatFilter.from_config(config.chat) if reclassify else None
    try:
        for message in read_log(paths, reclassify=chat_filter):
            if message.filtered is not None and not include_filtered:
                continue
            flag = f" [{message.filtered}]" if message.filtered else ""
            kind = " (notice)" if message.type == "usernotice" else ""
            typer.echo(
                f"{_utc(message.ts)} #{message.channel} <{message.user}>{kind}{flag} {message.text}"
            )
    except FileNotFoundError as exc:
        raise _fail(str(exc)) from exc


@chat_app.command("gaps")
def chat_gaps(
    paths: Annotated[list[Path], typer.Argument(help="Log files or directories.")],
    threshold: Annotated[
        float, typer.Option("--threshold", "-t", help="Tolerated downtime in seconds.", min=0)
    ] = 5.0,
) -> None:
    """Report ingest downtime per channel. Exits 1 if any exceeds the threshold."""
    _load()
    try:
        reports = analyze_gaps(paths)
    except FileNotFoundError as exc:
        raise _fail(str(exc)) from exc
    if not reports:
        raise _fail("no chat logs found")
    total_failures = 0
    for report in reports:
        failures = report.failures(threshold)
        total_failures += len(failures)
        status = (
            typer.style("OK  ", fg=typer.colors.GREEN)
            if not failures
            else typer.style("FAIL", fg=typer.colors.RED)
        )
        gap_at = f" at {_utc(report.max_message_gap_at)}" if report.max_message_gap_at else ""
        typer.echo(
            f"[{status}] #{report.channel}: {report.recorded_s / 60:.1f} min, "
            f"{report.messages} msgs, {report.sessions} session(s), "
            f"{len(report.downtimes)} downtime(s), "
            f"max message gap {report.max_message_gap_s:.1f}s{gap_at}"
        )
        for downtime in report.downtimes:
            length = "still down" if downtime.duration_s is None else f"{downtime.duration_s:.1f}s"
            tag = " (intentional)" if downtime.intentional else ""
            typer.echo(f"         {_utc(downtime.start_ts)} {length}{tag}: {downtime.reason}")
    if total_failures:
        typer.secho(
            f"\n{total_failures} downtime(s) over {threshold}s.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)
    typer.secho(f"\nNo unintentional downtime over {threshold}s.", fg=typer.colors.GREEN)
