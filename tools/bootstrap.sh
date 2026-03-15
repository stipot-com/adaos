#!/usr/bin/env bash
# tools/bootstrap.sh — bootstrap via venv + pip (Linux/macOS)
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
fail() { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

py_is_311() {
  local bin="$1"
  "$bin" -c 'import sys; raise SystemExit(0 if (sys.version_info[0], sys.version_info[1]) == (3, 11) else 1)' \
    >/dev/null 2>&1
}

choose_python_311() {
  local cands=()
  if [[ -n "${ADAOS_PYTHON:-}" ]]; then
    cands+=("$ADAOS_PYTHON")
  fi
  cands+=(python3.11 python3 python)

  for c in "${cands[@]}"; do
    have "$c" || continue
    local p
    p="$(command -v "$c")"
    if py_is_311 "$p"; then
      PY_BIN="$p"
      PY_VER="3.11"
      log "Using Python 3.11 -> ${PY_BIN}"
      return 0
    fi
  done

  fail "Python 3.11 not found. Install Python 3.11 and re-run (or set ADAOS_PYTHON)."
}

smart_npm_install() {
  if have pnpm; then
    pnpm install
    USED_PKG_CMD="pnpm install"
    return
  fi
  if [[ -f package-lock.json ]]; then
    if npm ci; then
      USED_PKG_CMD="npm ci"
    else
      warn "npm ci failed; falling back to npm install..."
      npm install
      USED_PKG_CMD="npm install"
    fi
  else
    npm install
    USED_PKG_CMD="npm install"
  fi
}

open_subshell_help() {
  [[ "${BOOTSTRAP_OPEN_SUBSHELL:-0}" != "1" ]] && return 0
  local help_text
  read -r -d '' help_text <<'EOF' || true
READY.

Next steps:
  1) API:
     python -m adaos api serve --host 127.0.0.1 --port 8777 --reload
  2) Backend (Inimatic):
     cd src/adaos/integrations/inimatic
     npm run start:api-dev
  3) Frontend (Inimatic):
     cd src/adaos/integrations/inimatic
     npm i
     npm run start
EOF

  if [[ -n "${SHELL:-}" && -x "$SHELL" ]]; then
    "$SHELL" --rcfile <(printf 'source .venv/bin/activate\nprintf "%s\n"\n' "$help_text") -i
  else
    bash --rcfile <(printf 'source .venv/bin/activate\nprintf "%s\n"\n' "$help_text") -i
  fi
}

cd "$(dirname "$0")/.." || fail "cannot cd to repo root"

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
Usage: tools/bootstrap.sh [options]
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
    *) fail "Unknown arg: $1 (try --help)" ;;
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

log "Choosing Python 3.11..."
choose_python_311

log "Creating venv (.venv)..."
if [[ -d .venv ]]; then
  VENV_VER="$(. .venv/bin/activate >/dev/null 2>&1 && python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' || true)"
  if [[ -n "${VENV_VER:-}" && "$VENV_VER" != "$PY_VER" ]]; then
    warn "Existing .venv is $VENV_VER; recreating for $PY_VER..."
    rm -rf .venv
  fi
fi
[[ -d .venv ]] || "$PY_BIN" -m venv .venv

log "Installing Python deps (editable)..."
. .venv/bin/activate
python -m pip install -U pip >/dev/null
python -m pip install -e .[dev] || fail "pip install -e .[dev] failed"

log "Bootstrapping .env..."
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

export ENV_TYPE="${ENV_TYPE:-dev}"

ADAOS_BASE_DIR="$(pwd)/.adaos"
mkdir -p "$ADAOS_BASE_DIR"
export ADAOS_BASE_DIR

log "Installing default webspace content (adaos install)..."
if ! python -m adaos install; then
  warn "adaos install failed (check output above)"
fi

export ADAOS_REV="$REV"

if [[ -n "${JOIN_CODE:-}" ]]; then
  log "Joining subnet via join-code..."
  if ! python -m adaos node join --code "$JOIN_CODE" --root "$ROOT_URL"; then
    warn "adaos node join failed (check output above)"
  fi
fi

if [[ -n "${ROLE:-}" ]]; then
  log "Setting node role: $ROLE"
  if ! python -m adaos node role set --role "$ROLE"; then
    warn "adaos node role set failed (check output above)"
  fi
fi

control_base="http://${SERVE_HOST}:${CONTROL_PORT}"
token="$(
  python -c 'import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; print(d.get("token") or "dev-local-token")' \
    "${ADAOS_BASE_DIR}/node.yaml" 2>/dev/null || echo "dev-local-token"
)"
expected_node_id="$(
  python -c 'import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; print(d.get("node_id") or "")' \
    "${ADAOS_BASE_DIR}/node.yaml" 2>/dev/null || echo ""
)"

log "Starting AdaOS API (${SERVE_HOST}:${SERVE_PORT}) ..."
service_installed=0
if [[ "$INSTALL_SERVICE" != "never" ]]; then
  if python -m adaos autostart enable --host "$SERVE_HOST" --port "$SERVE_PORT" >/dev/null 2>&1; then
    service_installed=1
    ok "Autostart installed (adaos autostart enable)"
  else
    warn "autostart enable failed; will fallback to background run"
  fi
fi

if [[ "$service_installed" != "1" || "$INSTALL_SERVICE" == "never" ]]; then
  nohup python -m adaos api serve --host "$SERVE_HOST" --port "$SERVE_PORT" >/dev/null 2>&1 & disown || true
fi

log "Waiting for ready=true ..."
deadline=$(( $(date +%s) + 120 ))
ready_json=""
while [[ $(date +%s) -lt $deadline ]]; do
  if ready_json="$(curl -fsS -H "X-AdaOS-Token: ${token}" "${control_base}/api/node/status" 2>/dev/null)"; then
    if python -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); exp=sys.argv[1]; ok=bool(d.get("ready")); nid=str(d.get("node_id") or ""); raise SystemExit(0 if (ok and (not exp or nid==exp)) else 1)' "$expected_node_id" <<<"$ready_json" >/dev/null 2>&1; then
      ok "READY: ${ready_json}"
      break
    fi
  fi
  sleep 2
done

ok "Bootstrap completed."
printf "\nTo activate venv:\n  source .venv/bin/activate\n\n"
open_subshell_help
