#!/bin/sh
set -eu

LOG_DIR="${LOG_DIR:-/logs}"
CONTAINERS="${LOGTAP_CONTAINERS:-reverse-proxy acme redis nats postgres nats-init frontend-a backend-a frontend-b backend-b}"
# By default, do NOT backfill full container history into an empty file.
# To intentionally backfill, set LOGTAP_SINCE_EMPTY=0 (or a larger window like 1h).
SINCE_DEFAULT="${LOGTAP_SINCE:-10s}"
SINCE_EMPTY="${LOGTAP_SINCE_EMPTY:-10s}"

mkdir -p "$LOG_DIR"
echo "[logtap] log dir: $LOG_DIR"
echo "[logtap] containers: $CONTAINERS"
echo "[logtap] since(default): $SINCE_DEFAULT"
echo "[logtap] since(empty): $SINCE_EMPTY"

follow_one() {
  name="$1"
  out="$LOG_DIR/${name}.log"
  touch "$out" || true

  while true; do
    if docker inspect "$name" >/dev/null 2>&1; then
      since="$SINCE_DEFAULT"
      if [ ! -s "$out" ]; then since="$SINCE_EMPTY"; fi
      echo "[logtap] follow $name (since=$since) -> $out"
      # docker logs exits when container restarts/dies; loop re-attaches.
      docker logs --timestamps --since "$since" -f "$name" >>"$out" 2>&1 || true
    else
      echo "[logtap] container not found yet: $name"
    fi
    sleep 1
  done
}

for c in $CONTAINERS; do
  follow_one "$c" &
done

wait
