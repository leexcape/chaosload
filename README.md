# chaosload

A supervised, never-ending, **randomised** load generator for CPU, memory,
storage and network. It drives a machine to roughly a configured share of its
capacity (90% by default) along a load curve that has no periodic pattern:
resources rise and fall independently, extreme stretches last a random length
of time, and quiet spells arrive at random too.

It keeps running until you stop it.

```bash
./chaos.sh start        # background, survives logout, runs forever
./chaos.sh status       # what is happening right now
./chaos.sh watch        # same, refreshed every 2s
./chaos.sh logs -f      # follow the log
./chaos.sh stop         # graceful stop, frees everything, cleans scratch files
```

Foreground instead (Ctrl-C to stop): `./chaos.sh run`

Requires only Python 3.9+ and the standard library.

---

## What it actually does

| Subsystem | Work performed |
|---|---|
| **CPU** | 18 classic kernels on rotation: integer/modular arithmetic, Collatz, trial-division primes, sieves, 1024-bit modular exponentiation, float transcendentals, naive matrix multiply, SHA/MD5/BLAKE2, PBKDF2, zlib round-trips, sorting, dict churn, random-permutation pointer chasing (cache-miss storms), bulk memcpy, regex, JSON, base64, recursive fib. The task in use is re-drawn constantly, so the instruction mix never settles. |
| **Memory** | Grows and shrinks a resident heap in chunks, writing a unique salt into every 4 KiB page so the pages are genuinely committed (and not deduplicated), then continuously sweeps and rewrites it to burn memory bandwidth and keep it hot. |
| **Storage** | Random mix of bulk sequential writes, appends, whole-file reads, random `pread`s, scattered `pwrite` rewrites, `fsync`/`fdatasync`, small-file metadata storms (create/stat/list/unlink by the hundred), and deletes. Periodic `POSIX_FADV_DONTNEED` evicts our own page cache so reads really reach the device. |
| **Network** | Loopback TCP and UDP: bulk streams, request/response ping-pong, rapid connect/send/close churn, and UDP blasts, against a server process this program runs itself. **Nothing leaves the host** -- see below. |

## How the randomness works

Five independent layers stack up, so no single frequency dominates and
nothing repeats:

1. **Semi-Markov regime machine** — each resource wanders between `idle`,
   `low`, `mid`, `high` and `extreme` moods. Dwell times are log-normal, so
   how long an extreme stretch lasts is itself random, with a long tail: the
   average is minutes, but occasionally it holds for an hour.
2. **Ornstein–Uhlenbeck drift** — smooth mean-reverting wander inside the
   current mood's band. Continuous, never repeating.
3. **Poisson shock train** — rare additive spikes and dips, with log-normal
   amplitude and duration, tapered so they are not square waves.
4. **Drifting cross-correlation** — every resource mixes a shared "global
   mood" with its own private one, and the mixing weight random-walks. So
   resources sometimes surge together and sometimes go their own way.
5. **Dropout train** — rare multiplicative quiet spells, applied after
   calibration so the valleys stay deep and visible.

Even the control loop's own tick is jittered (0.2–0.85s, with occasional long
ticks), so there is no sampling period to lock onto either.

**Self-calibration.** At startup the conductor simulates ~45 hours of virtual
load shape, then solves (by bisection, on the cached sample) for the exponent
that makes the *long-run mean* land exactly on `--target`. Ask for 0.90 and
you get a mean of 0.90 — with real excursions to both 1.0 and near-idle,
rather than a flat line pinned at 90%.

## How the targets are actually hit

* **CPU is closed-loop.** A PI controller drives the workers' duty cycle from
  measured system-wide utilisation, so the target is met regardless of what
  else is running — including this program's own I/O workers.
* **I/O gets first claim on the CPU budget.** Disk and network I/O from
  userspace burns CPU; on a small box it alone can pin every core and flatten
  the CPU curve into a permanent 100%. A governor throttles the I/O
  subsystems whenever the box is over target and the CPU filler is already
  idle, and lets them grow back when the filler is busy. The filler takes
  whatever is left, so the machine does interesting work rather than just
  spinning integers.
* **Memory is recomputed live** against `MemAvailable` every tick, so
  allocations by other processes push chaosload down instead of pushing the
  machine over.

## Staying out of trouble

This is designed to take a machine to the edge and hold it there without
going over:

* **Memory floor** — never lets `MemAvailable` fall below `--mem-floor-mb`
  (640 MB default). The floor is re-checked before *every* chunk allocation,
  chunks are small enough to stop on a dime, and a panic flag frees the whole
  heap at once. Important on a swapless box.
* **OOM ordering** — workers set `oom_score_adj` high, so if the kernel ever
  does have to kill something, it kills chaosload and not your other work.
* **Disk budget and free-space floor** — a byte budget (auto-sized to a third
  of free space, capped at 24 GB) plus a hard floor (`--disk-free-floor-gb`,
  4 GB default). Everything is written under `.chaos-scratch/`, which is
  removed on exit and re-created clean on start.
* **Nice** — workers run at `nice 5`, so an interactive shell stays usable.
* **Single instance** — an `flock`'d pid file; a second start refuses rather
  than doubling the load.
* **No billable egress, enforced.** Network load runs entirely over the
  loopback interface: packets never reach the NIC, so a cloud provider cannot
  meter them. This is not merely the default — `--net-host` is resolved at
  startup and the program *refuses to run* if it points anywhere but
  loopback, unless you pass `--allow-external-net` and accept the charges.
  The supervisor re-checks before it binds a socket. At these rates (500+
  MB/s) accidental egress would cost hundreds of GB per hour, so it takes a
  deliberate flag. `--no-net` turns the network load off entirely.

  To verify on a running instance, compare `lo` against the real interface:

  ```bash
  # all the traffic should be on lo; the NIC should show only your own ssh
  cat /proc/net/dev | awk 'NR>2 {print $1, $2, $10}'
  ```

## Robustness

The supervisor restarts any worker that dies or stops heartbeating, forever,
and it proves it: by default it SIGKILLs one of its own workers every ~15
minutes (`--chaos-rate`) and recovers.

For that to be survivable, **no cross-process state uses a lock**.
`multiprocessing.Event` and friends are built on POSIX semaphores in shared
memory — SIGKILL a process holding one and every other process blocks on that
futex forever. All coordination here is plain words in a shared array, which a
dead process cannot be holding. Workers also carry `PR_SET_PDEATHSIG` and
watch a supervisor liveness beacon, so they can never outlive their parent.

## Options

`--profile gentle|normal|busy|savage` picks a preset; everything is
overridable. A few of the useful ones:

```
--target 0.9          long-run mean load level (0..1)
--speed 1.0           how fast the random regimes change
--shock-rate 1.4      random spikes per minute per resource
--duration 0          seconds to run; 0 = until stopped
--only cpu,mem        run just these subsystems (or --no-disk, --no-net, ...)
--cpu-workers 0       0 = one per core
--mem-max-frac 0.88   ceiling on system-wide used memory
--mem-floor-mb 640    hard floor on MemAvailable
--disk-max-gb 0       0 = auto (a third of free space, capped)
--disk-dir PATH       where scratch files live
--net-max-mbps 420    per network worker
--chaos-rate 4        random worker kills per hour
```

Full list: `./chaos.sh help`. Every setting also takes a `CHAOS_<FIELD>`
environment variable.

## Running it as a service

```bash
sudo cp chaosload.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chaosload     # survives reboots
journalctl -u chaosload -f
```

## Files

```
chaos.sh              control wrapper (start/stop/status/watch/logs/clean)
chaosload/
  __main__.py         entry point, single-instance lock
  supervisor.py       spawning, control loops, watchdog, safety, status
  rng.py              the randomness engine (regimes, OU, shocks, calibration)
  config.py           defaults, profiles, CLI, env overrides
  metrics.py          /proc and statvfs sampling
  control.py          lock-free shared control blocks
  pacing.py           byte-rate pacer
  workers/            cpu.py  memory.py  disk.py  net.py
run/                  log, status.json, pid  (created at runtime)
.chaos-scratch/       disk scratch files     (created and removed at runtime)
```
