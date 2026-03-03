#!/usr/bin/env bash
# tools/bootstrap_uv.sh — bootstrap via uv (Linux/macOS)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

JOIN_CODE=""
ROLE=""
INSTALL_SERVICE="auto" # auto|always|never
SERVE_HOST="127.0.0.1"
SERVE_PORT="8777"
CONTROL_PORT="8777"
ROOT_URL="https://api.inimatic.com"
REV="rev2026"

log()  { printf '\033[36m[*] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Repo root
cd "$(dirname "$0")/.." || die "cannot cd to repo root"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --join-code) JOIN_CODE="${2:-}"; shift 2 ;;
    --role) ROLE="${2:-}"; shift 2 ;;
    --install-service) INSTALL_SERVICE="${2:-}"; shift 2 ;;
    --serve-host) SERVE_HOST="${2:-}"; shift 2 ;;
    --serve-port) SERVE_PORT="${2:-}"; shift 2 ;;
    --control-port) CONTROL_PORT="${2:-}"; shift 2 ;;
    --root-url) ROOT_URL="${2:-}"; shift 2 ;;
    --rev) REV="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: tools/bootstrap_uv.sh [options]
  --join-code CODE
  --role hub|member
  --install-service auto|always|never
  --serve-host HOST
  --serve-port PORT
  --control-port PORT
  --root-url URL
  --rev REV
EOF
      exit 0
      ;;
    *) die "Unknown arg: $1 (try --help)" ;;
  esac
done

if [[ -n "${JOIN_CODE:-}" ]]; then
  if [[ "${SERVE_PORT:-}" == "8777" ]]; then
    SERVE_PORT="8778"
  fi
  if [[ "${CONTROL_PORT:-}" == "8777" ]]; then
    CONTROL_PORT="$SERVE_PORT"
  fi
fi

if [[ -z "${ROLE:-}" ]]; then
  if [[ -n "${JOIN_CODE:-}" ]]; then
    ROLE="member"
  else
    ROLE="hub"
  fi
fi

# 1) uv
if ! have uv; then
  log "Installing uv..."
  curl -fsSL https://astral.sh/uv/install.sh | sh || die "uv install failed"
  export PATH="$HOME/.local/bin:$PATH"
fi

# 1.5) Python 3.11 only (uv-managed)
log "Ensuring Python 3.11..."
uv python install 3.11 || die "uv python install 3.11 failed"
export UV_PYTHON="3.11"

# 2) Python deps
if [[ -f uv.lock ]]; then
  log "Syncing environment from uv.lock..."
  set +e
  uv sync --locked
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    warn "uv sync --locked failed, refreshing lock..."
    uv lock || die "uv lock failed"
    uv sync || die "uv sync failed"
  fi
else
  log "Locking and syncing environment..."
  uv lock || die "uv lock failed"
  uv sync || die "uv sync failed"
fi
ok "Python environment ready"

# 4) .env
if [[ ! -f .env ]]; then
  if [[ -f .env.sample ]]; then
    cp .env.sample .env
    ok ".env created from .env.sample"
  elif [[ -f .env.prod.sample ]]; then
    cp .env.prod.sample .env
    ok ".env created from .env.prod.sample"
  else
    warn "No .env found and no .env.sample/.env.prod.sample present"
  fi
fi

# 5) Convenience PATH for current shell session
if [[ -d ".venv/bin" ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

# 6) Default webspace content (scenarios + skills) via built-in `adaos install`
export ENV_TYPE="${ENV_TYPE:-dev}"
ADAOS_BASE_DIR="$PWD/.adaos"
mkdir -p "$ADAOS_BASE_DIR"
export ADAOS_BASE_DIR

log "Installing default webspace content (adaos install)..."
if ! uv run adaos install; then
  warn "adaos install failed (check output above)"
fi

export ADAOS_REV="$REV"

if [[ -n "${JOIN_CODE:-}" ]]; then
  log "Joining subnet via join-code..."
  if ! uv run adaos node join --code "$JOIN_CODE" --root "$ROOT_URL"; then
    warn "adaos node join failed (check output above)"
  fi
fi

if [[ -n "${ROLE:-}" ]]; then
  log "Setting node role: $ROLE"
  if ! uv run adaos node role set --role "$ROLE"; then
    warn "adaos node role set failed (check output above)"
  fi
fi

control_base="http://${SERVE_HOST}:${CONTROL_PORT}"
token="$(
  uv run python -c 'import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; print(d.get("token") or "dev-local-token")' \
    "${ADAOS_BASE_DIR}/node.yaml" 2>/dev/null || echo "dev-local-token"
)"
expected_node_id="$(
  uv run python -c 'import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; print(d.get("node_id") or "")' \
    "${ADAOS_BASE_DIR}/node.yaml" 2>/dev/null || echo ""
)"

log "Starting AdaOS API (${SERVE_HOST}:${SERVE_PORT}) ..."
service_installed=0
if [[ "$INSTALL_SERVICE" != "never" ]]; then
  if uv run adaos autostart enable --host "$SERVE_HOST" --port "$SERVE_PORT" >/dev/null 2>&1; then
    service_installed=1
    ok "Autostart installed (adaos autostart enable)"
  else
    warn "autostart enable failed; will fallback to background run"
  fi
fi
if [[ "$service_installed" != "1" || "$INSTALL_SERVICE" == "never" ]]; then
  nohup uv run adaos api serve --host "$SERVE_HOST" --port "$SERVE_PORT" >/dev/null 2>&1 & disown || true
fi

log "Waiting for ready=true ..."
deadline=$(( $(date +%s) + 120 ))
ready_json=""
while [[ $(date +%s) -lt $deadline ]]; do
  if ready_json="$(curl -fsS -H "X-AdaOS-Token: ${token}" "${control_base}/api/node/status" 2>/dev/null)"; then
    if uv run python -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); exp=sys.argv[1]; ok=bool(d.get("ready")); nid=str(d.get("node_id") or ""); raise SystemExit(0 if (ok and (not exp or nid==exp)) else 1)' "$expected_node_id" <<<"$ready_json" >/dev/null 2>&1; then
      ok "READY: ${ready_json}"
      break
    fi
  fi
  sleep 2
done

echo
ok "Bootstrap completed."
echo "Quick checks:"
echo "  uv --version"
echo "  uv run python -V"
echo "  uv run adaos --help"
echo
echo "To run the API:"
echo "  uv run adaos api serve --host 127.0.0.1 --port 8777 --reload"
