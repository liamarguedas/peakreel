"""The ``peakreel`` command line interface.

Run ``peakreel --help`` for the command list. Every command reads ``config.yaml``
from the current directory unless ``--config`` points elsewhere.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError

from peakreel import __version__
from peakreel.blocklist import load_blocklist
from peakreel.captions.whisper import Captioner, load_model
from peakreel.chat.base import ChatEvent
from peakreel.chat.gaps import analyze_gaps
from peakreel.chat.ingest import record_chat
from peakreel.chat.replay import read_events, read_log
from peakreel.chat.twitch import TwitchChat
from peakreel.chat.writer import ChatLogWriter
from peakreel.config import Config, ConfigError, DetectParams, load_config, resolve_tool
from peakreel.db import init_db
from peakreel.detect.filters import ChatFilter
from peakreel.detect.labels import (
    VOD_LEAD_S,
    LabelError,
    evaluate,
    format_results,
    load_grid,
    read_labels,
    write_label_sheet,
)
from peakreel.detect.spikes import Spike, detect_spikes
from peakreel.doctor import CheckResult, run_checks
from peakreel.locks import ChannelLocks, LockHeld
from peakreel.log import configure_logging, get_logger
from peakreel.pipeline import LivePipeline
from peakreel.record.buffer import (
    WindowUnavailable,
    channel_segments,
    cut_window,
    disk_usage,
    get_window,
    prune_channel,
)
from peakreel.record.recorder import ChannelRecorder
from peakreel.render.vertical import RenderError, font_family, render_clip, resolve_font
from peakreel.twitch.helix import (
    Credentials,
    HelixClient,
    HelixError,
    VodLocator,
    delete_credentials,
    load_credentials,
    save_credentials,
    vod_url,
)

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
        console=not os.environ.get(_BACKGROUND_ENV),
    )
    return config


_BACKGROUND_ENV = "PEAKREEL_BACKGROUND"


def _detach(config: Config, what: str) -> None:
    """Start this same command again in the background, without a console, and return.

    The child logs only to the rotating log file (``logging.file``); uncaught
    errors land there too. It is confirmed alive after a few seconds, so a start-up
    failure (a lock held, a port in use) is reported here rather than lost.
    """
    import subprocess

    args = [sys.executable, "-m", "peakreel", *[a for a in sys.argv[1:] if a != "--detach"]]
    env = {**os.environ, _BACKGROUND_ENV: "1"}
    options: dict[str, Any] = {"start_new_session": True}
    if sys.platform == "win32":
        options = {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    child = subprocess.Popen(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )
    try:
        code = child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        typer.secho(f"{what} started in the background (pid {child.pid})", fg=typer.colors.GREEN)
        typer.echo(f"logs: {config.log_file}")
        return
    raise _fail(f"{what} exited at start (code {code}); see {config.log_file}")


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


def _chat_stop_file(config: Config) -> Path:
    return config.data_dir / "run" / "chat-record.stop"


async def _cancel_on_stop_file(path: Path, task: asyncio.Task[None] | None) -> None:
    """Cancel ``task`` once ``path`` appears, then remove it.

    This is the graceful stop for a recorder running detached (no console), where
    neither Ctrl+C nor Ctrl+Break can reach it. See ``peakreel chat stop``.
    """
    while not await asyncio.to_thread(path.exists):  # noqa: ASYNC110 (no event for a file)
        await asyncio.sleep(1.0)
    await asyncio.to_thread(path.unlink, missing_ok=True)
    get_logger("peakreel.cli").info("stop requested", stop_file=str(path))
    if task is not None:
        task.cancel()


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

    stop_file = _chat_stop_file(config)
    stop_file.unlink(missing_ok=True)  # a stale request must not stop a fresh recorder

    async def main() -> None:
        task = asyncio.current_task()
        _cancel_on_ctrl_break(task)
        watcher = asyncio.create_task(_cancel_on_stop_file(stop_file, task))
        with ChatLogWriter(config.chat_log_dir) as writer:
            log.info("chat recording", channels=list(source.channels), out=str(config.chat_log_dir))
            try:
                async with asyncio.timeout(duration):
                    await record_chat(source, writer, chat_filter)
            except TimeoutError:
                log.info("chat recording finished", duration_s=duration, written=writer.written)
            finally:
                watcher.cancel()

    try:
        with ChannelLocks(config.run_dir / "locks", source.channels):
            asyncio.run(main())
    except LockHeld as exc:
        raise _fail(str(exc)) from exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        # The recorder was cancelled (Ctrl+C or Ctrl+Break). It wrote its `stopped`
        # records and closed every file before this point.
        log.info("chat recording stopped by user")


@chat_app.command("stop")
def chat_stop() -> None:
    """Ask a running `chat record` (including a detached one) to stop gracefully."""
    config = _load()
    path = _chat_stop_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    typer.echo(f"stop requested ({path}); the recorder exits within about 1 s")


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


detect_app = typer.Typer(help="Spike detection over recorded chat.", no_args_is_help=True)
app.add_typer(detect_app, name="detect")
twitch_app = typer.Typer(help="Twitch API credentials.", no_args_is_help=True)
app.add_typer(twitch_app, name="twitch")

_SetOption = Annotated[
    list[str] | None,
    typer.Option("--set", "-s", help="Override a detector parameter, e.g. --set k=3 (repeatable)."),
]


def _parse_overrides(items: list[str] | None) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise _fail(f"--set expects name=value, got {item!r}")
        overrides[name.strip()] = yaml.safe_load(value)
    return overrides


def _params(config: Config, items: list[str] | None, *, candidate: bool = False) -> DetectParams:
    base: dict[str, object] = dict(config.detect.candidate) if candidate else {}
    try:
        return config.detect.params({**base, **_parse_overrides(items)})
    except ValidationError as exc:
        raise _fail(f"invalid detector parameters: {exc}") from exc


def _load_events(paths: list[Path]) -> list[ChatEvent]:
    try:
        return list(read_events(paths))
    except FileNotFoundError as exc:
        raise _fail(str(exc)) from exc


@detect_app.command("replay")
def detect_replay(
    paths: Annotated[list[Path], typer.Argument(help="Chat log files or directories.")],
    overrides: _SetOption = None,
    candidate: Annotated[
        bool, typer.Option("--candidate", help="Start from the permissive candidate set.")
    ] = False,
    show_suppressed: Annotated[
        bool, typer.Option("--show-suppressed", help="Also list raid, sub train and spam events.")
    ] = False,
) -> None:
    """Run the detector offline and print events with their statistics."""
    config = _load()
    params = _params(config, overrides, candidate=candidate)
    result = detect_spikes(_load_events(paths), params)
    shown = 0
    for spike in result.spikes:
        if spike.suppressed_by is not None and not show_suppressed:
            continue
        shown += 1
        flag = f" [{spike.suppressed_by}]" if spike.suppressed_by else ""
        top = " | ".join(f"{text} x{count}" for text, count in spike.top_messages[:3])
        typer.echo(
            f"{_utc(spike.start_ts)[:-5]}Z #{spike.channel} {spike.duration_s:>4.0f}s "
            f"peak {spike.peak_count} (base {spike.baseline_median:g}+/-{spike.baseline_mad:g}) "
            f"uniq {spike.unique_chatters} score {spike.score:g}{flag} :: {top}"
        )
    suppressed = sum(1 for s in result.spikes if s.suppressed_by)
    typer.echo(
        f"\n{len(result.spikes) - suppressed} event(s), {suppressed} suppressed, "
        f"over {result.active_hours:.1f} chat-active h ({result.valid_hours:.1f} h connected); "
        f"shown {shown}"
    )


def _vod_locator(config: Config) -> Callable[[Spike], tuple[str, str]]:
    credentials = load_credentials()
    if credentials is None:
        return lambda _spike: ("", "no Twitch credentials: run `peakreel twitch login`")
    locator = VodLocator(HelixClient(credentials), config.data_dir / "twitch")

    def locate(spike: Spike) -> tuple[str, str]:
        # Chat timestamps are the local clock; Twitch VODs use Twitch's clock.
        twitch_ts = spike.start_ts - (spike.receive_delay_s or 0.0)
        try:
            vod, reason = locator.locate(spike.channel, twitch_ts)
        except HelixError as exc:
            return "", f"VOD lookup failed: {exc}"
        if vod is None:
            return "", reason
        return vod_url(vod, twitch_ts - vod.created_at - VOD_LEAD_S), ""

    return locate


@detect_app.command("label-sheet")
def detect_label_sheet(
    paths: Annotated[list[Path], typer.Argument(help="Chat log files or directories.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Label sheet CSV.")] = Path(
        "data/labels/labels.csv"
    ),
    overrides: _SetOption = None,
) -> None:
    """Write (or extend) the labeling sheet from the candidate parameter set."""
    config = _load()
    params = _params(config, overrides, candidate=True)
    result = detect_spikes(_load_events(paths), params)
    spikes = [s for s in result.spikes if s.suppressed_by is None]
    try:
        added = write_label_sheet(out, spikes, _vod_locator(config))
        rows = read_labels(out)
    except LabelError as exc:
        raise _fail(str(exc)) from exc
    todo = sum(1 for r in rows if not r.label)
    typer.echo(f"{out}: {added} new row(s), {len(rows)} total, {todo} still to label")


@detect_app.command("evaluate")
def detect_evaluate(
    labels: Annotated[Path, typer.Argument(help="Labeled CSV from `detect label-sheet`.")],
    paths: Annotated[list[Path], typer.Argument(help="Chat log files or directories.")],
    grid: Annotated[
        Path | None,
        typer.Option("--grid", "-g", help="YAML with `sets: [{name: ..., <param>: ...}]`."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Also write the table to this Markdown file.")
    ] = None,
) -> None:
    """Score parameter sets against the labels: precision, recall and events per hour."""
    config = _load()
    try:
        rows = read_labels(labels)
    except LabelError as exc:
        raise _fail(str(exc)) from exc
    if not rows:
        raise _fail(f"no labels in {labels}")
    sets: list[tuple[str, DetectParams]] = [
        ("default", config.detect.params()),
        ("candidate", config.detect.candidate_params()),
    ]
    if grid is not None:
        try:
            sets.extend(load_grid(grid, config.detect))
        except LabelError as exc:
            raise _fail(str(exc)) from exc
    events = _load_events(paths)
    scores = [evaluate(name, detect_spikes(events, params), rows) for name, params in sets]
    table = format_results(scores)
    typer.echo(table)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(table + "\n", encoding="utf-8")
        typer.echo(f"\nwritten to {out}")


@twitch_app.command("login")
def twitch_login(
    client_id: Annotated[str, typer.Option(prompt="Twitch client ID")],
    client_secret: Annotated[str, typer.Option(prompt="Twitch client secret", hide_input=True)],
) -> None:
    """Verify and store dev.twitch.tv app credentials in the OS credential store."""
    _load()
    credentials = Credentials(client_id.strip(), client_secret.strip())
    try:
        HelixClient(credentials).token()
    except HelixError as exc:
        raise _fail(f"Twitch rejected these credentials: {exc}") from exc
    save_credentials(credentials)
    typer.secho(
        "Credentials verified and stored in the OS credential store.", fg=typer.colors.GREEN
    )


@twitch_app.command("status")
def twitch_status() -> None:
    """Show whether credentials are stored and still accepted by Twitch."""
    _load()
    credentials = load_credentials()
    if credentials is None:
        raise _fail("no Twitch credentials stored: run `peakreel twitch login`")
    try:
        HelixClient(credentials).token()
    except HelixError as exc:
        raise _fail(f"stored credentials for client {credentials.client_id} fail: {exc}") from exc
    typer.secho(f"OK: client {credentials.client_id}", fg=typer.colors.GREEN)


@twitch_app.command("logout")
def twitch_logout() -> None:
    """Remove stored Twitch credentials."""
    delete_credentials()
    typer.echo("Twitch credentials removed.")


record_app = typer.Typer(help="Live video recording into a rolling buffer.", no_args_is_help=True)
app.add_typer(record_app, name="record")


def _record_stop_file(config: Config) -> Path:
    return config.data_dir / "run" / "record.stop"


def _parse_time(value: str, now: float) -> float:
    """Parse ``2026-11-17T08:00:00Z``, epoch seconds, or ``now-300`` (seconds ago)."""
    text = value.strip()
    if text.startswith("now"):
        rest = text[3:].strip()
        return now + (float(rest) if rest else 0.0)
    try:
        return float(text)
    except ValueError:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@record_app.command("start")
def record_start(
    channels: Annotated[
        list[str] | None,
        typer.Option("--channel", "-C", help="Channel to record (repeatable). Defaults to config."),
    ] = None,
) -> None:
    """Record live channels into data/buffer until stopped (Ctrl+C or `record stop`)."""
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
    stop = threading.Event()
    stop_file = _record_stop_file(config)
    stop_file.unlink(missing_ok=True)

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, request_stop)

    keep_s = config.record.buffer_minutes * 60
    threads = [
        threading.Thread(
            target=ChannelRecorder(name, config.record, config.buffer_dir).run,
            args=(stop,),
            name=f"record-{name}",
            daemon=True,
        )
        for name in dict.fromkeys(names)
    ]
    for thread in threads:
        thread.start()
    log.info("video recording", channels=names, buffer=str(config.buffer_dir), keep_s=keep_s)
    next_prune = 0.0
    while not stop.is_set():
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            log.info("stop requested", stop_file=str(stop_file))
            break
        if time.monotonic() >= next_prune:
            next_prune = time.monotonic() + config.record.prune_interval_s
            freed = 0
            for name in names:
                freed += prune_channel(config.buffer_dir / name, keep_s)[1]
            log.info(
                "buffer pruned",
                freed_mb=round(freed / 1e6, 1),
                usage_mb={k: round(v / 1e6, 1) for k, v in disk_usage(config.buffer_dir).items()},
            )
        stop.wait(1.0)
    stop.set()
    for thread in threads:
        thread.join(timeout=15)
    log.info("video recording stopped")


@record_app.command("stop")
def record_stop() -> None:
    """Ask a running `record start` (including a detached one) to stop gracefully."""
    config = _load()
    path = _record_stop_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    typer.echo(f"stop requested ({path})")


@record_app.command("status")
def record_status() -> None:
    """Show what each channel's buffer holds."""
    config = _load()
    usage = disk_usage(config.buffer_dir)
    if not usage:
        raise _fail(f"buffer is empty ({config.buffer_dir})")
    for channel, size in usage.items():
        segments = channel_segments(config.buffer_dir / channel)
        if not segments:
            typer.echo(f"#{channel}: no segments ({size / 1e6:.0f} MB)")
            continue
        span = segments[-1].end - segments[0].pdt
        typer.echo(
            f"#{channel}: {_utc(segments[0].pdt)} to {_utc(segments[-1].end)} "
            f"({span / 60:.1f} min, {len(segments)} segments, {size / 1e6:.0f} MB)"
        )


@record_app.command("cut")
def record_cut(
    channel: Annotated[str, typer.Argument(help="Channel login.")],
    start: Annotated[str, typer.Argument(help="ISO UTC, epoch seconds, or now-<seconds>.")],
    end: Annotated[str, typer.Argument(help="ISO UTC, epoch seconds, or now-<seconds>.")],
    out: Annotated[Path, typer.Argument(help="Output .mp4.")],
) -> None:
    """Cut a time range (broadcast clock) out of the buffer into an MP4."""
    config = _load()
    now = time.time()
    try:
        window = get_window(
            config.buffer_dir, channel.lower(), _parse_time(start, now), _parse_time(end, now)
        )
    except (ValueError, WindowUnavailable) as exc:
        raise _fail(str(exc)) from exc
    for gap_start, gap_end in window.gaps:
        typer.secho(
            f"warning: no video {_utc(gap_start)} to {_utc(gap_end)} ({gap_end - gap_start:.1f}s)",
            fg=typer.colors.YELLOW,
            err=True,
        )
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    try:
        cut_window(window, out, ffmpeg)
    except subprocess.CalledProcessError as exc:
        raise _fail(f"ffmpeg failed: {exc.stderr}") from exc
    typer.echo(
        f"{out}: {_utc(window.start)} +{window.duration_s:.1f}s "
        f"from {len(window.segments)} segments"
    )


run_app = typer.Typer(
    help="Live pipeline: chat, detection, video and clipping.", no_args_is_help=True
)
app.add_typer(run_app, name="run")


def _run_stop_file(config: Config) -> Path:
    return config.data_dir / "run" / "pipeline.stop"


@run_app.command("start")
def run_start(
    channels: Annotated[
        list[str] | None,
        typer.Option("--channel", "-C", help="Channel to run (repeatable). Defaults to config."),
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option("--duration", "-d", help="Stop after this many seconds.", min=1),
    ] = None,
    detach: Annotated[
        bool, typer.Option("--detach", help="Run in the background (logs go to the log file).")
    ] = False,
) -> None:
    """Supervise chat, detection, video and clipping for the channels until stopped."""
    config = _load(log_to_file=True)
    enabled = [c.name for c in config.channels if c.enabled]
    names = [n.strip().lstrip("#").lower() for n in channels] if channels else enabled
    if not names and not config.discover.enabled:
        raise _fail(
            "no channels: add enabled `channels` to config.yaml, pass --channel, "
            "or enable `discover`"
        )
    if config.supervisor.require_allowlist:
        unlisted = sorted(set(names) - set(enabled))
        if unlisted:
            raise _fail(
                f"not on the allowlist (enabled `channels` in config): {', '.join(unlisted)}"
            )
    try:
        blocked = load_blocklist(config.blocklist_path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    refused = sorted(set(names) & blocked)
    if refused:
        raise _fail(f"refusing to run blocklisted channel(s): {', '.join(refused)}")
    kick_channels = [n for n in names if n.startswith("kick-")]
    wants_kick = kick_channels or (config.discover.enabled and "kick" in config.discover.platforms)
    if wants_kick:
        from peakreel.kick.api import load_credentials as load_kick

        if load_kick() is None:
            raise _fail("Kick channels need the Kick app: run `peakreel kick setup` (docs/kick.md)")
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    if detach:
        _detach(config, "pipeline")
        return
    stop_file = _run_stop_file(config)
    stop_file.unlink(missing_ok=True)

    async def main() -> None:
        pipeline = LivePipeline(config, names, ffmpeg, locks=locks)
        _cancel_on_ctrl_break(asyncio.current_task())

        async def stop_on_file() -> None:
            while not await asyncio.to_thread(stop_file.exists):  # noqa: ASYNC110 (no event for a file)
                await asyncio.sleep(1.0)
            await asyncio.to_thread(stop_file.unlink, missing_ok=True)
            get_logger("peakreel.cli").info("stop requested", stop_file=str(stop_file))
            pipeline.stop_requested.set()

        async def stop_after(seconds: float) -> None:
            await asyncio.sleep(seconds)
            pipeline.stop_requested.set()

        helpers = [asyncio.create_task(stop_on_file())]
        if duration is not None:
            helpers.append(asyncio.create_task(stop_after(duration)))
        try:
            await pipeline.run()
        finally:
            for task in helpers:
                task.cancel()

    try:
        with ChannelLocks(config.run_dir / "locks", names) as locks:
            asyncio.run(main())
    except LockHeld as exc:
        raise _fail(str(exc)) from exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        get_logger("peakreel.cli").info("live pipeline stopped by user")


@run_app.command("stop")
def run_stop() -> None:
    """Ask a running `run start` (including a detached one) to stop gracefully."""
    config = _load()
    path = _run_stop_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    typer.echo(f"stop requested ({path}); clips in flight are finished first")


@app.command("render")
def render_command(
    paths: Annotated[list[Path], typer.Argument(help="Clip files or directories of clips.")],
    channel: Annotated[
        str | None,
        typer.Option("--channel", "-C", help="Channel, for clips without a sidecar."),
    ] = None,
    captions: Annotated[
        bool | None,
        typer.Option("--captions/--no-captions", help="Burn in captions (default: config)."),
    ] = None,
) -> None:
    """Render clips as 1080x1920 vertical videos with a credit overlay and thumbnail."""
    config = _load()
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    clips: list[Path] = []
    for path in paths:
        candidates = sorted(path.rglob("*.mp4")) if path.is_dir() else [path]
        clips += [c for c in candidates if not c.name.endswith((".vertical.mp4", ".part.mp4"))]
    if not clips:
        raise _fail("no clips found")
    use_captions = config.captions.enabled if captions is None else captions
    captioner = _cli_captioner(config, ffmpeg) if use_captions else None
    failed = 0
    for clip in clips:
        caption = captioner.caption_sync(clip) if captioner else None
        if caption is not None and caption.status != "ok":
            typer.secho(
                f"     captions {caption.status}: {caption.error or clip.name}",
                fg=typer.colors.YELLOW,
            )
        try:
            result = render_clip(config, clip, ffmpeg, channel=channel, captions=caption)
        except RenderError as exc:
            failed += 1
            typer.secho(f"FAIL {clip}: {exc}", fg=typer.colors.RED, err=True)
            continue
        typer.echo(f"ok   {result.vertical} ({result.layout}) + {result.thumbnail.name}")
    if captioner is not None:
        captioner.close()
    if failed:
        raise typer.Exit(code=1)


captions_app = typer.Typer(help="Speech captions (faster-whisper).", no_args_is_help=True)
app.add_typer(captions_app, name="captions")


def _cli_captioner(config: Config, ffmpeg: Path) -> Captioner:
    try:
        family = font_family(resolve_font(config.render.font_file), config.render.font_name)
    except RenderError as exc:
        raise _fail(str(exc)) from exc
    return Captioner(
        config.captions,
        config.output,
        config.resolve(config.tools.whisper_model_dir),
        ffmpeg,
        family,
    )


@captions_app.command("download")
def captions_download() -> None:
    """Download the configured Whisper model once into tools.whisper_model_dir."""
    config = _load()
    model_dir = config.resolve(config.tools.whisper_model_dir)
    typer.echo(f"loading {config.captions.model!r} into {model_dir} (downloads on first use)...")
    started = time.monotonic()
    try:
        load_model(config.captions, model_dir, allow_download=True)
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    typer.secho(f"model ready in {time.monotonic() - started:.0f}s", fg=typer.colors.GREEN)


@captions_app.command("make")
def captions_make(
    clip: Annotated[Path, typer.Argument(help="Clip to transcribe.")],
) -> None:
    """Transcribe one clip, write <name>.ass, and print the word timings."""
    config = _load()
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    captioner = _cli_captioner(config, ffmpeg)
    try:
        words, language = captioner.transcribe(clip)
        result = captioner.caption_sync(clip)
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    finally:
        captioner.close()
    for word in words:
        typer.echo(f"{word.start:7.2f} {word.end:7.2f}  {word.text.strip()}")
    typer.echo(f"\n{result.status}: language={language} words={len(words)} {result.ass_path or ''}")


safety_app = typer.Typer(
    help="Safety checks and duplicate detection for clips.", no_args_is_help=True
)
app.add_typer(safety_app, name="safety")


@safety_app.command("download")
def safety_download() -> None:
    """Download the blocked-word lists for safety.languages (LDNOOBW, CC BY 4.0)."""
    from peakreel.safety.words import LDNOOBW_COMMIT, download_word_lists

    config = _load()
    try:
        counts = download_word_lists(config.word_lists_dir, config.safety.languages)
    except (OSError, ValueError) as exc:
        raise _fail(f"download failed: {exc}") from exc
    for language, count in counts.items():
        typer.echo(f"  {language}: {count} terms")
    typer.secho(
        f"word lists ready in {config.word_lists_dir} (LDNOOBW {LDNOOBW_COMMIT[:7]})",
        fg=typer.colors.GREEN,
    )


@safety_app.command("scan")
def safety_scan(
    clips: Annotated[list[Path], typer.Argument(help="Clips to check (horizontal .mp4).")],
    transcribe: Annotated[
        bool, typer.Option(help="Transcribe clips whose sidecar has no transcript.")
    ] = False,
) -> None:
    """Run the safety checks on existing clips and print each score and flag.

    Use it to calibrate ``safety.music_threshold`` and the word lists on your own
    clips. The alerts check needs live chat and is not run here.
    """
    import json

    from peakreel.captions.ass import NO_SPACE_LANGUAGES
    from peakreel.captions.whisper import decode_audio
    from peakreel.safety.checks import music_flag, word_flags
    from peakreel.safety.music import music_score
    from peakreel.safety.words import load_word_list

    config = _load()
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    try:
        words = load_word_list(
            config.word_lists_dir, config.safety.languages, config.safety_words_path
        )
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"{words.size} blocked terms; music threshold {config.safety.music_threshold}")
    captioner = _cli_captioner(config, ffmpeg) if transcribe else None
    try:
        for clip in clips:
            sidecar_path = clip.with_suffix(".json")
            sidecar: dict[str, Any] = (
                json.loads(sidecar_path.read_text(encoding="utf-8"))
                if sidecar_path.exists()
                else {}
            )
            captions: dict[str, Any] = sidecar.get("captions") or {}
            transcript: str | None = captions.get("text")
            if transcript is None and captioner is not None:
                found, language = captioner.transcribe(clip)
                joiner = "" if language in NO_SPACE_LANGUAGES else " "
                transcript = joiner.join(w.text.strip() for w in found)
            spike: dict[str, Any] = sidecar.get("spike") or {}
            chat = [str(m["text"]) for m in spike.get("top_messages", [])]
            score = music_score(decode_audio(clip, ffmpeg))
            flags = word_flags(words, transcript, chat)
            music = music_flag(score, config.safety.music_threshold)
            flags += [music] if music else []
            has_text = "yes" if transcript else "no"
            typer.echo(f"{clip.name}  music={score:.3f}  transcript={has_text}")
            for flag in flags:
                typer.echo(f"    [{flag.kind}] {flag.message}")
    finally:
        if captioner is not None:
            captioner.close()


@safety_app.command("compare")
def safety_compare(
    first: Annotated[Path, typer.Argument(help="A clip.")],
    second: Annotated[Path, typer.Argument(help="Another clip.")],
) -> None:
    """Print the audio match strength of two clips (duplicate detection)."""
    from peakreel.dedupe import fingerprint, load_fingerprint, similarity

    config = _load()
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    try:
        prints = [load_fingerprint(fingerprint(clip, ffmpeg)) for clip in (first, second)]
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    match = similarity(*prints, max_offset_s=config.dedupe.max_offset_s)
    same = match.strength >= config.dedupe.min_strength
    typer.echo(
        f"strength {match.strength:.1f} (near-match {match.fraction:.0%}) at offset "
        f"{match.offset_s:+.2f}s: {'same moment' if same else 'different'} "
        f"(threshold {config.dedupe.min_strength:g})"
    )


publish_app = typer.Typer(
    help="Publication queue: per-platform packages and posts.", no_args_is_help=True
)
app.add_typer(publish_app, name="publish")


@publish_app.command("run")
def publish_run(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and report; change nothing.")
    ] = False,
    watch: Annotated[
        bool, typer.Option(help="Keep running and process publications as they come due.")
    ] = False,
    interval: Annotated[float, typer.Option(help="Seconds between passes with --watch.")] = 30.0,
) -> None:
    """Process due publications, and resolve any interrupted by a crash.

    Approving in the review tool already does this for export mode; run it after
    a crash, for retries, or with --watch for scheduled (private/public) posts.
    """
    from peakreel.db import connect, init_db
    from peakreel.publish.queue import process_due

    config = _load(log_to_file=True)
    init_db(config.db_path)
    while True:
        conn = connect(config.db_path)
        try:
            results = process_due(config, conn, dry_run=dry_run)
        finally:
            conn.close()
        for item in results:
            typer.echo(f"clip {item.clip_id:>5} {item.platform:<9} {item.state:<10} {item.detail}")
        if not watch:
            if not results:
                typer.echo("nothing due")
            return
        time.sleep(interval)


@publish_app.command("status")
def publish_status(
    limit: Annotated[int, typer.Option(help="Most recent publications to show.")] = 30,
) -> None:
    """List recent publications with their state, suggested time and location."""
    from datetime import UTC, datetime

    from peakreel.db import connect, init_db

    config = _load()
    init_db(config.db_path)
    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT p.*, ch.name AS channel FROM publications p JOIN clips c ON c.id = p.clip_id "
            "JOIN channels ch ON ch.id = c.channel_id ORDER BY p.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        typer.echo("no publications yet")
    for row in rows:
        when = (
            datetime.fromtimestamp(row["scheduled_at"], UTC).strftime("%m-%d %H:%M")
            if row["scheduled_at"]
            else "-"
        )
        where = row["error"] or row["url"] or ""
        typer.echo(
            f"clip {row['clip_id']:>5} {row['channel']:<18} {row['platform']:<9} "
            f"{row['state']:<10} {when:<11} {where}"
        )


@publish_app.command("preview")
def publish_preview(
    clip_id: Annotated[int, typer.Argument(help="Clip id (see the review tool).")],
) -> None:
    """Print the generated post text of a clip for every platform, with problems."""
    from peakreel.db import connect, init_db
    from peakreel.publish.queue import posts_preview
    from peakreel.review.store import get_clip, view_of

    config = _load()
    init_db(config.db_path)
    conn = connect(config.db_path)
    try:
        row = get_clip(conn, clip_id)
        if row is None:
            raise _fail(f"no clip {clip_id}")
        preview = posts_preview(config, conn, view_of(row), row)
    finally:
        conn.close()
    for platform, info in preview.items():
        meta = info["meta"]
        typer.secho(f"== {platform} ==", bold=True)
        if meta["title"] is not None:
            typer.echo(f"title: {meta['title']}")
        typer.echo(meta["text"])
        if meta["tags"]:
            typer.echo(f"tags: {', '.join(meta['tags'])}")
        for note in meta["notes"]:
            typer.echo(f"  note: {note}")
        for problem in info["problems"]:
            typer.secho(f"  problem: {problem}", fg=typer.colors.RED)
        typer.echo("")


youtube_app = typer.Typer(help="YouTube channels for publishing (M10).", no_args_is_help=True)
app.add_typer(youtube_app, name="youtube")


def _account_ids(config: Config) -> list[str]:
    return [a.id for a in config.publish.accounts]


@youtube_app.command("setup")
def youtube_setup(
    client_file: Annotated[
        Path, typer.Argument(help="client_secret_*.json of a 'Desktop app' OAuth client.")
    ],
) -> None:
    """Store the Google Cloud OAuth client (once). The file can be deleted afterwards."""
    from peakreel.publish.youtube import OAuthClient, save_client

    _load()
    try:
        client = OAuthClient.from_file(client_file)
    except (OSError, ValueError, KeyError) as exc:
        raise _fail(f"cannot read {client_file}: {exc}") from exc
    save_client(client)
    typer.secho("OAuth client stored in the credential store", fg=typer.colors.GREEN)
    typer.echo("next: peakreel youtube login --account <id> for each posting account")


@youtube_app.command("login")
def youtube_login(
    account: Annotated[str, typer.Option("--account", "-a", help="Posting account id (e.g. en).")],
) -> None:
    """Connect a posting account to its YouTube channel (opens the browser)."""
    from peakreel.publish.publishers import PublishError
    from peakreel.publish.youtube import (
        ChannelToken,
        YouTubeApi,
        authorize,
        load_client,
        save_token,
    )

    config = _load()
    if account not in _account_ids(config):
        raise _fail(f"unknown account {account!r}; configured: {', '.join(_account_ids(config))}")
    client = load_client()
    if client is None:
        raise _fail("no OAuth client: run `peakreel youtube setup client_secret.json` first")
    api = YouTubeApi(client)
    typer.echo("A browser opens: sign in, pick the channel for this account, and allow access.")
    try:
        refresh, access = authorize(api)
        channel_id, title = api.my_channel(access)
    except PublishError as exc:
        raise _fail(str(exc)) from exc
    save_token(ChannelToken(account, refresh, channel_id, title))
    typer.secho(f"account {account}: connected to YouTube channel {title!r}", fg=typer.colors.GREEN)


@youtube_app.command("status")
def youtube_status() -> None:
    """Show which posting accounts are connected, and check each token works."""
    from peakreel.publish.publishers import PublishError
    from peakreel.publish.youtube import YouTubeApi, load_client, load_token

    config = _load()
    client = load_client()
    typer.echo(f"OAuth client: {'stored' if client else 'missing (peakreel youtube setup)'}")
    typer.echo(f"YouTube mode: {config.publish.mode_for('youtube')}")
    api = YouTubeApi(client) if client else None
    for account in _account_ids(config):
        token = load_token(account)
        if token is None:
            typer.echo(f"  {account:<8} not connected (peakreel youtube login --account {account})")
            continue
        state = "token stored"
        if api is not None:
            try:
                api.access_token(token)
                state = "ok"
            except PublishError as exc:
                state = f"FAILED: {exc}"
        typer.echo(f"  {account:<8} {token.channel_title} ({token.channel_id}): {state}")


@youtube_app.command("logout")
def youtube_logout(
    account: Annotated[str, typer.Option("--account", "-a", help="Posting account id.")],
) -> None:
    """Forget a posting account's YouTube authorization."""
    from peakreel.publish.youtube import delete_token

    _load()
    delete_token(account)
    typer.echo(f"account {account}: YouTube authorization removed")


meta_app = typer.Typer(
    help="Instagram and Facebook Pages for publishing (M12).", no_args_is_help=True
)
app.add_typer(meta_app, name="meta")


@meta_app.command("setup")
def meta_setup() -> None:
    """Store the Meta app's id and secret (once; developers.facebook.com, App settings)."""
    from peakreel.publish.meta import MetaApp, save_app

    _load()
    app_id = typer.prompt("Meta app id").strip()
    secret = typer.prompt("Meta app secret", hide_input=True).strip()
    config_id = typer.prompt(
        "Facebook Login for Business configuration id (empty if none)", default=""
    ).strip()
    if not app_id.isdigit() or not secret:
        raise _fail("the app id is a number and the secret must not be empty")
    save_app(MetaApp(app_id, secret, config_id or None))
    typer.secho("Meta app stored in the credential store", fg=typer.colors.GREEN)
    typer.echo("next: peakreel meta login --account <id> for each posting account")


@meta_app.command("login")
def meta_login(
    account: Annotated[str, typer.Option("--account", "-a", help="Posting account id (e.g. en).")],
    page: Annotated[
        str | None, typer.Option(help="Facebook Page name or id for this account.")
    ] = None,
) -> None:
    """Connect a posting account to its Facebook Page and linked Instagram account."""
    from peakreel.publish.meta import LOGIN_PORT, load_app, page_token_from, save_page, sign_in
    from peakreel.publish.publishers import PublishError

    config = _load()
    if account not in _account_ids(config):
        raise _fail(f"unknown account {account!r}; configured: {', '.join(_account_ids(config))}")
    meta = load_app()
    if meta is None:
        raise _fail("no Meta app: run `peakreel meta setup` first")
    typer.echo(
        f"A browser opens: sign in with Facebook and allow access to the Pages.\n"
        f"(The app must list http://localhost:{LOGIN_PORT}/ as a valid OAuth redirect URI.)"
    )
    try:
        _user_token, pages = sign_in(meta)
    except PublishError as exc:
        raise _fail(str(exc)) from exc
    if not pages:
        raise _fail("no Facebook Pages found for this login (did you allow the Pages?)")
    chosen: dict[str, Any] | None = None
    if page is not None:
        wanted = page.casefold()
        chosen = next((p for p in pages if wanted in (p["id"], p["name"].casefold())), None)
        if chosen is None:
            raise _fail(f"no Page {page!r}; found: {', '.join(p['name'] for p in pages)}")
    else:
        for number, item in enumerate(pages, 1):
            instagram: dict[str, Any] = item.get("instagram_business_account") or {}
            linked = instagram.get("username")
            typer.echo(
                f"  {number}. {item['name']}" + (f"  (Instagram @{linked})" if linked else "")
            )
        choice = int(typer.prompt(f"Page for account {account}", type=int))
        if not 1 <= choice <= len(pages):
            raise _fail("no such Page number")
        chosen = pages[choice - 1]
    token = page_token_from(account, chosen)
    save_page(token)
    typer.secho(f"account {account}: Page {token.page_name!r}", fg=typer.colors.GREEN)
    if token.instagram_username:
        typer.secho(f"  Instagram @{token.instagram_username}", fg=typer.colors.GREEN)
    else:
        typer.secho(
            "  no Instagram account linked to this Page: link it in Meta Accounts Center, "
            "then log in again",
            fg=typer.colors.YELLOW,
        )


@meta_app.command("status")
def meta_status() -> None:
    """Show which posting accounts have a Page and an Instagram account connected."""
    from peakreel.publish.meta import load_app, load_page

    config = _load()
    typer.echo(f"Meta app: {'stored' if load_app() else 'missing (peakreel meta setup)'}")
    for platform in ("instagram", "facebook"):
        typer.echo(f"{platform} mode: {config.publish.mode_for(platform)}")
    for account in _account_ids(config):
        token = load_page(account)
        if token is None:
            typer.echo(f"  {account:<8} not connected (peakreel meta login --account {account})")
            continue
        instagram = f"@{token.instagram_username}" if token.instagram_username else "no Instagram"
        typer.echo(f"  {account:<8} Page {token.page_name!r}, {instagram}")


@meta_app.command("logout")
def meta_logout(
    account: Annotated[str, typer.Option("--account", "-a", help="Posting account id.")],
) -> None:
    """Forget a posting account's Page token."""
    from peakreel.publish.meta import delete_page

    _load()
    delete_page(account)
    typer.echo(f"account {account}: Meta authorization removed")


discover_app = typer.Typer(
    help="Top live streamers of a Twitch category (discover: in config).", no_args_is_help=True
)
app.add_typer(discover_app, name="discover")


@discover_app.command("preview")
def discover_preview(
    category: Annotated[
        str | None, typer.Option(help="Category name (default: discover.category).")
    ] = None,
    top: Annotated[int, typer.Option(help="How many live streams to list.")] = 25,
) -> None:
    """List the category's top live streams now, and which ones discovery would record."""
    from peakreel.discover import plan_discovery
    from peakreel.kick.api import KickApi, KickError
    from peakreel.kick.api import load_credentials as load_kick
    from peakreel.twitch.helix import HelixClient, HelixError, StreamInfo, load_credentials

    config = _load()
    settings = config.discover
    streams: list[StreamInfo] = []
    if "twitch" in settings.platforms:
        name = category or settings.category
        credentials = load_credentials()
        if credentials is None:
            raise _fail("no Twitch credentials: run `peakreel twitch login` first")
        client = HelixClient(credentials)
        try:
            game_id = client.game_id(name)
            if game_id is None:
                raise _fail(f"no Twitch category named {name!r} (use the exact name from Twitch)")
            streams += client.top_streams(game_id, settings.languages, 100)
        except HelixError as exc:
            raise _fail(str(exc)) from exc
    if "kick" in settings.platforms:
        kick_credentials = load_kick()
        if kick_credentials is None:
            raise _fail("no Kick app: run `peakreel kick setup` first")
        kick = KickApi(kick_credentials)
        try:
            kick_id = kick.category_id(settings.kick_category)
            if kick_id is None:
                raise _fail(f"no Kick category named {settings.kick_category!r}")
            streams += kick.top_streams(kick_id, settings.languages, 100)
        except KickError as exc:
            raise _fail(str(exc)) from exc
    streams.sort(key=lambda x: x.viewers, reverse=True)
    name = category or settings.category
    try:
        blocked = load_blocklist(config.blocklist_path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    accounts = config.publish.accounts
    if settings.languages is not None:
        languages = set(settings.languages)
    elif any(not a.languages for a in accounts):
        languages = {s.language for s in streams}
    else:
        languages = {code for a in accounts for code in a.languages}
    manual = [c.name for c in config.channels if c.enabled]
    plan = plan_discovery(
        streams, settings=settings, languages=languages, manual=manual, discovered={},
        blocked=blocked, now=time.time(),
    )  # fmt: skip
    picked = {s.login for s in plan.add}
    typer.echo(
        f"{name}: {len(streams)} live; languages {sorted(languages)}, min {settings.min_viewers} "
        f"viewers, {settings.max_channels} channels ({len(manual)} manual)"
    )
    for rank, stream in enumerate(streams[:top], 1):
        if stream.login in picked:
            mark = "record"
        elif stream.login in manual:
            mark = "manual"
        elif stream.login in blocked or stream.login in settings.exclude:
            mark = "blocked"
        elif stream.language not in languages:
            mark = "no account"
        elif stream.viewers < settings.min_viewers:
            mark = "few viewers"
        else:
            mark = "no slot"
        login, language = stream.login, stream.language
        typer.echo(f"{rank:>3}. {login:<22} {language:<3} {stream.viewers:>7} viewers  {mark}")


kick_app = typer.Typer(
    help="Kick: app credentials and chat webhooks (docs/kick.md).", no_args_is_help=True
)
app.add_typer(kick_app, name="kick")


@kick_app.command("setup")
def kick_setup() -> None:
    """Store the Kick developer app (kick.com/settings/developer) and check it works."""
    from peakreel.kick.api import KickApi, KickCredentials, KickError, save_credentials

    _load()
    client_id = typer.prompt("Kick client ID").strip()
    secret = typer.prompt("Kick client secret", hide_input=True).strip()
    credentials = KickCredentials(client_id, secret)
    try:
        KickApi(credentials).token()
    except KickError as exc:
        raise _fail(f"Kick refused these credentials: {exc}") from exc
    save_credentials(credentials)
    typer.secho(
        "Kick app stored in the credential store (token check passed)", fg=typer.colors.GREEN
    )


@kick_app.command("status")
def kick_status() -> None:
    """Check the Kick app, the webhook key, and list the active chat subscriptions."""
    from peakreel.kick.api import KickApi, KickError, load_credentials

    config = _load()
    credentials = load_credentials()
    if credentials is None:
        raise _fail("no Kick app: run `peakreel kick setup`")
    api = KickApi(credentials)
    settings = config.kick
    try:
        api.token()
        api.public_key()
        subscriptions = api.subscriptions()
    except KickError as exc:
        raise _fail(str(exc)) from exc
    typer.echo("Kick app: ok (token and webhook signing key)")
    local = f"http://{settings.webhook_host}:{settings.webhook_port}{settings.webhook_path}"
    typer.echo(f"webhook endpoint (tunnel target): {local}")
    if settings.public_url:
        public = settings.public_url.rstrip("/") + settings.webhook_path
        typer.echo(f"Kick app webhook URL should be: {public}")
    typer.echo(f"active event subscriptions: {len(subscriptions)} (cleared and remade at each run)")


@kick_app.command("logout")
def kick_logout() -> None:
    """Forget the Kick app."""
    from peakreel.kick.api import delete_credentials

    _load()
    delete_credentials()
    typer.echo("Kick app removed")


@app.command("cleanup")
def cleanup_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be deleted; delete nothing.")
    ] = False,
) -> None:
    """Apply the retention rules now (the running pipeline also does this regularly)."""
    from peakreel.cleanup import run_cleanup
    from peakreel.db import connect, init_db

    config = _load(log_to_file=True)
    init_db(config.db_path)
    conn = connect(config.db_path)
    try:
        report = run_cleanup(config, conn, dry_run=dry_run)
    finally:
        conn.close()
    verb = "would delete" if dry_run else "deleted"
    typer.echo(
        f"{verb}: {len(report.purged)} clip(s) ({len(report.emergency)} early, disk low), "
        f"{report.packages} package(s), {report.chat_files} chat log(s); "
        f"{report.freed_mb} MB; {report.free_gb} GB free"
    )
    if report.low_disk:
        typer.secho(
            f"disk is below retention.min_free_gb ({config.retention.min_free_gb} GB)",
            fg=typer.colors.RED,
        )


review_app = typer.Typer(
    help="Serve the review page: preview, approve or reject, edit, export.",
    invoke_without_command=True,
)
app.add_typer(review_app, name="review")


@review_app.callback()
def review_command(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option(help="Interface to bind (default: config).")] = None,
    port: Annotated[int | None, typer.Option(help="Port (default: config).")] = None,
    detach: Annotated[
        bool, typer.Option("--detach", help="Run in the background (logs go to the log file).")
    ] = False,
) -> None:
    """Serve the review page: preview, approve or reject, edit, export.

    It needs a password (``peakreel review password``) once it can be reached
    from other devices: ``--host`` or ``review.host`` not loopback, or
    ``review.public_hosts`` set.
    """
    if ctx.invoked_subcommand is not None:
        return
    import uvicorn

    from peakreel.review.app import create_app
    from peakreel.review.auth import load_credentials

    config = _load(log_to_file=True)
    bind = host or config.review.host
    bind_port = port or config.review.port
    if config.review.login_required(bind) and load_credentials() is None:
        typer.secho(
            "The review page can be reached from other devices (review.host, --host or "
            "review.public_hosts), so it needs a password first: peakreel review password",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    if detach:
        _detach(config, f"review tool on http://{bind}:{bind_port}/")
        return
    login = " (password login)" if config.review.login_required(bind) else ""
    typer.echo(f"Peakreel review on http://{bind}:{bind_port}/{login}  (Ctrl+C to stop)")
    uvicorn.run(create_app(config, bind=bind), host=bind, port=bind_port, log_level="warning")


@review_app.command("password")
def review_password(
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove the password instead of setting one.")
    ] = False,
) -> None:
    """Set the review page password; every device must log in again.

    Only an scrypt hash is stored, in the OS credential store. A running review
    page picks up the change within 10 seconds.
    """
    from peakreel.review.auth import MIN_PASSWORD_LENGTH, clear_password, set_password

    _load()
    if clear:
        clear_password()
        typer.echo("Review password removed; a page reachable from other devices will not start.")
        return
    password = typer.prompt(
        f"New review password (at least {MIN_PASSWORD_LENGTH} characters)",
        hide_input=True,
        confirmation_prompt=True,
    )
    try:
        set_password(password)
    except ValueError as exc:
        typer.secho(f"Not set: {exc}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo("Review password set. Devices that were logged in must log in again.")
