"""Network pressure over the loopback path: bulk streams, request/response
ping-pong, connection churn and UDP blasts, all rate-paced by the supervisor.

Everything stays on this host by default (127.0.0.1) -- the point is to load
the kernel network stack, socket layer and scheduler, not to spray packets at
anyone else. Point --net-host elsewhere only if you own the other end.

The listening sockets are created by the supervisor and inherited across
fork(), so restarting the server never races for a port.
"""
from __future__ import annotations

import errno
import os
import select
import struct
import socket
import threading
import time

from ..control import Ctl
from ..pacing import Pacer
from ..rng import new_rng
from ..util import clamp, die_with_parent, set_nice, set_oom_score_adj, set_proc_title

perf = time.perf_counter
MB = 1 << 20
MAX_CONN_THREADS = 96


# ------------------------------------------------------------- server ------

def _serve_conn(conn: socket.socket, ctl: Ctl, sem: threading.Semaphore) -> None:
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(5.0)
        while not ctl.should_stop():
            try:
                data = conn.recv(1 << 18)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            # echo a slice back: keeps the reverse path and both CPUs busy
            # without letting an un-draining peer stall this thread.
            try:
                conn.send(data[: min(len(data), 8192)])
            except BlockingIOError:
                pass
            except OSError:
                break
    finally:
        try:
            conn.close()
        except OSError:
            pass
        sem.release()


def run_server(ctl: Ctl, cfg, tcp_sock: socket.socket, udp_sock: socket.socket) -> None:
    set_proc_title("chaos-netsrv")
    die_with_parent()
    set_oom_score_adj(700)
    set_nice(cfg.cpu_nice)
    sem = threading.Semaphore(MAX_CONN_THREADS)

    def udp_loop():
        udp_sock.settimeout(0.5)
        while not ctl.should_stop():
            try:
                data, addr = udp_sock.recvfrom(65535)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                if ctl.should_stop():
                    return
                time.sleep(0.05)
                continue
            if data[:1] == b"R":              # reply requested
                try:
                    udp_sock.sendto(data[:1024], addr)
                except OSError:
                    pass

    t = threading.Thread(target=udp_loop, daemon=True, name="chaos-udp")
    t.start()

    tcp_sock.settimeout(0.5)
    served = 0
    while not ctl.should_stop():
        try:
            conn, _ = tcp_sock.accept()
        except (socket.timeout, BlockingIOError):
            ctl.beat(float(served))
            continue
        except OSError as exc:
            if ctl.should_stop() or exc.errno == errno.EBADF:
                break
            time.sleep(0.05)
            continue
        served += 1
        if not sem.acquire(blocking=False):
            conn.close()                      # shed load rather than fork-bomb
            continue
        threading.Thread(target=_serve_conn, args=(conn, ctl, sem),
                         daemon=True, name="chaos-conn").start()
        ctl.beat(float(served))


# ------------------------------------------------------------- client ------

_LINGER_OFF = struct.pack("ii", 1, 0)   # close() -> RST, so no client TIME_WAIT


def _connect(cfg, timeout: float = 3.0, abortive: bool = False) -> socket.socket | None:
    """Open a loopback connection. `abortive` sockets reset on close, which
    keeps the churn mode from filling the ephemeral port range with
    TIME_WAIT entries over a long run."""
    try:
        s = socket.create_connection((cfg.net_host, cfg.net_tcp_port), timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if abortive:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_OFF)
        s.setblocking(False)
        return s
    except OSError:
        return None


def _drain(sock: socket.socket, cap: int = 4 << 20) -> int:
    got = 0
    while got < cap:
        try:
            b = sock.recv(1 << 16)
        except (BlockingIOError, InterruptedError):
            return got
        except OSError:
            return -1
        if not b:
            return -1
        got += len(b)
    return got


def _send(sock: socket.socket, buf: memoryview, ctl: Ctl) -> int:
    """Non-blocking sendall that keeps draining so neither side deadlocks."""
    sent = 0
    total = len(buf)
    deadline = perf() + 10.0
    while sent < total and not ctl.should_stop() and perf() < deadline:
        try:
            n = sock.send(buf[sent:])
            sent += n
        except (BlockingIOError, InterruptedError):
            if _drain(sock) < 0:
                return -1
            select.select([sock], [sock], [], 0.2)
        except OSError:
            return -1
    return sent


def run_client(ctl: Ctl, cfg) -> None:
    set_proc_title(f"chaos-net{ctl.index}")
    die_with_parent()
    set_oom_score_adj(800)
    set_nice(cfg.cpu_nice)
    rng = new_rng(f"net{ctl.index}")
    pacer = Pacer(ctl)

    payload = bytes(rng.getrandbits(8) for _ in range(1 << 16))
    conns: list[socket.socket] = []
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setblocking(False)
    moved = 0.0
    conn_count = 0
    last_report = perf()

    def close_all():
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        conns.clear()

    try:
        while not ctl.should_stop():
            rate = max(1.0, ctl.target) * MB
            if ctl.panic:
                close_all()
                ctl.sleep(rng.uniform(0.3, 1.0))
                continue

            # keep a randomly-sized pool of live connections
            want = max(1, int(cfg.net_conns * rng.uniform(0.4, 1.4)))
            while len(conns) < want:
                s = _connect(cfg)
                if s is None:
                    ctl.sleep(rng.uniform(0.05, 0.3))
                    break
                conns.append(s)
                conn_count += 1
            if not conns:
                continue

            mode = rng.choices(
                ("bulk", "pingpong", "churn", "udp", "burst"),
                (3.2, 1.8, 1.0, 1.4, 1.2), k=1)[0]

            try:
                if mode in ("bulk", "burst"):
                    sock = conns[rng.randrange(len(conns))]
                    chunk = rng.choice([8 << 10, 64 << 10, 256 << 10, 1 << 20])
                    reps = rng.randrange(1, 12) if mode == "bulk" else rng.randrange(8, 40)
                    blob = (payload * (chunk // len(payload) + 1))[:chunk]
                    mv = memoryview(blob)
                    for _ in range(reps):
                        if ctl.should_stop():
                            break
                        n = _send(sock, mv, ctl)
                        if n < 0:
                            conns.remove(sock)
                            sock.close()
                            break
                        moved += n
                        back = _drain(sock)
                        if back > 0:
                            moved += back
                        ctl.beat()              # paced sends can be slow
                        pacer.account(n, rate)

                elif mode == "pingpong":
                    sock = conns[rng.randrange(len(conns))]
                    size = rng.randrange(64, 8192)
                    for _ in range(rng.randrange(5, 120)):
                        if ctl.should_stop():
                            break
                        n = _send(sock, memoryview(payload)[:size], ctl)
                        if n < 0:
                            conns.remove(sock)
                            sock.close()
                            break
                        moved += n
                        t_end = perf() + 0.05
                        while perf() < t_end:
                            got = _drain(sock, 1 << 16)
                            if got < 0:
                                break
                            if got:
                                moved += got
                                break
                        ctl.beat()
                        pacer.account(n, rate)

                elif mode == "churn":
                    # rapid connect/send/close: socket table + TIME_WAIT churn
                    for _ in range(rng.randrange(3, 40)):
                        if ctl.should_stop():
                            break
                        s = _connect(cfg, timeout=1.0, abortive=True)
                        if s is None:
                            break
                        conn_count += 1
                        size = rng.randrange(32, 4096)
                        n = _send(s, memoryview(payload)[:size], ctl)
                        if n > 0:
                            moved += n
                            ctl.beat()
                            pacer.account(n, rate)
                        try:
                            s.close()
                        except OSError:
                            pass

                else:  # udp
                    addr = (cfg.net_host, cfg.net_udp_port)
                    for _ in range(rng.randrange(20, 400)):
                        if ctl.should_stop():
                            break
                        size = rng.choice([64, 512, 1400, 8192, 32768])
                        head = b"R" if rng.random() < 0.25 else b"x"
                        try:
                            n = udp.sendto(head + payload[: size - 1], addr)
                            moved += n
                            ctl.beat()
                            pacer.account(n, rate)
                        except BlockingIOError:
                            ctl.sleep(0.002)
                        except OSError:
                            break
                        try:
                            while True:
                                moved += len(udp.recv(65535))
                        except (BlockingIOError, OSError):
                            pass

            except (OSError, ValueError):
                close_all()
                ctl.sleep(rng.uniform(0.05, 0.4))

            # recycle a connection now and then so nothing is truly long-lived
            if conns and rng.random() < 0.06:
                s = conns.pop(rng.randrange(len(conns)))
                try:
                    s.close()
                except OSError:
                    pass

            now = perf()
            if now - last_report >= 0.5:
                ctl.beat(moved / (now - last_report), float(conn_count), force=True)
                moved = 0.0
                last_report = now
            else:
                ctl.beat()
    finally:
        close_all()
        try:
            udp.close()
        except OSError:
            pass
