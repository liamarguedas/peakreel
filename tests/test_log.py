from __future__ import annotations

import json
import logging
from pathlib import Path

from peakreel.log import configure_logging, get_logger


def test_logging_writes_utf8_plain_tracebacks(tmp_path: Path) -> None:
    log_file = tmp_path / "log.jsonl"
    configure_logging(level="INFO", file=log_file)
    try:
        raise ValueError("boom ünï 😀")
    except ValueError:
        get_logger("t").exception("failed", text="ünï 😀")
    logging.shutdown()
    record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["text"] == "ünï 😀"
    assert "ValueError: boom ünï 😀" in record["exception"]
    configure_logging(level="INFO")
