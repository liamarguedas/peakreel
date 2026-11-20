from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
import yaml
from keyring.backend import KeyringBackend

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


class MemoryKeyring(KeyringBackend):
    """An in-memory credential store, so tests never touch the real one."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    """Replace the OS credential store for every test.

    Autouse on purpose: with real YouTube, Meta or Twitch credentials stored on
    this machine, a test that reaches the default publishers would otherwise post
    to the real accounts (it happened: a test uploaded to a channel on 2026-11-17).
    """
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def _fresh_meta_budget() -> None:
    """Each test starts without a Meta request pause (the budget is per process)."""
    from peakreel.publish import meta

    meta.BUDGET.used_pct = meta.BUDGET.read_at = meta.BUDGET.paused_until = 0.0
