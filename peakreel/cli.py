"""The ``peakreel`` command line interface.

Run ``peakreel --help`` for the command list. Every command reads ``config.yaml``
from the current directory unless ``--config`` points elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from peakreel import __version__
from peakreel.config import Config, ConfigError, load_config
from peakreel.db import init_db
from peakreel.doctor import CheckResult, run_checks
from peakreel.log import configure_logging

__all__ = ["app"]

app = typer.Typer(
    name="peakreel",
    help="Detect Twitch chat spikes and turn them into captioned vertical clips.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
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
def doctor() -> None:
    """Check Python, ffmpeg, ffprobe, streamlink, the data directory and the database."""
    try:
        config = load_config(_state.config_path)
    except ConfigError as exc:
        _print_check(CheckResult("config", False, str(exc)))
        raise typer.Exit(code=1) from exc
    _print_check(CheckResult("config", True, str(_state.config_path.resolve())))
    results = run_checks(config)
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
