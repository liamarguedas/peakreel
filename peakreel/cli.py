"""The ``peakreel`` command line interface.

Run ``peakreel --help`` for the command list. Every command reads ``config.yaml``
from the current directory unless ``--config`` points elsewhere.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from peakreel import __version__
from peakreel.blocklist import load_blocklist
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
        asyncio.run(main())
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
) -> None:
    """Record chat and video, detect spikes and cut clips into the outbox until stopped."""
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
        raise _fail(f"refusing to run blocklisted channel(s): {', '.join(refused)}")
    ffmpeg = resolve_tool(config.tools.ffmpeg, config.root)
    if ffmpeg is None:
        raise _fail("ffmpeg not found; run `peakreel doctor`")
    stop_file = _run_stop_file(config)
    stop_file.unlink(missing_ok=True)

    async def main() -> None:
        pipeline = LivePipeline(config, names, ffmpeg)
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
        asyncio.run(main())
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
