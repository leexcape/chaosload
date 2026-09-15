"""Storage pressure: a random mix of bulk writes, random reads, rewrites,
fsyncs, and small-file metadata churn -- rate-limited to the supervisor's
target and hard-capped by both a byte budget and a free-space floor.

Every byte we write lives under our own scratch directory, and the directory
is removed on the way out (and on the next start).
"""
from __future__ import annotations

import errno
import os
import shutil
import time

from .. import metrics
from ..control import Ctl
from ..pacing import Pacer
from ..rng import new_rng
from ..util import clamp, die_with_parent, set_nice, set_oom_score_adj, set_proc_title

perf = time.perf_counter
MB = 1 << 20


def _fadvise_dontneed(fd: int) -> None:
    """Evict our own pages so later reads really touch the device."""
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except Exception:
        pass


def run(ctl: Ctl, cfg) -> None:
    set_proc_title(f"chaos-disk{ctl.index}")
    die_with_parent()
    set_oom_score_adj(800)
    set_nice(cfg.cpu_nice)
    rng = new_rng(f"disk{ctl.index}")

    root = os.path.join(cfg.disk_dir, f"w{ctl.index}")
    os.makedirs(root, exist_ok=True)
    budget = int(cfg.disk_max_gb * (1 << 30) / max(1, cfg.disk_workers))
    floor = cfg.disk_free_floor_gb * (1 << 30)

    pattern = bytes(rng.getrandbits(8) for _ in range(65536))
    files: list[str] = []
    held = 0
    moved_r = moved_w = 0.0
    last_report = perf()
    pacer = Pacer(ctl)
    seq = 0

    def path_for(i: int) -> str:
        return os.path.join(root, f"blob-{i:06d}.bin")

    def drop(name: str) -> None:
        nonlocal held
        try:
            held -= os.path.getsize(name)
        except OSError:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass
        if name in files:
            files.remove(name)
        if held < 0:
            held = 0

    def reclaim(target_bytes: int) -> None:
        while files and held > target_bytes:
            drop(files[rng.randrange(len(files))])

    def make_payload(n: int) -> bytes:
        # cheaply varied so the filesystem/device cannot dedupe or compress it away
        reps = n // len(pattern) + 1
        blob = bytearray(pattern * reps)[:n]
        for off in range(0, max(1, n - 8), 4096):
            blob[off : off + 4] = rng.getrandbits(32).to_bytes(4, "little")
        return bytes(blob)

    try:
        # start from a clean slate
        for stale in os.listdir(root):
            try:
                os.unlink(os.path.join(root, stale))
            except OSError:
                pass

        while not ctl.should_stop():
            rate = max(1.0, ctl.target) * MB          # target MiB/s -> bytes/s
            free, _ = metrics.disk_free(root)

            if ctl.panic or free < floor:
                reclaim(int(budget * 0.25))
                ctl.beat(float(held), 0.0, force=True)
                ctl.sleep(rng.uniform(0.3, 1.2))
                continue
            if held > budget:
                reclaim(int(budget * rng.uniform(0.55, 0.85)))

            ops = ["write", "write", "read_seq", "read_rand", "rewrite",
                   "append", "small_files", "delete", "sync_write"]
            weights = [3.0, 2.0, 2.0, 2.5, 1.6, 1.4, 1.0, 1.2, 1.0]
            if not files:
                op = "write"
            else:
                op = rng.choices(ops, weights, k=1)[0]

            try:
                if op in ("write", "sync_write"):
                    size = int(rng.choice([4, 8, 16, 32, 64, 128, 256]) * MB
                               * rng.uniform(0.5, 1.3))
                    size = int(min(size, max(MB, budget - held), free - floor))
                    if size < MB:
                        reclaim(int(budget * 0.6))
                        continue
                    seq += 1
                    name = path_for(seq)
                    block = rng.choice([64 << 10, 256 << 10, 1 << 20, 4 << 20])
                    written = 0
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                    try:
                        while written < size and not ctl.should_stop():
                            n = min(block, size - written)
                            os.write(fd, make_payload(n))
                            written += n
                            moved_w += n
                            ctl.beat()          # long paced writes are normal
                            pacer.account(n, rate)
                            if op == "sync_write" or rng.random() < cfg.disk_fsync_prob * 0.05:
                                os.fsync(fd)
                        if op == "sync_write" or rng.random() < cfg.disk_fsync_prob:
                            os.fsync(fd)
                        if rng.random() < 0.35:
                            _fadvise_dontneed(fd)
                    finally:
                        os.close(fd)
                    files.append(name)
                    held += written

                elif op == "append" and files:
                    name = files[rng.randrange(len(files))]
                    size = int(rng.choice([1, 2, 4, 8]) * MB)
                    if held + size < budget:
                        fd = os.open(name, os.O_WRONLY | os.O_APPEND)
                        try:
                            os.write(fd, make_payload(size))
                            if rng.random() < cfg.disk_fsync_prob:
                                os.fdatasync(fd)
                        finally:
                            os.close(fd)
                        held += size
                        moved_w += size
                        pacer.account(size, rate)

                elif op == "read_seq" and files:
                    name = files[rng.randrange(len(files))]
                    fd = os.open(name, os.O_RDONLY)
                    try:
                        block = rng.choice([64 << 10, 256 << 10, 1 << 20])
                        while not ctl.should_stop():
                            data = os.read(fd, block)
                            if not data:
                                break
                            moved_r += len(data)
                            ctl.beat()
                            pacer.account(len(data), rate)
                        if rng.random() < 0.4:
                            _fadvise_dontneed(fd)
                    finally:
                        os.close(fd)

                elif op == "read_rand" and files:
                    name = files[rng.randrange(len(files))]
                    size = os.path.getsize(name)
                    if size > 1 << 16:
                        fd = os.open(name, os.O_RDONLY)
                        try:
                            block = rng.choice([4 << 10, 16 << 10, 64 << 10, 256 << 10])
                            for _ in range(rng.randrange(8, 160)):
                                if ctl.should_stop():
                                    break
                                off = rng.randrange(0, max(1, size - block))
                                data = os.pread(fd, block, off)
                                moved_r += len(data)
                                ctl.beat()
                                pacer.account(len(data), rate)
                            if rng.random() < 0.5:
                                _fadvise_dontneed(fd)
                        finally:
                            os.close(fd)

                elif op == "rewrite" and files:
                    name = files[rng.randrange(len(files))]
                    size = os.path.getsize(name)
                    if size > 1 << 16:
                        fd = os.open(name, os.O_WRONLY)
                        try:
                            block = rng.choice([4 << 10, 64 << 10, 512 << 10])
                            for _ in range(rng.randrange(4, 64)):
                                if ctl.should_stop():
                                    break
                                off = rng.randrange(0, max(1, size - block))
                                os.pwrite(fd, make_payload(block), off)
                                moved_w += block
                                ctl.beat()
                                pacer.account(block, rate)
                            if rng.random() < cfg.disk_fsync_prob:
                                os.fdatasync(fd)
                        finally:
                            os.close(fd)

                elif op == "small_files":
                    # metadata storm: create / stat / read / unlink many tiny files
                    sub = os.path.join(root, f"tiny-{seq}-{rng.randrange(1 << 20)}")
                    os.makedirs(sub, exist_ok=True)
                    count = rng.randrange(80, 900)
                    try:
                        for i in range(count):
                            if ctl.should_stop():
                                break
                            fp = os.path.join(sub, f"f{i:05d}")
                            with open(fp, "wb") as fh:
                                fh.write(pattern[: rng.randrange(64, 4096)])
                            os.stat(fp)
                            moved_w += 2048
                            ctl.beat()
                            pacer.account(2048, rate)
                        os.listdir(sub)
                    finally:
                        shutil.rmtree(sub, ignore_errors=True)

                elif op == "delete" and files:
                    drop(files[rng.randrange(len(files))])

            except OSError as exc:
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    reclaim(int(budget * 0.3))
                    ctl.sleep(1.0)
                elif exc.errno not in (errno.ENOENT, errno.EBADF):
                    ctl.sleep(0.25)

            now = perf()
            if now - last_report >= 0.5:
                span = now - last_report
                ctl.beat((moved_r + moved_w) / span, float(held), force=True)
                moved_r = moved_w = 0.0
                last_report = now
            else:
                ctl.beat()
    finally:
        shutil.rmtree(root, ignore_errors=True)
