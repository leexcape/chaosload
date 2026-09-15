"""Entry point: python3 -m chaosload [options]"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys

from .config import config_from_args
from .supervisor import Supervisor
from .util import PidFile, pid_alive


def main(argv: list[str] | None = None) -> int:
    try:
        mp.set_start_method("fork")
    except RuntimeError:
        pass
    cfg, _args = config_from_args(argv)

    lock = PidFile(cfg.pid_file)
    if not lock.acquire():
        other = lock.read_pid()
        if other and pid_alive(other):
            print(f"chaosload already running (pid {other}); "
                  f"stop it first or use --pid-file", file=sys.stderr)
            return 3
        print(f"could not acquire {cfg.pid_file}", file=sys.stderr)
        return 3
    try:
        return Supervisor(cfg).run()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
