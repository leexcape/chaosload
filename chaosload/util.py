"""Small shared helpers: logging, clamping, pid files, process hygiene."""
from __future__ import annotations

import ctypes
import fcntl
import logging
import logging.handlers
import os
import signal
import sys
import time

# ---------------------------------------------------------------- numbers ---


def clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0 or unit == "T":
            if unit == "B":
                return f"{n:.0f}B"
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def human_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------- logging ---


def setup_logging(log_path: str | None, level: str = "INFO", console: bool = True,
                  tag: str = "chaos") -> logging.Logger:
    logger = logging.getLogger("chaosload")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s [" + tag + ":%(process)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if log_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=16 * 1024 * 1024, backupCount=3
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception as exc:  # logging must never be fatal
            print(f"warning: cannot open log file {log_path}: {exc}", file=sys.stderr)
    if console or not logger.handlers:
        sh = logging.StreamHandler(stream=sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


# ------------------------------------------------------- process hygiene ----

PR_SET_PDEATHSIG = 1


def die_with_parent(sig: int = signal.SIGTERM) -> bool:
    """Ask the kernel to signal us when our parent dies (Linux only)."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        return libc.prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0) == 0
    except Exception:
        return False


def set_oom_score_adj(value: int) -> bool:
    """Make ourselves the kernel's preferred OOM victim (children only)."""
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write(str(int(clamp(value, -1000, 1000))))
        return True
    except Exception:
        return False


def set_nice(value: int) -> bool:
    try:
        os.nice(int(value))
        return True
    except Exception:
        return False


def set_proc_title(name: str) -> None:
    """Best effort cosmetic rename so `top`/`ps` show what each worker does."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(name.encode()[:15] + b"\0")
        libc.prctl(15, ctypes.byref(buf), 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


class PidFile:
    """flock-based single-instance guard. Stale files are reclaimed safely."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return True

    def read_pid(self) -> int | None:
        try:
            with open(self.path) as fh:
                return int(fh.read().strip() or 0) or None
        except Exception:
            return None

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        try:
            os.unlink(self.path)
        except Exception:
            pass


def write_atomic(path: str, data: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def monotonic() -> float:
    return time.monotonic()
