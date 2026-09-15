"""CPU pressure: a rotating menu of dumb, classic, cache-unfriendly work.

The supervisor hands us a duty cycle (0..1); we alternate randomly sized work
quanta with proportional sleeps to land on it. Which *kind* of work runs is
re-drawn constantly, so the instruction mix, branch behaviour and memory
footprint keep changing instead of settling into one steady pattern.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import time
import zlib

from ..control import Ctl
from ..rng import new_rng
from ..util import clamp, die_with_parent, set_nice, set_oom_score_adj, set_proc_title

perf = time.perf_counter


# --------------------------------------------------------------- tasks ----
# Every task busy-works until `deadline` and returns an op count. They check
# the clock every few thousand operations: tight enough to honour the duty
# cycle, loose enough that the check is not the benchmark.
#
# `state` is a dict that lives as long as the task stays selected, so setup
# (big buffers, shuffled permutations, matrices) is paid once per selection
# rather than once per quantum -- otherwise a 20ms quantum could spend a
# second building its own inputs and the duty cycle would go to pieces.


def t_integer(rng, deadline, state):
    acc = state.get("acc") or rng.randrange(1 << 20, 1 << 30)
    k = state.setdefault("k", rng.randrange(3, 99999) | 1)
    ops = 0
    while perf() < deadline:
        for i in range(4000):
            acc = (acc * 1103515245 + 12345 + i * k) & 0x7FFFFFFFFFFF
            acc ^= acc >> 7
            acc += (acc % 9973) * 31
        ops += 4000
    state["acc"] = acc
    return ops


def t_collatz(rng, deadline, state):
    ops = 0
    n0 = state.get("n0") or rng.randrange(1 << 18, 1 << 24)
    while perf() < deadline:
        for _ in range(64):
            n = n0 = n0 + 1
            steps = 0
            while n != 1 and steps < 600:
                n = n // 2 if n % 2 == 0 else 3 * n + 1
                steps += 1
            ops += steps
    state["n0"] = n0
    return ops


def t_primes(rng, deadline, state):
    n = state.get("n") or (rng.randrange(100_000, 900_000) | 1)
    ops = 0
    while perf() < deadline:
        for _ in range(180):
            n += 2
            limit = int(math.isqrt(n))
            d = 3
            prime = n % 2 != 0
            while prime and d <= limit:
                if n % d == 0:
                    prime = False
                d += 2
            ops += limit // 2 + 1
    state["n"] = n
    return ops


def t_sieve(rng, deadline, state):
    ops = 0
    while perf() < deadline:
        size = rng.randrange(200_000, 1_200_000)
        flags = bytearray([1]) * size
        flags[0:2] = b"\0\0"
        for i in range(2, int(math.isqrt(size)) + 1):
            if flags[i]:
                start = i * i
                flags[start::i] = bytearray(len(range(start, size, i)))
        ops += size
    return ops


def t_bignum(rng, deadline, state):
    base = state.get("base") or (rng.getrandbits(1024) | 1)
    exp = state.setdefault("exp", rng.getrandbits(256) | 1)
    mod = state.setdefault("mod", rng.getrandbits(1024) | 1)
    ops = 0
    while perf() < deadline:
        for _ in range(4):
            pow(base, exp, mod)
            base = (base * 6364136223846793005 + 1442695040888963407) | 1
            ops += 1
    state["base"] = base
    return ops


def t_float(rng, deadline, state):
    x = state.get("x") or rng.random() * 10 + 1
    ops = 0
    while perf() < deadline:
        for _ in range(2500):
            x = math.sin(x) * math.cos(x) + math.sqrt(x * x + 1.0)
            x = math.log(abs(x) + 1.5) * 3.7 + math.exp(min(2.0, x * 0.01))
        ops += 2500
    state["x"] = x
    return ops


def t_matrix(rng, deadline, state):
    if "a" not in state:
        n = rng.randrange(28, 66)
        state["n"] = n
        state["a"] = [[rng.random() for _ in range(n)] for _ in range(n)]
        state["b"] = [[rng.random() for _ in range(n)] for _ in range(n)]
    n, a, b = state["n"], state["a"], state["b"]
    bt = list(zip(*b))
    ops = 0
    while perf() < deadline:
        a = [[sum(x * y for x, y in zip(row, col)) for col in bt] for row in a]
        ops += n * n * n
    state["a"] = a
    return ops


def t_hash(rng, deadline, state):
    algo = state.setdefault("algo", rng.choice(
        ["sha256", "sha512", "md5", "blake2b", "sha1", "sha3_256"]))
    buf = state.setdefault("buf", os.urandom(65536))
    ops = 0
    while perf() < deadline:
        for _ in range(24):
            h = hashlib.new(algo)
            h.update(buf)
            h.digest()
            ops += len(buf)
    return ops


def t_pbkdf(rng, deadline, state):
    salt = state.get("salt") or os.urandom(16)
    ops = 0
    while perf() < deadline:
        hashlib.pbkdf2_hmac("sha256", b"chaosload", salt, 4000)
        salt = salt[1:] + bytes([rng.getrandbits(8)])
        ops += 4000
    state["salt"] = salt
    return ops


def t_compress(rng, deadline, state):
    if "text" not in state:
        state["text"] = (os.urandom(2048)
                         + b"the quick brown fox jumps over the lazy dog " * 40) * 6
        state["level"] = rng.randrange(1, 9)
    text, level = state["text"], state["level"]
    ops = 0
    while perf() < deadline:
        blob = zlib.compress(text, level)
        zlib.decompress(blob)
        zlib.crc32(blob)
        ops += len(text)
    return ops


def t_sort(rng, deadline, state):
    if "data" not in state:
        # kept modest so one shuffle+sort stays well inside a work quantum
        state["data"] = [rng.random() for _ in range(rng.randrange(8_000, 45_000))]
    data = state["data"]
    ops = 0
    while perf() < deadline:
        rng.shuffle(data)
        data.sort()
        ops += len(data)
        if perf() >= deadline:
            break
        data.sort(reverse=True)
        ops += len(data)
    return ops


def t_dict_churn(rng, deadline, state):
    ops = 0
    while perf() < deadline:
        d = {}
        for i in range(30_000):
            d[(i * 2654435761) & 0xFFFFF] = i
        for i in range(0, 30_000, 3):
            d.pop((i * 2654435761) & 0xFFFFF, None)
        ops += 40_000
    return ops


def t_pointer_chase(rng, deadline, state):
    """Random-permutation walk: deliberately defeats the cache prefetcher."""
    if "perm" not in state:
        n = rng.choice([1 << 16, 1 << 18, 1 << 20])
        perm = list(range(n))
        rng.shuffle(perm)
        state["perm"] = perm
        state["i"] = 0
    perm, i = state["perm"], state["i"]
    ops = 0
    while perf() < deadline:
        for _ in range(60_000):
            i = perm[i]
        ops += 60_000
    state["i"] = i
    return ops


def t_memcopy(rng, deadline, state):
    if "src" not in state:
        size = rng.choice([1 << 20, 1 << 22, 1 << 23])
        state["src"] = memoryview(bytearray(size))
        state["dst"] = memoryview(bytearray(size))
        state["size"] = size
    src, dst, size = state["src"], state["dst"], state["size"]
    ops = 0
    while perf() < deadline:
        dst[:] = src
        src[0] = (src[0] + 1) & 0xFF
        ops += size
    return ops


def t_regex(rng, deadline, state):
    if "text" not in state:
        words = ["alpha", "beta", "gamma", "delta", "chaos", "load", "entropy"]
        state["text"] = " ".join(rng.choice(words) + str(rng.randrange(1000))
                                 for _ in range(4000))
        state["pat"] = re.compile(r"(\w+?)(\d+)")
    text, pat = state["text"], state["pat"]
    ops = 0
    while perf() < deadline:
        pat.findall(text)
        re.sub(r"\d+", lambda m: str(int(m.group()) + 1), text[:20000])
        ops += len(text)
    return ops


def t_json(rng, deadline, state):
    obj = state.setdefault("obj", {"id": 1, "items": [
        {"k": rng.random(), "v": "x" * rng.randrange(4, 40)} for _ in range(400)]})
    ops = 0
    while perf() < deadline:
        s = json.dumps(obj)
        json.loads(s)
        ops += len(s)
    return ops


def t_base64(rng, deadline, state):
    buf = state.setdefault("buf", os.urandom(8192))
    ops = 0
    while perf() < deadline:
        for _ in range(40):
            enc = base64.b64encode(buf)
            base64.b64decode(enc)
            ops += len(buf)
    return ops


def t_fib(rng, deadline, state):
    def fib(n):
        return n if n < 2 else fib(n - 1) + fib(n - 2)
    depth = state.setdefault("depth", rng.randrange(18, 23))
    ops = 0
    while perf() < deadline:
        fib(depth)
        ops += 1 << depth
    return ops


TASKS = (
    (t_integer, 1.7), (t_collatz, 1.1), (t_primes, 1.2), (t_sieve, 0.9),
    (t_bignum, 1.0), (t_float, 1.2), (t_matrix, 1.0), (t_hash, 1.3),
    (t_pbkdf, 0.7), (t_compress, 1.1), (t_sort, 1.0), (t_dict_churn, 0.9),
    (t_pointer_chase, 1.1), (t_memcopy, 0.9), (t_regex, 0.8), (t_json, 0.7),
    (t_base64, 0.7), (t_fib, 0.6),
)
_TASK_FNS = [t for t, _ in TASKS]
_TASK_WEIGHTS = [w for _, w in TASKS]


def run(ctl: Ctl, cfg) -> None:
    set_proc_title(f"chaos-cpu{ctl.index}")
    die_with_parent()
    set_oom_score_adj(750)
    set_nice(cfg.cpu_nice)
    rng = new_rng(f"cpu{ctl.index}")

    task = rng.choices(_TASK_FNS, _TASK_WEIGHTS, k=1)[0]
    state: dict = {}
    switch_at = perf() + rng.uniform(1.5, 12.0)
    ops = 0.0
    t_work = 0.0
    last_report = perf()

    while not ctl.should_stop():
        duty = clamp(ctl.target, 0.0, 1.0)
        if duty < 0.004:
            ctl.beat(0.0, 0.0, force=True)
            ctl.sleep(rng.uniform(0.05, 0.25))
            continue

        now = perf()
        if now >= switch_at:
            task = rng.choices(_TASK_FNS, _TASK_WEIGHTS, k=1)[0]
            state = {}
            # heavy-tailed: sometimes one task owns the core for minutes
            switch_at = now + rng.lognormvariate(math.log(6.0), 1.1)

        # Randomised work quantum: never a fixed slice, so duty cycling does
        # not create an audible/periodic sawtooth.
        quantum = rng.uniform(0.008, 0.055)
        t0 = perf()
        try:
            ops += task(rng, t0 + quantum, state)
        except Exception:            # noqa: BLE001 - fall back, never die
            task = t_integer
            state = {}
        worked = perf() - t0
        t_work += worked

        if duty < 0.995:
            idle = worked * (1.0 - duty) / max(duty, 1e-3)
            idle *= rng.uniform(0.8, 1.25)      # jitter the gaps too
            if idle > 0.0005:
                ctl.sleep(min(idle, 0.5))

        now = perf()
        if now - last_report >= 0.25:
            span = now - last_report
            ctl.beat(ops / max(span, 1e-6), t_work / max(span, 1e-6), force=True)
            ops = 0.0
            t_work = 0.0
            last_report = now
