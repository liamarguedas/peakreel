"""Structured logging setup.

Logs go to two places: a human-readable console stream on stderr, and a rotating
JSON-lines file that is easy to grep and to load into analysis tools.
Both are fed by the standard :mod:`logging` module through :mod:`structlog`, so
third-party libraries that use ``logging`` end up in the same outputs.

Examples
--------
>>> configure_logging(level="INFO")
>>> log = get_logger("peakreel.example")
>>> log.info("started", channel="somestreamer")  # doctest: +SKIP
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

__all__ = ["configure_logging", "get_logger"]

_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]


def configure_logging(
    *,
    level: str = "INFO",
    file: Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure console and file logging for the process.

    Safe to call more than once: handlers installed by a previous call are replaced.

    Parameters
    ----------
    level : str, default "INFO"
        Minimum level name (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``).
    file : pathlib.Path or None, default None
        JSON-lines log file. Parent directories are created. ``None`` disables file
        logging (used by short-lived commands and tests).
    max_bytes : int, default 10 MiB
        Size at which the file rotates.
    backup_count : int, default 5
        Number of rotated files kept.

    Notes
    -----
    Timestamps are ISO 8601 in UTC, matching the UTC epoch seconds used in data files.
    Side effect: ``sys.stderr`` is reconfigured to UTF-8.
    """
    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Windows defaults a redirected stderr to the ANSI code page, which escapes any
    # non-ASCII chat text. Log output is always UTF-8.
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED_PROCESSORS,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(
                    colors=sys.stderr.isatty(),
                    # Plain tracebacks: no boxes, and no local variables, which can hold
                    # chat content or credentials.
                    exception_formatter=structlog.dev.plain_traceback,
                ),
            ],
        )
    )
    handlers.append(console)

    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=_SHARED_PROCESSORS,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        handlers.append(file_handler)

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    # The websockets library logs every frame at DEBUG; keep it out of our logs.
    logging.getLogger("websockets").setLevel(max(logging.INFO, root.level))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger.

    Parameters
    ----------
    name : str
        Logger name, conventionally ``__name__``.

    Returns
    -------
    structlog.stdlib.BoundLogger
        Accepts an event string plus arbitrary key-value context.
    """
    return structlog.stdlib.get_logger(name)
