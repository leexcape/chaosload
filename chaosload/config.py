"""Configuration: machine-derived defaults, CLI flags, env overrides, profiles."""
from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import os
import socket

from . import metrics
from .util import clamp, human_bytes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROFILES = {
    # name        target  speed  shocks  mem_max  disk  net
    "gentle":   dict(target=0.45, speed=1.0, shock_rate=1.0, mem_max_frac=0.55),
    "normal":   dict(target=0.75, speed=1.0, shock_rate=1.4, mem_max_frac=0.78),
    "busy":     dict(target=0.90, speed=1.0, shock_rate=1.4, mem_max_frac=0.88),
    "savage":   dict(target=0.97, speed=1.4, shock_rate=2.2, mem_max_frac=0.93),
}


def resolve_is_loopback(host: str) -> tuple[bool, list[str]]:
    """Does `host` resolve to loopback addresses *only*?

    Loopback traffic never reaches the NIC, so no cloud provider can bill it.
    Anything else is potentially metered egress, which this program refuses to
    generate unless you explicitly opt in.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return (False, [])
    addrs = sorted({i[4][0] for i in infos})
    if not addrs:
        return (False, [])
    for a in addrs:
        try:
            if not ipaddress.ip_address(a).is_loopback:
                return (False, addrs)
        except ValueError:
            return (False, addrs)
    return (True, addrs)


@dataclasses.dataclass
class Config:
    # --- global shape ---
    profile: str = "busy"
    target: float = 0.90          # long-run mean level in [0,1]
    speed: float = 1.0            # >1 = regimes change faster
    shock_rate: float = 1.4       # spikes per minute per channel
    duration: float = 0.0         # 0 = forever
    tick_min: float = 0.20        # control loop interval is itself jittered
    tick_max: float = 0.85

    # --- which subsystems ---
    cpu: bool = True
    mem: bool = True
    disk: bool = True
    net: bool = True

    # --- cpu ---
    cpu_workers: int = 0          # 0 = auto (one per core)
    cpu_min_util: float = 0.01
    cpu_max_util: float = 1.00
    cpu_nice: int = 5

    # --- memory ---
    mem_workers: int = 2
    mem_max_frac: float = 0.88    # ceiling on *system-wide* used fraction
    mem_min_frac: float = 0.10    # our own floor contribution
    mem_floor_mb: int = 640       # never let MemAvailable drop below this
    mem_chunk_mb: int = 48
    mem_touch_mbps: float = 900.0 # background re-touch bandwidth at level 1

    # --- disk ---
    disk_workers: int = 2
    disk_dir: str = ""            # default: <root>/.chaos-scratch
    disk_max_gb: float = 0.0      # 0 = auto
    disk_free_floor_gb: float = 4.0
    disk_max_mbps: float = 260.0  # per worker, at level 1.0
    disk_fsync_prob: float = 0.22

    # --- network ---
    net_workers: int = 2
    net_max_mbps: float = 420.0   # per worker, at level 1.0
    net_host: str = "127.0.0.1"
    net_port: int = 0             # 0 = ephemeral
    net_tcp_port: int = 0         # resolved at runtime
    net_udp_port: int = 0         # resolved at runtime
    net_conns: int = 6            # concurrent sockets per client worker
    allow_external_net: bool = False   # opt-in to metered, off-box traffic

    # --- safety / ops ---
    respect_others: bool = True   # back off if foreign load already fills the box
    chaos_restarts: bool = True   # randomly recycle workers to prove resilience
    chaos_rate_per_hour: float = 4.0
    log_file: str = ""
    status_file: str = ""
    pid_file: str = ""
    log_level: str = "INFO"
    status_every: float = 15.0
    quiet: bool = False

    # --- derived at runtime ---
    ncpu: int = 0
    mem_total: int = 0

    def finalize(self) -> "Config":
        self.ncpu = metrics.cpu_count()
        total, _, _ = metrics.mem_state()
        self.mem_total = total
        if self.cpu_workers <= 0:
            self.cpu_workers = max(1, self.ncpu)
        self.mem_workers = max(1, self.mem_workers)
        self.disk_workers = max(1, self.disk_workers)
        self.net_workers = max(1, self.net_workers)
        if not self.disk_dir:
            self.disk_dir = os.path.join(ROOT, ".chaos-scratch")
        self.disk_dir = os.path.abspath(self.disk_dir)
        if self.disk_max_gb <= 0:
            free, _ = metrics.disk_free(os.path.dirname(self.disk_dir) or "/")
            budget = free / (1024 ** 3) - self.disk_free_floor_gb
            self.disk_max_gb = clamp(budget * 0.35, 0.5, 24.0)
        run_dir = os.path.join(ROOT, "run")
        self.log_file = self.log_file or os.path.join(run_dir, "chaosload.log")
        self.status_file = self.status_file or os.path.join(run_dir, "status.json")
        self.pid_file = self.pid_file or os.path.join(run_dir, "chaosload.pid")
        self.target = clamp(self.target, 0.02, 0.995)
        self.mem_max_frac = clamp(self.mem_max_frac, 0.10, 0.95)
        self.tick_min = clamp(self.tick_min, 0.05, 5.0)
        self.tick_max = clamp(max(self.tick_max, self.tick_min + 0.05), 0.1, 10.0)
        return self

    def summary(self) -> str:
        subsystems = ",".join(n for n, on in (
            ("cpu", self.cpu), ("mem", self.mem),
            ("disk", self.disk), ("net", self.net)) if on) or "none"
        return (
            f"profile={self.profile} target_mean={self.target:.2f} speed={self.speed} "
            f"subsystems={subsystems} | cpu_workers={self.cpu_workers} "
            f"mem={self.mem_workers}w<={self.mem_max_frac:.0%} of "
            f"{human_bytes(self.mem_total)} floor={self.mem_floor_mb}MB | "
            f"disk={self.disk_workers}w<={self.disk_max_gb:.1f}GB @{self.disk_dir} | "
            f"net={self.net_workers}w {self.net_host}"
        )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _env_overrides(cfg: Config) -> None:
    """CHAOS_<FIELD> env vars override defaults (CLI still wins)."""
    for field in dataclasses.fields(cfg):
        raw = os.environ.get("CHAOS_" + field.name.upper())
        if raw is None:
            continue
        try:
            if field.type is bool or isinstance(getattr(cfg, field.name), bool):
                setattr(cfg, field.name, raw.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(getattr(cfg, field.name), int):
                setattr(cfg, field.name, int(float(raw)))
            elif isinstance(getattr(cfg, field.name), float):
                setattr(cfg, field.name, float(raw))
            else:
                setattr(cfg, field.name, raw)
        except (TypeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chaosload",
        description="Randomly and relentlessly load CPU, memory, disk and network.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--profile", choices=sorted(PROFILES), default="busy",
                   help="preset load shape")
    p.add_argument("--target", type=float, default=None,
                   help="long-run mean load level 0..1 (overrides profile)")
    p.add_argument("--speed", type=float, default=None,
                   help="how fast the random regimes change (1.0 = default)")
    p.add_argument("--shock-rate", type=float, default=None,
                   help="random spikes per minute per resource")
    p.add_argument("--duration", type=float, default=0.0,
                   help="seconds to run; 0 = until killed")
    p.add_argument("--seed-note", default="", help=argparse.SUPPRESS)

    g = p.add_argument_group("subsystems")
    g.add_argument("--no-cpu", action="store_true")
    g.add_argument("--no-mem", action="store_true")
    g.add_argument("--no-disk", action="store_true")
    g.add_argument("--no-net", action="store_true")
    g.add_argument("--only", default="",
                   help="comma list of subsystems to run (cpu,mem,disk,net)")

    g = p.add_argument_group("cpu")
    g.add_argument("--cpu-workers", type=int, default=0, help="0 = one per core")
    g.add_argument("--cpu-max-util", type=float, default=1.0)
    g.add_argument("--cpu-nice", type=int, default=5)

    g = p.add_argument_group("memory")
    g.add_argument("--mem-workers", type=int, default=2)
    g.add_argument("--mem-max-frac", type=float, default=None,
                   help="ceiling on system-wide used memory fraction")
    g.add_argument("--mem-floor-mb", type=int, default=640,
                   help="hard floor on MemAvailable; we free instantly below it")
    g.add_argument("--mem-chunk-mb", type=int, default=48)

    g = p.add_argument_group("disk")
    g.add_argument("--disk-workers", type=int, default=2)
    g.add_argument("--disk-dir", default="", help="scratch dir (created, cleaned up)")
    g.add_argument("--disk-max-gb", type=float, default=0.0, help="0 = auto")
    g.add_argument("--disk-free-floor-gb", type=float, default=4.0)
    g.add_argument("--disk-max-mbps", type=float, default=260.0, help="per worker")

    g = p.add_argument_group("network")
    g.add_argument("--net-workers", type=int, default=2)
    g.add_argument("--net-max-mbps", type=float, default=420.0, help="per worker")
    g.add_argument("--net-host", default="127.0.0.1",
                   help="bind/connect address; must be loopback unless "
                        "--allow-external-net is given")
    g.add_argument("--allow-external-net", action="store_true",
                   help="permit traffic that leaves this host. On a cloud "
                        "instance that is METERED EGRESS and costs real money "
                        "-- at these rates, hundreds of GB per hour")
    g.add_argument("--net-port", type=int, default=0)

    g = p.add_argument_group("ops")
    g.add_argument("--no-chaos-restarts", action="store_true",
                   help="do not randomly recycle workers")
    g.add_argument("--chaos-rate", type=float, default=4.0,
                   help="random worker kills per hour (resilience exercise)")
    g.add_argument("--no-respect-others", action="store_true",
                   help="ignore pre-existing foreign load when aiming")
    g.add_argument("--log-file", default="")
    g.add_argument("--status-file", default="")
    g.add_argument("--pid-file", default="")
    g.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    g.add_argument("--status-every", type=float, default=15.0)
    g.add_argument("--quiet", action="store_true", help="no console output")
    g.add_argument("--print-config", action="store_true",
                   help="print the resolved config as JSON and exit")
    return p


def config_from_args(argv: list[str] | None = None) -> tuple[Config, argparse.Namespace]:
    args = build_parser().parse_args(argv)
    cfg = Config()
    _env_overrides(cfg)

    cfg.profile = args.profile
    preset = PROFILES[args.profile]
    cfg.target = preset["target"]
    cfg.speed = preset["speed"]
    cfg.shock_rate = preset["shock_rate"]
    cfg.mem_max_frac = preset["mem_max_frac"]

    if args.target is not None:
        cfg.target = args.target
    if args.speed is not None:
        cfg.speed = args.speed
    if args.shock_rate is not None:
        cfg.shock_rate = args.shock_rate
    if args.mem_max_frac is not None:
        cfg.mem_max_frac = args.mem_max_frac

    cfg.duration = args.duration
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",") if s.strip()}
        cfg.cpu, cfg.mem, cfg.disk, cfg.net = (
            "cpu" in wanted, "mem" in wanted, "disk" in wanted, "net" in wanted)
    else:
        cfg.cpu = not args.no_cpu
        cfg.mem = not args.no_mem
        cfg.disk = not args.no_disk
        cfg.net = not args.no_net

    cfg.cpu_workers = args.cpu_workers
    cfg.cpu_max_util = clamp(args.cpu_max_util, 0.05, 1.0)
    cfg.cpu_nice = args.cpu_nice
    cfg.mem_workers = args.mem_workers
    cfg.mem_floor_mb = args.mem_floor_mb
    cfg.mem_chunk_mb = max(4, args.mem_chunk_mb)
    cfg.disk_workers = args.disk_workers
    cfg.disk_dir = args.disk_dir
    cfg.disk_max_gb = args.disk_max_gb
    cfg.disk_free_floor_gb = args.disk_free_floor_gb
    cfg.disk_max_mbps = args.disk_max_mbps
    cfg.net_workers = args.net_workers
    cfg.net_max_mbps = args.net_max_mbps
    cfg.net_host = args.net_host
    cfg.net_port = args.net_port
    cfg.allow_external_net = args.allow_external_net
    cfg.chaos_restarts = not args.no_chaos_restarts
    cfg.chaos_rate_per_hour = args.chaos_rate
    cfg.respect_others = not args.no_respect_others
    cfg.log_file = args.log_file
    cfg.status_file = args.status_file
    cfg.pid_file = args.pid_file
    cfg.log_level = args.log_level
    cfg.status_every = args.status_every
    cfg.quiet = args.quiet

    cfg.finalize()

    # Refuse to bill the user by accident. Loopback stays on the box; anything
    # else is metered egress on a cloud instance, so it takes a deliberate flag.
    if cfg.net:
        ok, addrs = resolve_is_loopback(cfg.net_host)
        if not ok and not cfg.allow_external_net:
            raise SystemExit(
                f"refusing to start: --net-host {cfg.net_host!r} resolves to "
                f"{', '.join(addrs) or 'nothing'}, which is not loopback.\n"
                f"Traffic to a non-loopback address leaves this instance and is "
                f"billed per GB by your cloud provider (this generator moves "
                f"hundreds of GB per hour).\n"
                f"Use the default 127.0.0.1, disable the network load with "
                f"--no-net, or pass --allow-external-net if you really mean it "
                f"and accept the charges.")

    if args.print_config:
        print(json.dumps(cfg.to_dict(), indent=2, sort_keys=True))
        raise SystemExit(0)
    return cfg, args
