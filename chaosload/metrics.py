"""Cheap /proc + statvfs sampling. Everything degrades gracefully to None."""
from __future__ import annotations

import os
import time


def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except Exception:
        return ""


class CpuMeter:
    """System-wide CPU utilisation between successive sample() calls."""

    def __init__(self) -> None:
        self._prev = self._raw()

    @staticmethod
    def _raw() -> tuple[float, float] | None:
        line = _read("/proc/stat").split("\n", 1)[0]
        if not line.startswith("cpu "):
            return None
        parts = [float(x) for x in line.split()[1:]]
        while len(parts) < 8:
            parts.append(0.0)
        idle = parts[3] + parts[4]          # idle + iowait
        total = sum(parts[:8])
        return (total - idle, total)

    def sample(self) -> float | None:
        cur = self._raw()
        if cur is None or self._prev is None:
            self._prev = cur
            return None
        busy = cur[0] - self._prev[0]
        total = cur[1] - self._prev[1]
        self._prev = cur
        if total <= 0:
            return None
        return max(0.0, min(1.0, busy / total))


class _RateMeter:
    def __init__(self) -> None:
        self._prev: tuple[float, tuple[float, ...]] | None = None

    def _rates(self, values: tuple[float, ...]) -> tuple[float, ...] | None:
        now = time.monotonic()
        if self._prev is None:
            self._prev = (now, values)
            return None
        dt = now - self._prev[0]
        prev = self._prev[1]
        self._prev = (now, values)
        if dt <= 0 or len(prev) != len(values):
            return None
        return tuple(max(0.0, (a - b) / dt) for a, b in zip(values, prev))


class NetMeter(_RateMeter):
    """Bytes/s across all interfaces, and loopback separately."""

    def sample(self) -> tuple[float, float] | None:
        total = lo = 0.0
        for line in _read("/proc/net/dev").split("\n")[2:]:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            cols = rest.split()
            if len(cols) < 9:
                continue
            moved = float(cols[0]) + float(cols[8])  # rx + tx bytes
            if name == "lo":
                lo += moved
            else:
                total += moved
        r = self._rates((total, lo))
        # every loopback byte is accounted on both rx and tx, so halve it
        return (r[0], r[1] / 2.0) if r else None


class DiskMeter(_RateMeter):
    """Bytes/s read+written across real block devices (from /proc/diskstats)."""

    SECTOR = 512

    def __init__(self) -> None:
        super().__init__()
        self._whole: dict[str, bool] = {}

    def _is_whole_disk(self, name: str) -> bool:
        """A whole disk has /sys/block/<name>; a partition lives under it.

        Counting both `xvda` and `xvda1` would double every byte, since the
        parent disk's counters already include its partitions'.
        """
        hit = self._whole.get(name)
        if hit is None:
            hit = os.path.isdir(f"/sys/block/{name}")
            self._whole[name] = hit
        return hit

    def sample(self) -> tuple[float, float] | None:
        read = write = 0.0
        for line in _read("/proc/diskstats").split("\n"):
            cols = line.split()
            if len(cols) < 10:
                continue
            name = cols[2]
            if name.startswith(("loop", "ram", "dm-", "zram")):
                continue
            # skip partitions; the parent disk already counts their bytes
            if not self._is_whole_disk(name):
                continue
            try:
                read += float(cols[5]) * self.SECTOR
                write += float(cols[9]) * self.SECTOR
            except ValueError:
                continue
        r = self._rates((read, write))
        return (r[0], r[1]) if r else None


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read("/proc/meminfo").split("\n"):
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        parts = val.split()
        if not parts:
            continue
        try:
            n = int(parts[0])
        except ValueError:
            continue
        out[key] = n * 1024 if len(parts) > 1 and parts[1] == "kB" else n
    return out


def mem_state() -> tuple[int, int, float]:
    """(total_bytes, available_bytes, used_fraction) -- used excludes reclaimable."""
    mi = meminfo()
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    if total <= 0:
        return (0, 0, 0.0)
    return (total, avail, max(0.0, min(1.0, 1.0 - avail / total)))


def disk_free(path: str) -> tuple[int, int]:
    """(free_bytes_for_us, total_bytes)"""
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize)
    except Exception:
        return (0, 0)


def loadavg() -> tuple[float, float, float]:
    try:
        parts = _read("/proc/loadavg").split()
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except Exception:
        return (0.0, 0.0, 0.0)


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return max(1, os.cpu_count() or 1)
