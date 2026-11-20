"""Process and machine health, for the status file (M8) and the review page.

Cheap to call (no file walks), so the supervisor samples it with every status
write. On Windows the process numbers come from the process APIs; elsewhere memory
is the peak resident size and "handles" are open file descriptors. The machine's
CPU, RAM and download rate come from psutil, the GPU from ``nvidia-smi`` when there
is one.
"""

from __future__ import annotations

import ctypes
import importlib
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

__all__ = ["MachineHealth", "ProcessHealth", "sample", "sample_machine"]


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


@dataclass(frozen=True, slots=True)
class MachineHealth:
    """One sample of the whole machine.

    Every field is ``None`` when it cannot be measured (no NVIDIA GPU, the first
    sample of a rate).

    Attributes
    ----------
    cpu_pct : float or None
        CPU use since the previous sample, all cores (100 = all busy).
    ram_used_gb, ram_total_gb : float or None
    gpu_pct : float or None
        GPU use, from ``nvidia-smi``.
    vram_used_gb, vram_total_gb : float or None
    download_mbps : float or None
        Bytes received on all network interfaces since the previous sample, in
        Mbit/s: the recordings' bandwidth, the pipeline's limit on a slow link.
    """

    cpu_pct: float | None
    ram_used_gb: float | None
    ram_total_gb: float | None
    gpu_pct: float | None
    vram_used_gb: float | None
    vram_total_gb: float | None
    download_mbps: float | None

    def to_json(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


_last_received: tuple[float, int] | None = None  # (perf_counter time, bytes) of the last sample
# (busy, total) CPU seconds of the last sample. Kept here: psutil.cpu_percent keeps its
# previous sample per thread, and the status is written from a different pool thread
# each time, so it always read 0.0.
_last_cpu: tuple[float, float] | None = None


def _gpu() -> tuple[float | None, float | None, float | None]:
    tool = shutil.which("nvidia-smi")
    if tool is None:
        return None, None, None
    try:
        out = subprocess.run(
            [tool, "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout  # fmt: skip
        use, used, total = (float(x) for x in out.splitlines()[0].split(","))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, None, None
    return use, round(used / 1024, 1), round(total / 1024, 1)


def sample_machine() -> MachineHealth:
    """Measure the machine now (rates are since the previous call).

    Returns
    -------
    MachineHealth

    Notes
    -----
    The first call has no previous sample: its CPU use and download rate are
    ``None``. Calling it more often than about once a
    second makes the rates noisy.
    """
    global _last_received, _last_cpu
    memory = psutil.virtual_memory()
    # perf_counter: time.monotonic ticks only every 15.6 ms on Windows (two samples, no rate).
    now, received = time.perf_counter(), psutil.net_io_counters().bytes_recv
    download = None
    if _last_received is not None and now > _last_received[0]:
        download = round((received - _last_received[1]) * 8 / (now - _last_received[0]) / 1e6, 1)
    _last_received = (now, received)
    times = psutil.cpu_times()
    total = float(sum(times))
    busy = total - times.idle
    cpu = None
    if _last_cpu is not None and total > _last_cpu[1]:
        cpu = round(100 * (busy - _last_cpu[0]) / (total - _last_cpu[1]), 1)
    _last_cpu = (busy, total)
    gpu, vram_used, vram_total = _gpu()
    return MachineHealth(
        cpu_pct=cpu,
        ram_used_gb=round(memory.used / 2**30, 1),
        ram_total_gb=round(memory.total / 2**30, 1),
        gpu_pct=gpu,
        vram_used_gb=vram_used,
        vram_total_gb=vram_total,
        download_mbps=download,
    )


def sample() -> ProcessHealth:
    """Measure this process now."""
    try:
        memory, handles = _windows() if sys.platform == "win32" else _posix()
    except (OSError, AttributeError, ValueError):
        memory, handles = None, None
    return ProcessHealth(memory, handles, threading.active_count())
