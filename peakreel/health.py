"""Process health: memory, OS handles and threads, for the status file (M8).

Cheap to call (no file walks), so the supervisor samples it with every status
write. On Windows the numbers come from the process APIs; elsewhere memory is the
peak resident size and "handles" are open file descriptors.
"""

from __future__ import annotations

import ctypes
import importlib
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = ["ProcessHealth", "sample"]


@dataclass(frozen=True, slots=True)
class ProcessHealth:
    """One sample of this process's resources.

    Attributes
    ----------
    memory_mb : float or None
        Working set (Windows) or peak resident size (elsewhere).
    handles : int or None
        OS handles (Windows) or open file descriptors (Linux).
    threads : int
        Python threads alive.
    """

    memory_mb: float | None
    handles: int | None
    threads: int

    def to_json(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows() -> tuple[float | None, int | None]:
    from ctypes import wintypes

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - absent off Windows
    kernel32 = windll.kernel32
    psapi = windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    count = wintypes.DWORD()
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handles = (
        int(count.value) if kernel32.GetProcessHandleCount(process, ctypes.byref(count)) else None
    )
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD,
    ]  # fmt: skip
    ok = psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
    memory = round(counters.WorkingSetSize / 2**20, 1) if ok else None
    return memory, handles


def _posix() -> tuple[float | None, int | None]:
    resource: Any = importlib.import_module("resource")  # not on Windows
    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    memory = peak / 2**20 if sys.platform == "darwin" else peak / 2**10
    fd_dir = Path("/proc/self/fd")
    handles = sum(1 for _ in fd_dir.iterdir()) if fd_dir.is_dir() else None
    return round(memory, 1), handles


def sample() -> ProcessHealth:
    """Measure this process now."""
    try:
        memory, handles = _windows() if sys.platform == "win32" else _posix()
    except (OSError, AttributeError, ValueError):
        memory, handles = None, None
    return ProcessHealth(memory, handles, threading.active_count())
