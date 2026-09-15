"""Aperiodic load-shape generator.

Layers, each independently random, deliberately stacked so that no single
frequency dominates and nothing repeats:

  1. semi-Markov regime machine  -- which "mood" the box is in (idle..extreme),
     with heavy-tailed (log-normal) dwell times, so the length of an extreme
     stretch is itself random and occasionally very long.
  2. Ornstein-Uhlenbeck drift    -- smooth mean-reverting wander inside the
     regime's band; continuous, never repeating.
  3. Poisson shock train         -- rare additive spikes/dropouts with
     log-normally distributed amplitude and duration.
  4. drifting cross-correlation  -- each resource mixes a shared "global mood"
     with its own private mood, and the mixing weight itself random-walks, so
     resources sometimes move together and sometimes independently.
  5. gamma calibration           -- the whole thing is warped so the long-run
     mean level lands on a configured target (e.g. 0.90).

Sampling is driven by wall-clock dt, so jittered, irregular tick intervals
feed straight through; there is no fixed step size anywhere.
"""
from __future__ import annotations

import math
import os
import random
import struct

from .util import clamp


def new_rng(label: str = "") -> random.Random:
    """A fresh stream seeded from the OS CSPRNG (never from wall clock alone)."""
    seed = struct.unpack("<Q", os.urandom(8))[0] ^ (hash(label) & 0xFFFFFFFFFFFFFFFF)
    return random.Random(seed)


def lognormal_dwell(rng: random.Random, mean: float, sigma: float) -> float:
    """Heavy-tailed positive duration whose expectation is `mean`."""
    mu = math.log(max(1e-6, mean)) - 0.5 * sigma * sigma
    return max(0.35, rng.lognormvariate(mu, sigma))


class OU:
    """Ornstein-Uhlenbeck process: dx = theta*(mu-x)dt + sigma*sqrt(dt)*dW."""

    def __init__(self, rng: random.Random, mu: float = 0.5, theta: float = 0.12,
                 sigma: float = 0.35, lo: float = 0.0, hi: float = 1.0,
                 x0: float | None = None):
        self.rng = rng
        self.mu = mu
        self.theta = theta
        self.sigma = sigma
        self.lo = lo
        self.hi = hi
        self.x = clamp(mu if x0 is None else x0, lo, hi)

    def step(self, dt: float) -> float:
        dt = clamp(dt, 1e-3, 30.0)
        drift = self.theta * (self.mu - self.x) * dt
        shock = self.sigma * math.sqrt(dt) * self.rng.gauss(0.0, 1.0)
        self.x = clamp(self.x + drift + shock, self.lo, self.hi)
        return self.x


class Regime:
    __slots__ = ("name", "lo", "hi", "dwell", "dwell_sigma", "weight")

    def __init__(self, name: str, lo: float, hi: float, dwell: float,
                 dwell_sigma: float, weight: float):
        self.name = name
        self.lo = lo
        self.hi = hi
        self.dwell = dwell
        self.dwell_sigma = dwell_sigma
        self.weight = weight


# Bands are deliberately overlapping and the dwell sigmas are large: an
# "extreme" stretch averages ~3min but the tail reaches well past an hour.
DEFAULT_REGIMES = (
    Regime("idle",    0.00, 0.12,  22.0, 1.05, 0.07),
    Regime("low",     0.10, 0.38,  40.0, 1.00, 0.12),
    Regime("mid",     0.32, 0.66,  65.0, 0.95, 0.19),
    Regime("high",    0.60, 0.90, 105.0, 0.90, 0.28),
    Regime("extreme", 0.86, 1.00, 175.0, 1.25, 0.34),
)


class RegimeMachine:
    """Semi-Markov chain: random regime, random (heavy-tailed) dwell, repeat."""

    def __init__(self, rng: random.Random, regimes=DEFAULT_REGIMES,
                 speed: float = 1.0, stickiness: float = 0.22):
        self.rng = rng
        self.regimes = list(regimes)
        self.speed = max(0.05, speed)
        self.stickiness = clamp(stickiness, 0.0, 0.9)
        self.current = self._pick(None)
        self.remaining = self._dwell(self.current)
        self.transitions = 0

    def _pick(self, avoid: Regime | None) -> Regime:
        # Weighted draw, with a bias toward neighbouring regimes so the box
        # usually ramps rather than teleporting between idle and extreme.
        weights = []
        idx = self.regimes.index(avoid) if avoid in self.regimes else None
        for i, r in enumerate(self.regimes):
            w = r.weight
            if idx is not None:
                if i == idx:
                    w *= self.stickiness
                else:
                    w *= 1.0 / (1.0 + 0.55 * (abs(i - idx) - 1))
            weights.append(max(1e-9, w))
        return self.rng.choices(self.regimes, weights=weights, k=1)[0]

    def _dwell(self, r: Regime) -> float:
        return lognormal_dwell(self.rng, r.dwell / self.speed, r.dwell_sigma)

    def step(self, dt: float) -> Regime:
        self.remaining -= dt
        while self.remaining <= 0.0:
            nxt = self._pick(self.current)
            self.current = nxt
            self.remaining += self._dwell(nxt)
            self.transitions += 1
        return self.current


class ShockTrain:
    """Poisson-arrival additive spikes and dropouts of random size/length."""

    def __init__(self, rng: random.Random, rate_per_min: float = 1.4,
                 up_bias: float = 0.72):
        self.rng = rng
        self.rate = max(0.0, rate_per_min) / 60.0
        self.up_bias = clamp(up_bias, 0.0, 1.0)
        self.active: list[list[float]] = []  # [amplitude, remaining, total]

    def step(self, dt: float) -> float:
        # arrivals
        if self.rate > 0.0:
            p = 1.0 - math.exp(-self.rate * dt)
            while self.rng.random() < p:
                up = self.rng.random() < self.up_bias
                amp = self.rng.lognormvariate(math.log(0.28), 0.75)
                amp = clamp(amp, 0.03, 1.2) * (1.0 if up else -1.0)
                dur = lognormal_dwell(self.rng, 9.0 if up else 6.0, 1.15)
                self.active.append([amp, dur, dur])
                p *= 0.25  # allow rare double arrivals, then stop
        total = 0.0
        still: list[list[float]] = []
        for s in self.active:
            s[1] -= dt
            if s[1] <= 0.0:
                continue
            # smooth rise/fall so shocks don't look like square waves
            frac = clamp(s[1] / max(1e-6, s[2]), 0.0, 1.0)
            total += s[0] * math.sin(math.pi * frac) ** 0.5
            still.append(s)
        self.active = still
        return total


class DropoutTrain:
    """Rare multiplicative quiet spells applied *after* mean calibration.

    Calibration warps levels upward, which would otherwise squash every dip
    into the top of the range. Dropouts are applied afterwards so the valleys
    stay deep and visible while the long-run mean is still honoured (the
    calibrator accounts for them exactly).
    """

    def __init__(self, rng: random.Random, rate_per_min: float = 0.5,
                 floor: float = 0.05, mean_len: float = 13.0):
        self.rng = rng
        self.rate = max(0.0, rate_per_min) / 60.0
        self.floor = clamp(floor, 0.0, 0.95)
        self.mean_len = max(1.0, mean_len)
        self.remaining = 0.0
        self.total = 1.0
        self.depth = 1.0

    def step(self, dt: float) -> float:
        if self.remaining <= 0.0:
            if self.rate > 0.0 and self.rng.random() < 1.0 - math.exp(-self.rate * dt):
                self.total = lognormal_dwell(self.rng, self.mean_len, 1.20)
                self.remaining = self.total
                self.depth = self.rng.uniform(self.floor, min(0.92, self.floor + 0.55))
            else:
                return 1.0
        self.remaining -= dt
        if self.remaining <= 0.0:
            return 1.0
        # cosine taper in and out: no square edges, no repeating period
        frac = clamp(self.remaining / max(1e-6, self.total), 0.0, 1.0)
        env = math.sin(math.pi * frac) ** 0.6
        return clamp(1.0 - (1.0 - self.depth) * env, 0.0, 1.0)


class Channel:
    """One resource's level in [0,1]: regime band + OU + shocks + global mix."""

    def __init__(self, name: str, rng: random.Random, speed: float = 1.0,
                 shock_rate: float = 1.4, coupling: float = 0.5,
                 dropout_rate: float = 0.5, dropout_floor: float = 0.05):
        self.name = name
        self.rng = rng
        self.regimes = RegimeMachine(rng, speed=speed * rng.uniform(0.7, 1.45))
        self.ou = OU(rng, mu=0.5, theta=rng.uniform(0.10, 0.30),
                     sigma=rng.uniform(0.28, 0.55))
        self.shocks = ShockTrain(rng, rate_per_min=shock_rate * rng.uniform(0.6, 1.5))
        # how strongly this channel follows the shared global mood; drifts.
        self.mix = OU(rng, mu=clamp(coupling, 0.05, 0.95),
                      theta=0.05, sigma=0.09, lo=0.0, hi=1.0)
        self.dropouts = DropoutTrain(rng, rate_per_min=dropout_rate * rng.uniform(0.6, 1.5),
                                     floor=dropout_floor,
                                     mean_len=13.0 * rng.uniform(0.6, 1.8) / max(0.05, speed))
        self.regime_name = self.regimes.current.name
        self.last_dropout = 1.0

    def step(self, dt: float, global_level: float) -> float:
        r = self.regimes.step(dt)
        self.regime_name = r.name
        u = self.ou.step(dt)
        own = r.lo + (r.hi - r.lo) * u
        w = self.mix.step(dt)
        level = w * global_level + (1.0 - w) * own
        level += self.shocks.step(dt)
        self.last_dropout = self.dropouts.step(dt)
        return clamp(level, 0.0, 1.0)


class Conductor:
    """Drives every resource channel and self-calibrates to a target mean."""

    CHANNELS = ("cpu", "mem", "disk", "net")
    # Memory dips are expensive (whole-heap free + refault storm), so its
    # quiet spells are shallower and rarer than the others'.
    DROPOUT_FLOOR = {"cpu": 0.04, "mem": 0.30, "disk": 0.02, "net": 0.02}
    DROPOUT_RATE = {"cpu": 0.55, "mem": 0.25, "disk": 0.70, "net": 0.70}

    def __init__(self, target_mean: float = 0.90, speed: float = 1.0,
                 shock_rate: float = 1.4, seed_label: str = "conductor",
                 calibrate: bool = True):
        self.rng = new_rng(seed_label)
        self.target_mean = clamp(target_mean, 0.05, 0.995)
        self.speed = speed
        self.shock_rate = shock_rate
        self.global_regimes = RegimeMachine(self.rng, speed=speed * 0.65)
        self.global_ou = OU(self.rng, mu=0.5, theta=0.09, sigma=0.30)
        self.global_shocks = ShockTrain(self.rng, rate_per_min=shock_rate * 0.6)
        self.channels = {
            name: Channel(name, new_rng(f"{seed_label}:{name}:{os.getpid()}"),
                          speed=speed, shock_rate=shock_rate,
                          coupling=self.rng.uniform(0.25, 0.75),
                          dropout_rate=self.DROPOUT_RATE.get(name, 0.5),
                          dropout_floor=self.DROPOUT_FLOOR.get(name, 0.05))
            for name in self.CHANNELS
        }
        self.gamma = 1.0
        self.global_level = 0.5
        self.global_regime = self.global_regimes.current.name
        self.calibration_mean = None
        if calibrate:
            self.gamma, self.calibration_mean = self._calibrate()

    # -- raw (uncalibrated) sampling -------------------------------------
    def _raw_step(self, dt: float) -> dict[str, float]:
        r = self.global_regimes.step(dt)
        self.global_regime = r.name
        g = r.lo + (r.hi - r.lo) * self.global_ou.step(dt)
        g = clamp(g + self.global_shocks.step(dt), 0.0, 1.0)
        self.global_level = g
        return {n: c.step(dt, g) for n, c in self.channels.items()}

    def _calibrate(self, virtual_seconds: float = 160_000.0, dt: float = 2.5):
        """Solve for the gamma warp that makes the *final* mean hit the target.

        One throwaway simulation caches (raw level, dropout multiplier) pairs;
        gamma is then found by bisection on that cached sample, so the answer
        accounts for dropouts exactly instead of approximating around them.
        """
        probe = Conductor(self.target_mean, self.speed, self.shock_rate,
                          seed_label="calibrate", calibrate=False)
        raws: list[float] = []
        mults: list[float] = []
        for _ in range(int(virtual_seconds / dt)):
            vals = probe._raw_step(dt)
            for name, v in vals.items():
                raws.append(v)
                mults.append(probe.channels[name].last_dropout)
        if not raws:
            return (1.0, None)

        def mean_at(gamma: float) -> float:
            tot = 0.0
            for v, m in zip(raws, mults):
                tot += (v ** gamma) * m
            return tot / len(raws)

        lo, hi = 0.02, 12.0
        if mean_at(lo) < self.target_mean:
            return (lo, mean_at(lo))       # target unreachable (dropouts too deep)
        if mean_at(hi) > self.target_mean:
            return (hi, mean_at(hi))
        for _ in range(34):
            mid = 0.5 * (lo + hi)
            if mean_at(mid) > self.target_mean:
                lo = mid
            else:
                hi = mid
        gamma = 0.5 * (lo + hi)
        return (gamma, mean_at(gamma))

    # -- public ----------------------------------------------------------
    def step(self, dt: float) -> dict[str, float]:
        raw = self._raw_step(dt)
        out = {}
        for name, v in raw.items():
            lvl = (v ** self.gamma) * self.channels[name].last_dropout
            out[name] = clamp(lvl, 0.0, 1.0)
        return out

    def describe(self) -> str:
        parts = [f"g:{self.global_regime}"]
        for n, c in self.channels.items():
            tag = c.regime_name
            if c.last_dropout < 0.9:
                tag += f"-quiet{c.last_dropout:.2f}"
            parts.append(f"{n}:{tag}")
        return " ".join(parts)
