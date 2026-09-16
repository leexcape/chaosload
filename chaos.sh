#!/usr/bin/env bash
# chaos.sh -- start/stop/inspect the chaosload generator.
#
#   ./chaos.sh start [chaosload args...]   background, survives logout
#   ./chaos.sh run   [chaosload args...]   foreground (Ctrl-C to stop)
#   ./chaos.sh stop | restart | status | watch | logs [-f] | top | clean
#
# Anything after the sub-command is passed straight through to
# `python3 -m chaosload` (see --help for the full list).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/run"
PID_FILE="$RUN_DIR/chaosload.pid"
LOG_FILE="$RUN_DIR/chaosload.log"
ERR_FILE="$RUN_DIR/chaosload.err"
STATUS_FILE="$RUN_DIR/status.json"
PYTHON="${PYTHON:-python3}"

mkdir -p "$RUN_DIR"

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(head -n1 "$PID_FILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  echo "$pid"
}

# Workers are renamed via prctl, so match on the process name, not the
# command line: -f would both miss them and risk matching our own caller.
sweep_orphans() {
  pkill -9 '^chaos-(cpu|mem|disk|net)' 2>/dev/null || true
}

cmd_start() {
  if pid="$(running_pid)"; then
    echo "already running (pid $pid) -- use './chaos.sh restart' or 'stop'"
    return 1
  fi
  cd "$ROOT"
  setsid nohup "$PYTHON" -m chaosload --quiet "$@" >/dev/null 2>"$ERR_FILE" &
  local started=$!
  for _ in $(seq 1 60); do
    if pid="$(running_pid)"; then
      echo "chaosload started (pid $pid)"
      echo "  log:    $LOG_FILE"
      echo "  status: ./chaos.sh status     stop: ./chaos.sh stop"
      return 0
    fi
    kill -0 "$started" 2>/dev/null || break
    sleep 0.5
  done
  echo "failed to start; last errors:" >&2
  tail -n 20 "$ERR_FILE" >&2 || true
  return 1
}

cmd_run() {
  cd "$ROOT"
  exec "$PYTHON" -m chaosload "$@"
}

cmd_stop() {
  local pid
  if ! pid="$(running_pid)"; then
    echo "not running"
    # sweep up anything orphaned by a hard kill of a previous supervisor
    sweep_orphans
    return 0
  fi
  echo -n "stopping pid $pid "
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 60); do
    kill -0 "$pid" 2>/dev/null || { echo "-- stopped"; break; }
    echo -n "."
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo " -- did not exit, sending SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi
  sweep_orphans
  rm -rf "$ROOT/.chaos-scratch"
  echo "stopped"
}

cmd_status() {
  if pid="$(running_pid)"; then
    echo "state: RUNNING (pid $pid)"
  else
    echo "state: stopped"
  fi
  [[ -f "$STATUS_FILE" ]] || { echo "(no status file yet)"; return 0; }
  "$PYTHON" - "$STATUS_FILE" <<'PY'
import json, sys, time
try:
    st = json.load(open(sys.argv[1]))
except Exception as exc:
    print("status unreadable:", exc); raise SystemExit(0)
if st.get("state") == "stopped":
    print(f"last run: {st.get('uptime_s', 0)}s, stats={st.get('stats')}"); raise SystemExit(0)
def hb(n):
    n = float(n)
    for u in "B K M G T".split():
        if abs(n) < 1024 or u == "T": return f"{n:.1f}{u}"
        n /= 1024
age = time.time() - st.get("ts", 0)
lv = st["levels"]
print(f"updated: {age:.0f}s ago   uptime: {st['uptime_s']:.0f}s   profile: {st['profile']} (target mean {st['target_mean']})")
print(f"levels:  cpu {lv['cpu']:.2f}  mem {lv['mem']:.2f}  disk {lv['disk']:.2f}  net {lv['net']:.2f}   aux_scale {st.get('aux_scale', 1)}")
print(f"regimes: {st['regimes']}")
c, m, d, n = st["cpu"], st["mem"], st["disk"], st["net"]
print(f"cpu:     {c['util']*100:.0f}% of target {c.get('target',0)*100:.0f}%  duty {c['duty']:.2f}  cores busy {c['cores_busy']:.2f}  load {c['loadavg']}")
print(f"memory:  {m['used_frac']*100:.0f}% used  ours {hb(m['ours'])}  available {hb(m['available'])} of {hb(m['total'])}")
print(f"disk:    workers {hb(d['worker_bytes_per_s'])}/s  device r {hb(d['dev_read_bytes_per_s'])}/s w {hb(d['dev_write_bytes_per_s'])}/s  held {hb(d['held_bytes'])}")
print(f"network: workers {hb(n['worker_bytes_per_s'])}/s  loopback {hb(n['lo_bytes_per_s'])}/s  connections {n['connections']} open, {n.get('connections_opened', 0)} opened")
print("workers: " + "  ".join(f"{w['name']}{'' if w['alive'] else '(dead)'}"
                              + (f"/r{w['restarts']}" if w['restarts'] else "") for w in st["workers"]))
print(f"stats:   {st['stats']}")
PY
}

cmd_watch() {
  while true; do
    clear
    cmd_status
    sleep "${1:-2}"
  done
}

cmd_logs() {
  [[ -f "$LOG_FILE" ]] || { echo "no log yet at $LOG_FILE"; return 0; }
  if [[ "${1:-}" == "-f" ]]; then tail -f "$LOG_FILE"; else tail -n "${1:-40}" "$LOG_FILE"; fi
}

cmd_top() {
  top -b -n1 -o %CPU 2>/dev/null | head -n 20
}

cmd_clean() {
  if running_pid >/dev/null; then echo "still running -- stop it first" >&2; return 1; fi
  rm -rf "$ROOT/.chaos-scratch"
  rm -f "$STATUS_FILE" "$PID_FILE" "$ERR_FILE" "$LOG_FILE"*
  echo "cleaned scratch, logs and status"
}

case "${1:-status}" in
  start)   shift; cmd_start "$@" ;;
  run)     shift; cmd_run "$@" ;;
  stop)    cmd_stop ;;
  restart) shift; cmd_stop; sleep 1; cmd_start "$@" ;;
  status)  cmd_status ;;
  watch)   shift; cmd_watch "${1:-2}" ;;
  logs)    shift; cmd_logs "${1:-40}" ;;
  top)     cmd_top ;;
  clean)   cmd_clean ;;
  -h|--help|help)
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    echo; "$PYTHON" -m chaosload --help ;;
  *) echo "unknown command: $1 (try: ./chaos.sh help)" >&2; exit 2 ;;
esac
