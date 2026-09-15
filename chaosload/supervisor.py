"""The supervisor: turns random load *levels* into actual resource pressure,
keeps every worker alive forever, and never lets the box fall over.

Control strategy
----------------
* CPU is closed-loop: a PI controller drives the workers' duty cycle from the
  measured system-wide utilisation, so the target is hit regardless of what
  else (including our own disk/net workers) happens to be running.
* Memory is computed against live MemAvailable, leaving a hard floor, and is
  re-derived every tick so foreign allocations push us down instead of out.
* Disk and network are open-loop byte-rate targets, capped by budget and by a
  free-space floor.

Everything is re-evaluated on a jittered tick, so even the control loop itself
has no fixed period.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import signal
import socket
import sys
import time

from . import metrics
from .config import Config, resolve_is_loopback
from .control import (Ctl, Flags, SLOT_A, SLOT_B, SLOT_HEARTBEAT, SLOT_HINT,
                      SLOT_PANIC, SLOT_TARGET, new_block)
from .rng import Conductor, new_rng
from .util import (clamp, human_bytes, human_time, pid_alive, set_proc_title,
                   setup_logging, write_atomic)
from .workers import cpu as cpu_worker
from .workers import disk as disk_worker
from .workers import memory as mem_worker
from .workers import net as net_worker

MB = 1 << 20
GB = 1 << 30


def _worker_entry(fn, ctl: Ctl, cfg: Config, extra: tuple) -> None:
    """Child process trampoline: isolate failures, never take the box down."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: ctl.request_stop())
    log = setup_logging(None, cfg.log_level, console=True, tag=f"{ctl.kind}{ctl.index}")
    try:
        ctl.beat(force=True)
        fn(ctl, cfg, *extra)
    except KeyboardInterrupt:
        pass
    except Exception as exc:                      # noqa: BLE001 - must not escape
        log.error("worker %s%d crashed: %r", ctl.kind, ctl.index, exc)
        time.sleep(0.5)
        os._exit(17)
    os._exit(0)


class Worker:
    """One supervised child process plus its shared control block."""

    STALE_AFTER = {"cpu": 20.0, "mem": 25.0, "disk": 90.0, "net": 45.0, "netsrv": 60.0}

    def __init__(self, kind: str, index: int, fn, cfg: Config, flags: Flags, extra=()):
        self.kind = kind
        self.index = index
        self.fn = fn
        self.cfg = cfg
        self.flags = flags
        self.extra = extra
        self.block = new_block()
        self.ctl = Ctl(self.block, flags, kind, index)
        self.proc: mp.Process | None = None
        self.restarts = 0
        self.started_at = 0.0

    @property
    def name(self) -> str:
        return f"{self.kind}{self.index}"

    def start(self) -> None:
        self.block[SLOT_HEARTBEAT] = time.monotonic()
        self.block[SLOT_PANIC] = 0.0
        self.proc = mp.Process(target=_worker_entry,
                               args=(self.fn, self.ctl, self.cfg, self.extra),
                               name=f"chaos-{self.name}", daemon=False)
        self.proc.start()
        self.started_at = time.monotonic()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def stale_for(self) -> float:
        return time.monotonic() - self.block[SLOT_HEARTBEAT]

    def is_stale(self) -> bool:
        limit = self.STALE_AFTER.get(self.kind, 30.0)
        return (time.monotonic() - self.started_at > limit
                and self.stale_for() > limit)

    def kill(self, sig=signal.SIGKILL) -> None:
        p = self.proc
        if p is None or p.pid is None:
            return
        try:
            os.kill(p.pid, sig)
        except ProcessLookupError:
            pass
        except Exception:
            pass

    def reap(self, timeout: float = 2.0) -> None:
        if self.proc is None:
            return
        self.proc.join(timeout)
        if self.proc.is_alive():
            self.kill(signal.SIGKILL)
            self.proc.join(1.0)
        self.proc = None

    # target/telemetry helpers
    def set_target(self, value: float) -> None:
        self.block[SLOT_TARGET] = float(value)

    def set_hint(self, value: float) -> None:
        self.block[SLOT_HINT] = float(value)

    def set_panic(self, on: bool) -> None:
        self.block[SLOT_PANIC] = 1.0 if on else 0.0

    @property
    def a(self) -> float:
        return self.block[SLOT_A]

    @property
    def b(self) -> float:
        return self.block[SLOT_B]


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = setup_logging(cfg.log_file, cfg.log_level, console=not cfg.quiet)
        self.rng = new_rng("supervisor")
        self.flags = Flags()
        self.workers: list[Worker] = []
        self.cpu_meter = metrics.CpuMeter()
        self.net_meter = metrics.NetMeter()
        self.disk_meter = metrics.DiskMeter()
        self.conductor: Conductor | None = None
        self.duty = 0.35          # CPU duty-cycle command
        self.integral = 0.0
        # Disk and network I/O done from userspace also burns CPU. On a small
        # box they alone can pin every core, which would flatten the random
        # CPU curve into a permanent 100%. This scale lets the CPU channel stay
        # in charge: it throttles the auxiliary subsystems whenever the CPU
        # workers are already idle and the box is still above target.
        self.aux_scale = 1.0
        self.started = time.monotonic()
        self.levels: dict[str, float] = {k: 0.0 for k in Conductor.CHANNELS}
        self.last_status = 0.0
        self.next_chaos = 0.0
        self.tcp_sock: socket.socket | None = None
        self.udp_sock: socket.socket | None = None
        self.mem_panic_until = 0.0
        self.stats = {"restarts": 0, "chaos_kills": 0, "mem_panics": 0, "ticks": 0}

    # ------------------------------------------------------------ setup --
    def _open_sockets(self) -> bool:
        cfg = self.cfg
        # Never emit billable egress, whatever the config path said.
        ok, addrs = resolve_is_loopback(cfg.net_host)
        if not ok and not cfg.allow_external_net:
            self.log.error("network subsystem disabled: %s resolves to %s, which "
                           "is not loopback, and --allow-external-net was not given "
                           "(off-box traffic is metered egress)",
                           cfg.net_host, ", ".join(addrs) or "nothing")
            cfg.net = False
            return False
        try:
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            t.bind((cfg.net_host, cfg.net_port))
            t.listen(1024)
            cfg.net_tcp_port = t.getsockname()[1]

            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                u.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
                u.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
            except OSError:
                pass
            try:
                u.bind((cfg.net_host, cfg.net_tcp_port))
            except OSError:
                u.bind((cfg.net_host, 0))
            cfg.net_udp_port = u.getsockname()[1]

            self.tcp_sock, self.udp_sock = t, u
            self.log.info("network target %s tcp/%d udp/%d (%s)",
                          cfg.net_host, cfg.net_tcp_port, cfg.net_udp_port,
                          "loopback only - no billable egress"
                          if not cfg.allow_external_net else
                          "EXTERNAL TRAFFIC ALLOWED - this is metered egress")
            return True
        except OSError as exc:
            self.log.error("network subsystem disabled: cannot bind %s: %s",
                           cfg.net_host, exc)
            cfg.net = False
            return False

    def _spawn_all(self) -> None:
        cfg = self.cfg
        if cfg.cpu:
            for i in range(cfg.cpu_workers):
                self.workers.append(Worker("cpu", i, cpu_worker.run, cfg, self.flags))
        if cfg.mem:
            for i in range(cfg.mem_workers):
                self.workers.append(Worker("mem", i, mem_worker.run, cfg, self.flags))
        if cfg.disk:
            shutil.rmtree(cfg.disk_dir, ignore_errors=True)
            os.makedirs(cfg.disk_dir, exist_ok=True)
            for i in range(cfg.disk_workers):
                self.workers.append(Worker("disk", i, disk_worker.run, cfg, self.flags))
        if cfg.net and self._open_sockets():
            self.workers.append(Worker("netsrv", 0, net_worker.run_server, cfg,
                                       self.flags, extra=(self.tcp_sock, self.udp_sock)))
            for i in range(cfg.net_workers):
                self.workers.append(Worker("net", i, net_worker.run_client, cfg, self.flags))

        for w in self.workers:
            w.set_target(0.0)
            w.set_hint(0.5)
            w.start()
        self.log.info("spawned %d workers: %s", len(self.workers),
                      ", ".join(f"{w.name}:{w.proc.pid}" for w in self.workers))

    def of_kind(self, kind: str) -> list[Worker]:
        return [w for w in self.workers if w.kind == kind]

    # ------------------------------------------------------- control law --
    def cpu_target(self, level: float) -> float:
        cfg = self.cfg
        return cfg.cpu_min_util + (cfg.cpu_max_util - cfg.cpu_min_util) * level

    def _govern(self, level: float, measured: float | None) -> None:
        """Keep *total* CPU tracking the CPU channel by throttling disk/net.

        Multiplicative decrease (fast, proportional to the overshoot) and
        additive-ish increase (slower), so the box recovers headroom gently
        instead of oscillating between saturated and idle.

        Priority: real I/O gets first claim on the CPU budget and the CPU
        filler workers take whatever is left. So we also grow the aux scale
        while the filler is busy -- the PI loop then backs the filler off by
        itself, and the box ends up doing interesting work rather than just
        spinning integers.
        """
        if measured is None:
            return
        excess = measured - self.cpu_target(level)
        if excess > 0.02 and self.duty <= 0.05:
            factor = clamp(1.0 - 2.2 * excess, 0.45, 0.97)
            self.aux_scale = clamp(self.aux_scale * factor, 0.02, 1.0)
        elif excess < -0.02 or self.duty > 0.25:
            self.aux_scale = clamp(self.aux_scale * 1.10 + 0.015, 0.02, 1.0)

    def _drive_cpu(self, level: float, measured: float | None) -> None:
        workers = self.of_kind("cpu")
        if not workers:
            self.duty = 0.0       # let the governor drive the aux subsystems
            return
        cfg = self.cfg
        target = self.cpu_target(level)

        if measured is not None:
            err = target - measured
            # PI with anti-windup; gentle integral so we do not oscillate
            self.integral = clamp(self.integral + err * 0.10, -0.6, 0.6)
            self.duty = clamp(self.duty + 1.15 * err + self.integral * 0.12, 0.0, 1.0)
            if target <= cfg.cpu_min_util + 1e-6:
                self.duty = 0.0
                self.integral = 0.0
        for w in workers:
            # per-worker jitter so the cores are never in lockstep
            w.set_target(clamp(self.duty * self.rng.uniform(0.9, 1.1), 0.0, 1.0))
            w.set_hint(level)

    def _drive_mem(self, level: float) -> tuple[int, int, float]:
        workers = self.of_kind("mem")
        total, avail, used_frac = metrics.mem_state()
        if not workers or total <= 0:
            return (total, avail, used_frac)
        cfg = self.cfg
        ours = sum(w.a for w in workers)
        floor = cfg.mem_floor_mb * MB
        others = max(0.0, (total - avail) - ours)

        want_frac = cfg.mem_min_frac + (cfg.mem_max_frac - cfg.mem_min_frac) * level
        want_total = want_frac * total
        our_target = want_total - others if cfg.respect_others else want_total
        # never plan past the floor, whatever the arithmetic says
        ceiling = max(0.0, total - others - floor)
        our_target = clamp(our_target, 0.0, ceiling)

        panicking = avail < floor * 0.8
        if panicking:
            self.mem_panic_until = time.monotonic() + self.rng.uniform(1.0, 3.0)
            self.stats["mem_panics"] += 1
        panic_now = time.monotonic() < self.mem_panic_until

        share = our_target / len(workers)
        for w in workers:
            w.set_panic(panic_now)
            w.set_target(share)
            # churn bandwidth costs CPU too, so it follows the governor
            w.set_hint(clamp(level * (0.3 + 0.7 * self.aux_scale), 0.03, 1.0))
        return (total, avail, used_frac)

    def _drive_disk(self, level: float) -> None:
        cfg = self.cfg
        free, _ = metrics.disk_free(cfg.disk_dir if os.path.isdir(cfg.disk_dir) else "/")
        panic = free < cfg.disk_free_floor_gb * GB
        for w in self.of_kind("disk"):
            w.set_panic(panic)
            # jittered per-worker rate so the two never move in lockstep
            # disk work is mostly kernel/device bound, so it is throttled
            # more gently than the network path
            w.set_target(max(1.0, cfg.disk_max_mbps * level
                             * (self.aux_scale ** 0.5) * self.rng.uniform(0.75, 1.25)))
            w.set_hint(level)

    def _drive_net(self, level: float) -> None:
        cfg = self.cfg
        for w in self.of_kind("net"):
            w.set_target(max(0.5, cfg.net_max_mbps * level * self.aux_scale
                             * self.rng.uniform(0.7, 1.3)))
            w.set_hint(level)
        for w in self.of_kind("netsrv"):
            w.set_target(level)

    # -------------------------------------------------------- supervision --
    def _supervise(self) -> None:
        for w in self.workers:
            if self.flags.stopping:
                return
            if not w.alive():
                code = w.proc.exitcode if w.proc else None
                w.reap(0.5)
                w.restarts += 1
                self.stats["restarts"] += 1
                self.log.warning("worker %s died (exit=%s), restart #%d",
                                 w.name, code, w.restarts)
                w.start()
            elif w.is_stale():
                self.log.warning("worker %s wedged (%.1fs since heartbeat), recycling",
                                 w.name, w.stale_for())
                w.kill(signal.SIGKILL)
                w.reap(2.0)
                w.restarts += 1
                self.stats["restarts"] += 1
                w.start()

    def _maybe_chaos(self, now: float) -> None:
        """Randomly execute a worker to prove the supervisor really recovers."""
        if not self.cfg.chaos_restarts or self.cfg.chaos_rate_per_hour <= 0:
            return
        if now < self.next_chaos:
            return
        if self.next_chaos > 0.0 and self.workers:
            victim = self.workers[self.rng.randrange(len(self.workers))]
            self.log.info("chaos: killing %s (pid=%s) on purpose",
                          victim.name, victim.proc.pid if victim.proc else "?")
            victim.kill(signal.SIGKILL)
            self.stats["chaos_kills"] += 1
        mean_gap = 3600.0 / max(0.01, self.cfg.chaos_rate_per_hour)
        self.next_chaos = now + self.rng.expovariate(1.0 / mean_gap)

    # ------------------------------------------------------------ status --
    def _status(self, cpu_util, mem_total, mem_avail, mem_used_frac, dio, netio) -> dict:
        ours_mem = sum(w.a for w in self.of_kind("mem"))
        cpu_ops = sum(w.a for w in self.of_kind("cpu"))
        cpu_busy = sum(w.b for w in self.of_kind("cpu"))
        disk_rate = sum(w.a for w in self.of_kind("disk"))
        disk_held = sum(w.b for w in self.of_kind("disk"))
        net_rate = sum(w.a for w in self.of_kind("net"))
        conns = sum(w.b for w in self.of_kind("net"))
        return {
            "ts": time.time(),
            "uptime_s": round(time.monotonic() - self.started, 1),
            "pid": os.getpid(),
            "profile": self.cfg.profile,
            "target_mean": self.cfg.target,
            "levels": {k: round(v, 3) for k, v in self.levels.items()},
            "regimes": self.conductor.describe() if self.conductor else "",
            "aux_scale": round(self.aux_scale, 3),
            "cpu": {"util": round(cpu_util or 0.0, 3), "duty": round(self.duty, 3),
                    "target": round(self.cpu_target(self.levels["cpu"]), 3),
                    "ops_per_s": round(cpu_ops, 1), "cores_busy": round(cpu_busy, 2),
                    "loadavg": metrics.loadavg()[0]},
            "mem": {"used_frac": round(mem_used_frac, 3),
                    "total": mem_total, "available": mem_avail, "ours": int(ours_mem)},
            "disk": {"worker_bytes_per_s": round(disk_rate, 1),
                     "held_bytes": int(disk_held),
                     "dev_read_bytes_per_s": round((dio or (0, 0))[0], 1),
                     "dev_write_bytes_per_s": round((dio or (0, 0))[1], 1)},
            "net": {"worker_bytes_per_s": round(net_rate, 1),
                    "connections": int(conns),
                    "if_bytes_per_s": round((netio or (0, 0))[0], 1),
                    "lo_bytes_per_s": round((netio or (0, 0))[1], 1)},
            "workers": [{"name": w.name, "pid": w.proc.pid if w.proc else None,
                         "alive": w.alive(), "restarts": w.restarts} for w in self.workers],
            "stats": dict(self.stats),
        }

    def _log_status(self, st: dict) -> None:
        lv = st["levels"]
        self.log.info(
            "lvl cpu=%.2f mem=%.2f dsk=%.2f net=%.2f aux=%.2f | cpu %3.0f%%->%.0f%% (duty %.2f, %.2f cores) "
            "| mem %3.0f%% used, ours %s, avail %s | dsk %s/s (dev r%s w%s, held %s) "
            "| net %s/s (%s conns) | up %s restarts=%d | %s",
            lv["cpu"], lv["mem"], lv["disk"], lv["net"], st["aux_scale"],
            st["cpu"]["util"] * 100, st["cpu"]["target"] * 100,
            st["cpu"]["duty"], st["cpu"]["cores_busy"],
            st["mem"]["used_frac"] * 100, human_bytes(st["mem"]["ours"]),
            human_bytes(st["mem"]["available"]),
            human_bytes(st["disk"]["worker_bytes_per_s"]),
            human_bytes(st["disk"]["dev_read_bytes_per_s"]),
            human_bytes(st["disk"]["dev_write_bytes_per_s"]),
            human_bytes(st["disk"]["held_bytes"]),
            human_bytes(st["net"]["worker_bytes_per_s"]), st["net"]["connections"],
            human_time(st["uptime_s"]), st["stats"]["restarts"], st["regimes"])

    # -------------------------------------------------------------- main --
    def run(self) -> int:
        cfg = self.cfg
        set_proc_title("chaos-super")
        self.log.info("chaosload starting: %s", cfg.summary())
        self.log.info("calibrating random load shape to mean %.2f ...", cfg.target)
        t0 = time.perf_counter()
        self.conductor = Conductor(target_mean=cfg.target, speed=cfg.speed,
                                   shock_rate=cfg.shock_rate)
        self.log.info("calibrated in %.1fs (gamma=%.3f, predicted mean=%.3f)",
                      time.perf_counter() - t0, self.conductor.gamma,
                      self.conductor.calibration_mean or -1)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)

        self.flags.beat()
        self._spawn_all()
        if not self.workers:
            self.log.error("no subsystems enabled; nothing to do")
            return 2

        self.cpu_meter.sample()
        self.net_meter.sample()
        self.disk_meter.sample()
        last = time.monotonic()
        deadline = self.started + cfg.duration if cfg.duration > 0 else None

        try:
            while not self.flags.stopping:
                # jittered tick: the control loop itself has no fixed period
                nap = self.rng.uniform(cfg.tick_min, cfg.tick_max)
                if self.rng.random() < 0.03:
                    nap *= self.rng.uniform(2.0, 6.0)      # occasional long tick
                self._nap(nap)
                if self.flags.stopping:
                    break

                now = time.monotonic()
                dt = max(1e-3, now - last)
                last = now
                self.stats["ticks"] += 1

                self.levels = self.conductor.step(dt)
                cpu_util = self.cpu_meter.sample()
                self._drive_cpu(self.levels["cpu"], cpu_util)
                self._govern(self.levels["cpu"], cpu_util)
                mem_total, mem_avail, mem_used = self._drive_mem(self.levels["mem"])
                self._drive_disk(self.levels["disk"])
                self._drive_net(self.levels["net"])

                self._supervise()
                self._maybe_chaos(now)

                if now - self.last_status >= cfg.status_every:
                    st = self._status(cpu_util, mem_total, mem_avail, mem_used,
                                      self.disk_meter.sample(), self.net_meter.sample())
                    self._log_status(st)
                    write_atomic(cfg.status_file, json.dumps(st, indent=1))
                    self.last_status = now

                if deadline and now >= deadline:
                    self.log.info("duration reached, shutting down")
                    break
        except KeyboardInterrupt:
            self.log.info("interrupted")
        finally:
            self.shutdown()
        return 0

    def _nap(self, seconds: float) -> None:
        """Sleep in slices, refreshing the liveness beacon the workers watch."""
        end = time.monotonic() + seconds
        while True:
            self.flags.beat()
            left = end - time.monotonic()
            if left <= 0 or self.flags.stopping:
                return
            time.sleep(min(0.1, left))

    def _on_signal(self, signum, _frame) -> None:
        self.log.info("signal %s received, stopping", signal.Signals(signum).name)
        self.flags.set_stop()

    def shutdown(self) -> None:
        self.flags.set_stop()
        for w in self.workers:
            w.set_panic(True)
            w.set_target(0.0)
        deadline = time.monotonic() + 6.0
        for w in self.workers:
            w.reap(max(0.2, deadline - time.monotonic()))
        for w in self.workers:
            if w.alive():
                w.kill(signal.SIGKILL)
                w.reap(1.0)
        for s in (self.tcp_sock, self.udp_sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        if self.cfg.disk:
            shutil.rmtree(self.cfg.disk_dir, ignore_errors=True)
        try:
            st = {"ts": time.time(), "state": "stopped",
                  "uptime_s": round(time.monotonic() - self.started, 1),
                  "stats": dict(self.stats)}
            write_atomic(self.cfg.status_file, json.dumps(st, indent=1))
        except Exception:
            pass
        self.log.info("stopped after %s (%d worker restarts, %d chaos kills)",
                      human_time(time.monotonic() - self.started),
                      self.stats["restarts"], self.stats["chaos_kills"])
