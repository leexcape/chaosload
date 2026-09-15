"""Memory pressure: hold a randomly-sized, constantly-churning resident heap.

The supervisor hands us an absolute byte target. We grow and shrink toward it
in chunks, writing a per-page salt so every page is genuinely resident (and
not deduplicated away), then keep walking the heap to burn memory bandwidth
and keep the pages hot.

This box has no swap, so the guard rails are absolute: before every single
chunk allocation we re-read MemAvailable, and any panic flag or floor breach
frees memory immediately, before anything else happens.
"""
from __future__ import annotations

import math
import os
import time

from .. import metrics
from ..control import Ctl
from ..rng import new_rng
from ..util import clamp, die_with_parent, human_bytes, set_nice, set_oom_score_adj, set_proc_title

PAGE = 4096
SRC_BLOCK = 1 << 20          # 1 MiB memcpy source
perf = time.perf_counter


def _make_source(rng) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(4096)) * (SRC_BLOCK // 4096)


def _alloc_chunk(size: int, src: bytes, rng) -> bytearray:
    """Allocate and fully touch `size` bytes; every page gets a unique salt."""
    buf = bytearray(size)
    mv = memoryview(buf)
    off = 0
    n = len(src)
    while off < size:
        end = min(off + n, size)
        mv[off:end] = src[: end - off]
        off = end
    salt = rng.getrandbits(32).to_bytes(4, "little")
    for p in range(0, size - 4, PAGE):
        mv[p : p + 4] = salt
    mv.release()
    return buf


def run(ctl: Ctl, cfg) -> None:
    set_proc_title(f"chaos-mem{ctl.index}")
    die_with_parent()
    set_oom_score_adj(900)          # if anything must die to an OOM, it is us
    set_nice(max(0, cfg.cpu_nice - 2))
    rng = new_rng(f"mem{ctl.index}")

    floor = cfg.mem_floor_mb * 1024 * 1024
    chunks: list[bytearray] = []
    held = 0
    src = _make_source(rng)
    touched = 0.0
    last_report = perf()
    scan_pos = 0

    def free_down_to(limit_bytes: int) -> None:
        nonlocal held
        while chunks and held > limit_bytes:
            held -= len(chunks.pop())

    try:
        while not ctl.should_stop():
            _, avail, _ = metrics.mem_state()

            # ---- guard rails first, always -----------------------------
            if ctl.panic:
                free_down_to(0)
                ctl.beat(0.0, 0.0, force=True)
                ctl.sleep(rng.uniform(0.2, 0.6))
                continue
            if avail < floor:
                # free roughly the shortfall, plus a margin, immediately
                shortfall = floor - avail
                free_down_to(max(0, held - int(shortfall * 1.4) - (32 << 20)))
                ctl.beat(float(held), 0.0, force=True)
                ctl.sleep(rng.uniform(0.15, 0.4))
                continue

            target = max(0.0, ctl.target)
            headroom = max(0, avail - floor)

            # ---- shrink ------------------------------------------------
            if held > target + (8 << 20):
                # shrink in bursts of random size so RSS does not step evenly
                to_free = (held - target) * rng.uniform(0.35, 1.0)
                free_down_to(int(max(0, held - to_free)))

            # ---- grow --------------------------------------------------
            elif held < target - (8 << 20) and headroom > (48 << 20):
                want = target - held
                chunk = int(cfg.mem_chunk_mb * (1 << 20) * rng.uniform(0.45, 1.6))
                chunk = int(min(chunk, want, headroom * 0.5))
                chunk = max(1 << 20, chunk)
                try:
                    chunks.append(_alloc_chunk(chunk, src, rng))
                    held += chunk
                    # touching pages costs CPU; when the supervisor wants the
                    # box quiet, ramp in more slowly instead of in one storm
                    quiet = 1.0 - clamp(ctl.hint, 0.0, 1.0)
                    if quiet > 0.05:
                        ctl.sleep(quiet * rng.uniform(0.01, 0.07))
                except MemoryError:
                    free_down_to(max(0, held - (256 << 20)))
                    ctl.sleep(0.5)

            # ---- churn: keep the pages hot and the bus busy ------------
            budget_mbps = max(8.0, cfg.mem_touch_mbps * clamp(ctl.hint, 0.05, 1.0))
            burst = int(budget_mbps * (1 << 20) * rng.uniform(0.02, 0.10))
            moved = 0
            t0 = perf()
            while chunks and moved < burst and not ctl.should_stop():
                buf = chunks[rng.randrange(len(chunks))]
                size = len(buf)
                if size <= SRC_BLOCK:
                    continue
                mv = memoryview(buf)
                span = min(size, rng.choice([1 << 16, 1 << 18, SRC_BLOCK]))
                if rng.random() < 0.55:
                    # sequential-ish sweep (bandwidth) with a moving cursor
                    scan_pos = (scan_pos + span) % max(1, size - span)
                    off = scan_pos
                else:
                    off = rng.randrange(0, size - span)
                if rng.random() < 0.5:
                    mv[off : off + span] = src[:span]        # write
                else:
                    sum(mv[off : off + span : PAGE])         # read, page-strided
                mv.release()
                moved += span
                if perf() - t0 > 0.35:
                    break
            touched += moved

            now = perf()
            if now - last_report >= 0.5:
                ctl.beat(float(held), touched / max(1e-6, now - last_report), force=True)
                touched = 0.0
                last_report = now
            else:
                ctl.beat(float(held))

            # pacing: tight when far from target, relaxed when settled
            gap = abs(held - target) / max(1.0, target if target > 0 else 1.0)
            ctl.sleep(rng.uniform(0.02, 0.12) if gap > 0.05 else rng.uniform(0.1, 0.35))
    finally:
        chunks.clear()
