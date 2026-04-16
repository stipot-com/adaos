#!/usr/bin/env sh
set -eu

BASE_DIR="${ADAOS_BASE_DIR:-$HOME/.adaos}"
RUNTIME_JSON="$BASE_DIR/state/supervisor/runtime.json"
LAST_RESULT_JSON="$BASE_DIR/state/core_update/last_result.json"
STATUS_JSON="$BASE_DIR/state/core_update/status.json"
OUT_DIR="${1:-/tmp/adaos-yjs-burst-$(date +%Y%m%d-%H%M%S)}"
SAMPLE_SEC="${SAMPLE_SEC:-5}"
SAMPLE_COUNT="${SAMPLE_COUNT:-24}"
PYSPY_DURATION="${PYSPY_DURATION:-30}"

mkdir -p "$OUT_DIR"

log() {
  printf '%s\n' "$*" >&2
}

capture() {
  name="$1"
  shift
  {
    printf '+'
    for arg in "$@"; do
      printf ' %s' "$arg"
    done
    printf '\n'
    "$@"
  } >"$OUT_DIR/$name" 2>&1 || true
}

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  log "python/python3 not found"
  exit 1
fi

PID="$("$PYTHON_BIN" - "$RUNTIME_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
pid = data.get("managed_pid") or data.get("pid")
print(str(pid or ""))
PY
)"

if [ -z "$PID" ]; then
  log "managed_pid not found in $RUNTIME_JSON"
  exit 1
fi

log "Writing diagnostics to $OUT_DIR for PID $PID"

capture runtime.json cat "$RUNTIME_JSON"
capture last_result.json cat "$LAST_RESULT_JSON"
capture status.json cat "$STATUS_JSON"
capture ps.txt ps -o pid,ppid,%cpu,%mem,rss,vsz,nlwp,etime,cmd -p "$PID"
capture proc_status.txt cat "/proc/$PID/status"
capture proc_limits.txt cat "/proc/$PID/limits"
capture proc_smaps_rollup.txt cat "/proc/$PID/smaps_rollup"
capture pmap_x.txt pmap -x "$PID"
capture task_count.txt sh -c "ls \"/proc/$PID/task\" | wc -l"
capture fd_count.txt sh -c "ls \"/proc/$PID/fd\" | wc -l"
capture top_threads.txt ps -T -p "$PID" -o pid,tid,pcpu,pmem,rss,vsz,etime,comm --sort=-rss
capture sockets.txt sh -c "ss -tpn | grep -E ':(8777|8778|7422)([^0-9]|$)' || true"
capture journalctl.txt journalctl -u adaos.service -n 400 --no-pager

if command -v lsof >/dev/null 2>&1; then
  capture lsof.txt lsof -n -P -p "$PID"
fi

if command -v py-spy >/dev/null 2>&1; then
  capture pyspy_dump.txt py-spy dump --pid "$PID"
  capture pyspy_record.log py-spy record -o "$OUT_DIR/pyspy.svg" --pid "$PID" --duration "$PYSPY_DURATION"
fi

if command -v gcore >/dev/null 2>&1; then
  capture gcore.log gcore -o "$OUT_DIR/gcore" "$PID"
fi

{
  printf 'pid=%s\n' "$PID"
  printf 'sample_sec=%s\n' "$SAMPLE_SEC"
  printf 'sample_count=%s\n' "$SAMPLE_COUNT"
  i=1
  while [ "$i" -le "$SAMPLE_COUNT" ]; do
    printf '\n=== sample %s %s ===\n' "$i" "$(date -Is)"
    ps -o pid,ppid,%cpu,%mem,rss,vsz,nlwp,etime,cmd -p "$PID" || true
    printf '\n-- /proc/%s/status --\n' "$PID"
    grep -E '^(VmRSS|VmSize|Threads|FDSize):' "/proc/$PID/status" || true
    printf '\n-- thread count --\n'
    ls "/proc/$PID/task" | wc -l || true
    printf '\n-- fd count --\n'
    ls "/proc/$PID/fd" | wc -l || true
    printf '\n-- sockets --\n'
    ss -tpn | grep -E ':(8777|8778|7422)([^0-9]|$)' || true
    sleep "$SAMPLE_SEC"
    i=$((i + 1))
  done
} >"$OUT_DIR/samples.txt" 2>&1 || true

cat >"$OUT_DIR/README.txt" <<EOF
AdaOS YJS burst profile bundle

PID: $PID
Runtime JSON: $RUNTIME_JSON
Output directory: $OUT_DIR

Most useful files:
- ps.txt
- proc_status.txt
- proc_smaps_rollup.txt
- pmap_x.txt
- top_threads.txt
- sockets.txt
- samples.txt
- journalctl.txt
- pyspy_dump.txt / pyspy.svg (if py-spy was installed)
- gcore.log + gcore.* (if gcore was installed)

If the process is still exploding, repeat this script during the growth window and compare:
- task_count.txt / top_threads.txt
- proc_smaps_rollup.txt
- sockets.txt
- samples.txt
EOF

log "Profile bundle ready: $OUT_DIR"
