#!/usr/bin/env bash
# tools/bootstrap.sh — bootstrap via venv + pip (Linux/macOS)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

VENV_DIR=".venv"
VENV_ACTIVATE=".venv/bin/activate"

JOIN_CODE=""
ROLE=""
INSTALL_SERVICE="auto" # auto|always|never
SERVE_HOST="127.0.0.1"
SERVE_PORT="8777"
CONTROL_PORT="8777"
ROOT_URL="https://api.inimatic.com"
REV="rev2026"
NO_VOICE="0"

log()  { printf '\033[36m[*] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
fail() { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

ORIG_ARGS=("$@")

detect_venv_activate() {
  if [[ -f "${VENV_DIR}/bin/activate" ]]; then
    VENV_ACTIVATE="${VENV_DIR}/bin/activate"
    return 0
  fi
  if [[ -f "${VENV_DIR}/Scripts/activate" ]]; then
    # Not expected on Linux/macOS, but helps on Git-Bash style envs.
    VENV_ACTIVATE="${VENV_DIR}/Scripts/activate"
    return 0
  fi
  return 1
}

venv_is_usable() {
  [[ -d "${VENV_DIR}" ]] || return 1
  detect_venv_activate || return 1
  return 0
}

http_get() {
  local url="$1"
  local header="${2:-}"
  if have curl; then
    if [[ -n "$header" ]]; then
      curl -fsS -H "$header" "$url"
    else
      curl -fsS "$url"
    fi
    return $?
  fi
  if have wget; then
    if [[ -n "$header" ]]; then
      wget -qO- --header="$header" "$url"
    else
      wget -qO- "$url"
    fi
    return $?
  fi
  return 1
}

read_env_type_from_file() {
  local path="$1"
  [[ -f "$path" ]] || return 0
  sed -n 's/^[[:space:]]*ENV_TYPE[[:space:]]*=[[:space:]]*//p' "$path" \
    | head -n 1 \
    | tr -d '\r' \
    | tr -d '"' \
    | tr -d "'" \
    | xargs \
    || true
}

resolve_adaos_base_dir() {
  if [[ -n "${ADAOS_BASE_DIR:-}" ]]; then
    printf '%s' "${ADAOS_BASE_DIR}"
    return 0
  fi
  local env_type="${ENV_TYPE:-}"
  if [[ -z "${env_type:-}" ]]; then
    env_type="$(read_env_type_from_file ".env" || true)"
  fi
  env_type="${env_type:-dev}"
  if [[ "$env_type" == "dev" ]]; then
    printf '%s' "$PWD/.adaos"
    return 0
  fi
  printf '%s' "$HOME/.adaos"
}

fallback_to_uv() {
  local reason="$1"
  warn "$reason"
  warn "Falling back to uv-based bootstrap (no root, no system Python required)..."
  exec "./tools/bootstrap_uv.sh" "${ORIG_ARGS[@]}"
}

print_next_steps() {
  local serve_host="$1"
  local serve_port="$2"
  local role="$3"
  local deep_link="$4"
  local connected_to_hub="$5"

  echo
  ok "Bootstrap completed."
  echo
  echo "Next steps:"
  if [[ -n "${deep_link:-}" ]]; then
    echo "  1) Telegram: open and confirm pairing:"
    echo "     ${deep_link}"
  else
    echo "  1) Telegram pairing:"
    echo "     python -m adaos dev telegram"
  fi
  echo "  2) Owner browser:"
  echo "     python -m adaos dev root login"
  echo "     Then open https://app.inimatic.com/owner-auth and enter the code."
  echo "  3) Start/stop/restart AdaOS API:"
  echo "     Start (foreground): python -m adaos api serve --host ${serve_host} --port ${serve_port}"
  echo "     Stop:              python -m adaos api stop"
  echo "     Restart:           python -m adaos api restart"
  echo "  4) Web UI:"
  echo "     Open https://app.inimatic.com/ and connect to your local node (ports 8777/8778)."
  if [[ "${role:-}" == "member" ]]; then
    echo "  5) Member → hub connectivity:"
    echo "     connected_to_hub=${connected_to_hub:-unknown}"
    echo "     Details: python -m adaos node status"
  fi
  echo
  echo "Docs:"
  echo "  https://stipot-com.github.io/adaos/"
}

install_voice_deps() {
  local py="$1"
  [[ "${NO_VOICE:-0}" == "1" ]] && return 0
  log "Installing voice deps (Rasa)..."
  if "$py" -c "import rasa; print(getattr(rasa, '__version__', ''))" >/dev/null 2>&1; then
    ok "Rasa already installed"
    return 0
  fi
  set +e
  "$py" -m pip install "rasa==3.6.20"
  local rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    warn "Rasa install failed. Continue without voice NLU (use --no_voice to skip)."
  else
    ok "Rasa installed"
  fi
}

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

  return 1
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
    "$SHELL" --rcfile <(printf 'source %s\nprintf "%s\n"\n' "${VENV_ACTIVATE:-.venv/bin/activate}" "$help_text") -i
  else
    bash --rcfile <(printf 'source %s\nprintf "%s\n"\n' "${VENV_ACTIVATE:-.venv/bin/activate}" "$help_text") -i
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
    --no_voice|--no-voice) NO_VOICE="1"; shift ;;
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
  --no_voice            Skip voice/NLU deps (Rasa)
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
if ! choose_python_311; then
  fallback_to_uv "Python 3.11 not found (or not on PATH)."
fi

log "Checking Python venv support..."
if ! "$PY_BIN" -c "import venv, ensurepip" >/dev/null 2>&1; then
  warn "System Python cannot create venv with pip (missing venv/ensurepip)."
  warn "If you are on Debian/Ubuntu, try: sudo apt-get install -y python3.11-venv"
  fallback_to_uv "System Python venv support is missing."
fi

log "Creating venv (.venv)..."
if [[ -d "${VENV_DIR}" ]]; then
  if ! venv_is_usable; then
    warn "Existing ${VENV_DIR} looks incomplete (missing activate script); removing..."
    rm -rf "${VENV_DIR}"
  else
    VENV_VER="$(. "$VENV_ACTIVATE" >/dev/null 2>&1 && python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' || true)"
    if [[ -n "${VENV_VER:-}" && "$VENV_VER" != "$PY_VER" ]]; then
      warn "Existing ${VENV_DIR} is $VENV_VER; recreating for $PY_VER..."
      rm -rf "${VENV_DIR}"
    fi
  fi
fi
if [[ ! -d "${VENV_DIR}" ]]; then
  set +e
  venv_out="$("$PY_BIN" -m venv "${VENV_DIR}" 2>&1)"
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "$venv_out" >&2
    fallback_to_uv "venv creation failed."
  fi
fi

log "Installing Python deps (editable)..."
if ! venv_is_usable; then
  warn "${VENV_DIR} was created but activate script is missing. Trying to recreate venv once..."
  rm -rf "${VENV_DIR}"
  set +e
  venv_out="$("$PY_BIN" -m venv "${VENV_DIR}" 2>&1)"
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "$venv_out" >&2
    warn "If you are on Debian/Ubuntu, try: sudo apt-get install -y python3.11-venv"
    fallback_to_uv "venv recreation failed."
  fi
  if ! venv_is_usable; then
    warn "If you are on Debian/Ubuntu, try: sudo apt-get install -y python3.11-venv"
    fallback_to_uv "Broken venv layout."
  fi
fi
. "$VENV_ACTIVATE"
python -m pip install -U pip >/dev/null
python -m pip install -e .[dev] || fail "pip install -e .[dev] failed"
install_voice_deps "python"

log "Bootstrapping .env..."
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    ok ".env created from .env.example"
  elif [[ -f .env.sample ]]; then
    cp .env.sample .env
    ok ".env created from .env.sample"
  elif [[ -f .env.prod.sample ]]; then
    cp .env.prod.sample .env
    ok ".env created from .env.prod.sample"
  else
    warn "No .env found and no .env.example/.env.sample/.env.prod.sample present"
  fi
fi

if [[ -z "${ENV_TYPE:-}" ]]; then
  ENV_TYPE="$(read_env_type_from_file ".env" || true)"
fi
export ENV_TYPE="${ENV_TYPE:-dev}"

ADAOS_BASE_DIR="$(resolve_adaos_base_dir)"
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
connected_to_hub=""
while [[ $(date +%s) -lt $deadline ]]; do
  if ready_json="$(http_get "${control_base}/api/node/status" "X-AdaOS-Token: ${token}" 2>/dev/null)"; then
    if python -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); exp=sys.argv[1]; ok=bool(d.get("ready")); nid=str(d.get("node_id") or ""); raise SystemExit(0 if (ok and (not exp or nid==exp)) else 1)' "$expected_node_id" <<<"$ready_json" >/dev/null 2>&1; then
      ok "READY: ${ready_json}"
      connected_to_hub="$(python -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); v=d.get("connected_to_hub"); print("" if v is None else str(bool(v)).lower())' <<<"$ready_json" 2>/dev/null || true)"
      break
    fi
  fi
  sleep 2
done

deep_link=""
log "Generating Telegram pairing link..."
set +e
tg_out="$(python -m adaos dev telegram 2>&1)"
tg_rc=$?
set -e
if [[ $tg_rc -eq 0 ]]; then
  deep_link="$(printf '%s\n' "$tg_out" | sed -n 's/^[[:space:]]*deep_link:[[:space:]]*//p' | head -n 1 | tr -d '\r' || true)"
fi
if [[ -z "${deep_link:-}" ]]; then
  warn "Telegram pairing link not generated automatically. Run: python -m adaos dev telegram"
fi

print_next_steps "$SERVE_HOST" "$SERVE_PORT" "$ROLE" "$deep_link" "$connected_to_hub"
printf "\nTo activate venv:\n  source %s\n\n" "${VENV_ACTIVATE:-.venv/bin/activate}"
open_subshell_help
