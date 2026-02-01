#!/bin/sh
set -eu

LOG_DIR="${LOG_DIR:-/logs}"
CONTAINERS="${LOGTAP_CONTAINERS:-reverse-proxy acme redis nats postgres nats-init frontend-a backend-a frontend-b backend-b}"

mkdir -p "$LOG_DIR"
echo "[logtap] log dir: $LOG_DIR"
echo "[logtap] containers: $CONTAINERS"

follow_one() {
  name="$1"
  out="$LOG_DIR/${name}.log"
  touch "$out" || true

  while true; do
    if docker inspect "$name" >/dev/null 2>&1; then
      since="10s"
      if [ ! -s "$out" ]; then
        since="0"
      fi
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

