"""Byte-rate pacing shared by the disk and network workers."""
from __future__ import annotations

import time

from .control import Ctl

perf = time.perf_counter


class Pacer:
    """Sleeps just enough to hold an average bytes/sec, allowing burstiness."""

    __slots__ = ("ctl", "debt", "t")

    def __init__(self, ctl: Ctl):
        self.ctl = ctl
        self.debt = 0.0
        self.t = perf()

    def account(self, nbytes: int, rate_bps: float) -> None:
        if rate_bps <= 0:
            self.ctl.sleep(0.1)
            return
        now = perf()
        self.debt += nbytes / rate_bps - (now - self.t)
        self.t = now
        if self.debt > 0.004:
            nap = min(self.debt, 0.5)
            self.ctl.sleep(nap)
            self.debt -= nap
        elif self.debt < -0.5:
            self.debt = -0.5      # never bank unlimited credit
