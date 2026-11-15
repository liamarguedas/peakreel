from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from peakreel.config import Config, load_config

WriteConfig = Callable[..., Path]


@pytest.fixture
def write_config(tmp_path: Path) -> WriteConfig:
    """Write a config.yaml (and optional local overlay) plus an empty blocklist."""

    def _write(data: dict[str, Any] | None = None, local: dict[str, Any] | None = None) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data or {}), encoding="utf-8")
        if local is not None:
            (tmp_path / "config.local.yaml").write_text(yaml.safe_dump(local), encoding="utf-8")
        blocklist = tmp_path / "blocklist.yaml"
        if not blocklist.exists():
            blocklist.write_text("channels: []\n", encoding="utf-8")
        return path

    return _write


@pytest.fixture
def config(write_config: WriteConfig) -> Config:
    """A default configuration rooted in a temporary directory."""
    return load_config(write_config())
