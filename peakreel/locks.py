"""Per-channel ownership locks.

Exactly one process may write a channel's chat log (and record its video). Two
writers appending to the same ``data/chat/<channel>/<day>.jsonl`` would duplicate
every message and corrupt the spike detector's input.

The lock is an operating-system file lock (``msvcrt.locking`` on Windows, ``flock``
elsewhere) on ``data/run/locks/<channel>.lock``. The OS releases it when the process
exits for any reason, including a crash or ``taskkill``, so a lock can never go
stale. The owner's PID is written into the file for the error message only.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import IO, Self

__all__ = ["ChannelLocks", "LockHeld"]

# Windows byte-range locks also block reads of the locked bytes, so lock one byte far
# past the PID text (a lock may extend beyond the end of the file).
_LOCK_OFFSET = 1 << 30


class LockHeld(RuntimeError):
    """Raised when another process owns a channel.

    Attributes
    ----------
    channel : str
    owner_pid : int or None
        PID recorded by the owner, when readable.
    """

    def __init__(self, channel: str, owner_pid: int | None) -> None:
        self.channel = channel
        self.owner_pid = owner_pid
        owner = f"process {owner_pid}" if owner_pid else "another process"
        super().__init__(
            f"#{channel} is already recorded by {owner}; stop it first "
            f"(`peakreel chat stop` / `peakreel run stop`)"
        )


def _try_lock(handle: IO[str]) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ChannelLocks:
    """Hold the ownership locks of several channels for the life of a process.

    Use as a context manager. Acquisition is all-or-nothing: if any channel is held
    elsewhere, the ones already taken are released and :class:`LockHeld` is raised.

    Parameters
    ----------
    lock_dir : pathlib.Path
        ``data/run/locks``; created if missing.
    channels : iterable of str

    Examples
    --------
    >>> with ChannelLocks(Path("data/run/locks"), ["alpha", "beta"]):  # doctest: +SKIP
    ...     record()
    """

    def __init__(self, lock_dir: Path, channels: Iterable[str]) -> None:
        self._dir = lock_dir
        self._channels = tuple(dict.fromkeys(channels))
        self._held: dict[str, IO[str]] = {}

    @property
    def held(self) -> tuple[str, ...]:
        """Channels currently locked by this object."""
        return tuple(self._held)

    def acquire(self) -> None:
        """Take every lock or none.

        Raises
        ------
        LockHeld
            If another process owns one of the channels.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            for channel in self._channels:
                self._acquire_one(channel)
        except BaseException:
            self.release()
            raise

    def _acquire_one(self, channel: str) -> None:
        path = self._dir / f"{channel}.lock"
        handle = path.open("a+", encoding="utf-8")
        if not _try_lock(handle):
            try:
                handle.seek(0)
                text = handle.read().strip()
            except OSError:
                text = ""
            handle.close()
            raise LockHeld(channel, int(text) if text.isdigit() else None)
        # The PID text is informational only; the lock is the OS lock on the file.
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._held[channel] = handle

    def add(self, channel: str) -> None:
        """Take one more channel's lock while holding the others.

        Raises
        ------
        LockHeld
            If another process owns ``channel``.
        """
        if channel in self._held:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._acquire_one(channel)

    def release(self, channels: Iterable[str] | None = None) -> None:
        """Release some or all held locks. Safe to call more than once.

        Parameters
        ----------
        channels : iterable of str, optional
            Defaults to every held channel.
        """
        for channel in list(channels if channels is not None else self._held):
            handle = self._held.pop(channel, None)
            if handle is not None:
                _unlock(handle)
                handle.close()

    def __enter__(self) -> Self:
        """Acquire every lock."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release every lock."""
        self.release()
