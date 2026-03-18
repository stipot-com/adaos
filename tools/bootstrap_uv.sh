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
NO_VOICE="0"

log()  { printf '\033[36m[*] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

fetch_to_stdout() {
  local url="$1"
  if have curl; then
    curl -fsSL "$url"
    return $?
  fi
  if have wget; then
    wget -qO- "$url"
    return $?
  fi
  die "Neither curl nor wget is available (required to download uv)."
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
  # Minimal, robust parsing: ENV_TYPE=<value> (ignores comments and quotes).
  sed -n 's/^[[:space:]]*ENV_TYPE[[:space:]]*=[[:space:]]*//p' "$path" \
    | head -n 1 \
    | tr -d '\r' \
    | tr -d '"' \
    | tr -d "'" \
    | xargs \
    || true
}

resolve_adaos_base_dir() {
  # Priority:
  #  1) user-provided ADAOS_BASE_DIR
  #  2) ENV_TYPE=dev -> repo-local .adaos
  #  3) otherwise -> ~/.adaos
  if [[ -n "${ADAOS_BASE_DIR:-}" ]]; then
    printf '%s' "${ADAOS_BASE_DIR}"
    return 0
  fi
  local env_type="${ENV_TYPE:-}"
  if [[ -z "${env_type:-}" ]]; then
    env_type="$(read_env_type_from_file ".env" || true)"
  fi
  env_type="${env_type:-prod}"
  if [[ "$env_type" == "dev" ]]; then
    printf '%s' "$PWD/.adaos"
    return 0
  fi
  printf '%s' "$HOME/.adaos"
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
    echo "     ${ADAOS_PY} -m adaos dev telegram"
  fi
  echo "  2) Owner browser:"
  echo "     ${ADAOS_PY} -m adaos dev root login"
  echo "     Then open https://app.inimatic.com/owner-auth and enter the code."
  echo "  3) Start/stop/restart AdaOS API:"
  echo "     Start (foreground): ${ADAOS_PY} -m adaos api serve --host ${serve_host} --port ${serve_port}"
  echo "     Stop:              ${ADAOS_PY} -m adaos api stop"
  echo "     Restart:           ${ADAOS_PY} -m adaos api restart"
  echo "  4) Web UI:"
  echo "     Open https://app.inimatic.com/ and connect to your local node (ports 8777/8778)."
  if [[ "${role:-}" == "member" ]]; then
    echo "  5) Member → hub connectivity:"
    echo "     connected_to_hub=${connected_to_hub:-unknown}"
    echo "     Details: ${ADAOS_PY} -m adaos node status"
  fi
  echo
  echo "Docs:"
  echo "  https://stipot-com.github.io/adaos/"
}

install_voice_deps() {
  local py="$1"
  [[ "${NO_VOICE:-0}" == "1" ]] && return 0
  log "Installing voice deps (Rasa)..."
  # Rasa 3.6.x does not support Python 3.11+. Avoid noisy pip failures on servers.
  local py_ver=""
  py_ver="$("$py" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
  case "$py_ver" in
    3.11|3.12|3.13)
      warn "Skipping voice NLU deps: rasa==3.6.20 is not available for Python ${py_ver}."
      warn "If you need voice NLU, use Python 3.10 or run with --no_voice to silence this step."
      return 0
      ;;
  esac
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
    --no_voice|--no-voice) NO_VOICE="1"; shift ;;
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
  --no_voice            Skip voice/NLU deps (Rasa)
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
  fetch_to_stdout "https://astral.sh/uv/install.sh" | sh || die "uv install failed"
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

# Prefer invoking AdaOS via venv python -m adaos to avoid console-script wrapper issues.
ADAOS_PY="$PWD/.venv/bin/python"
if [[ ! -x "$ADAOS_PY" ]]; then
  die "Expected venv python at $ADAOS_PY (uv sync should have created .venv)"
fi

install_voice_deps "$ADAOS_PY"

# 4) .env
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

# 5) Convenience PATH for current shell session
if [[ -d ".venv/bin" ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

# 6) Default webspace content (scenarios + skills) via built-in `adaos install`
if [[ -z "${ENV_TYPE:-}" ]]; then
  ENV_TYPE="$(read_env_type_from_file ".env" || true)"
fi
export ENV_TYPE="${ENV_TYPE:-dev}"
ADAOS_BASE_DIR="$(resolve_adaos_base_dir)"
mkdir -p "$ADAOS_BASE_DIR"
export ADAOS_BASE_DIR

log "Installing default webspace content (adaos install)..."
if ! "$ADAOS_PY" -m adaos install; then
  warn "adaos install failed (check output above)"
fi

export ADAOS_REV="$REV"

if [[ -n "${JOIN_CODE:-}" ]]; then
  log "Joining subnet via join-code..."
  if ! "$ADAOS_PY" -m adaos node join --code "$JOIN_CODE" --root "$ROOT_URL"; then
    warn "adaos node join failed (check output above)"
  fi
fi

if [[ -n "${ROLE:-}" ]]; then
  log "Setting node role: $ROLE"
  if ! "$ADAOS_PY" -m adaos node role set --role "$ROLE"; then
    warn "adaos node role set failed (check output above)"
  fi
fi

control_base="http://${SERVE_HOST}:${CONTROL_PORT}"
token="$(
  "$ADAOS_PY" -c 'import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; print(d.get("token") or "dev-local-token")' \
    "${ADAOS_BASE_DIR}/node.yaml" 2>/dev/null || echo "dev-local-token"
)"
expected_node_id="$(
  "$ADAOS_PY" -c 'import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}; print(d.get("node_id") or "")' \
    "${ADAOS_BASE_DIR}/node.yaml" 2>/dev/null || echo ""
)"

log "Starting AdaOS API (${SERVE_HOST}:${SERVE_PORT}) ..."
service_installed=0
if [[ "$INSTALL_SERVICE" != "never" ]]; then
  if "$ADAOS_PY" -m adaos autostart enable --host "$SERVE_HOST" --port "$SERVE_PORT" >/dev/null 2>&1; then
    service_installed=1
    ok "Autostart installed (adaos autostart enable)"
  else
    warn "autostart enable failed; will fallback to background run"
  fi
fi
if [[ "$service_installed" != "1" || "$INSTALL_SERVICE" == "never" ]]; then
  nohup "$ADAOS_PY" -m adaos api serve --host "$SERVE_HOST" --port "$SERVE_PORT" >/dev/null 2>&1 & disown || true
fi

log "Waiting for ready=true ..."
deadline=$(( $(date +%s) + 120 ))
ready_json=""
connected_to_hub=""
while [[ $(date +%s) -lt $deadline ]]; do
  if ready_json="$(http_get "${control_base}/api/node/status" "X-AdaOS-Token: ${token}" 2>/dev/null)"; then
    if "$ADAOS_PY" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); exp=sys.argv[1]; ok=bool(d.get("ready")); nid=str(d.get("node_id") or ""); raise SystemExit(0 if (ok and (not exp or nid==exp)) else 1)' "$expected_node_id" <<<"$ready_json" >/dev/null 2>&1; then
      ok "READY: ${ready_json}"
      connected_to_hub="$("$ADAOS_PY" -c 'import json,sys; d=json.loads(sys.stdin.read() or \"{}\"); v=d.get(\"connected_to_hub\"); print(\"\" if v is None else str(bool(v)).lower())' <<<"$ready_json" 2>/dev/null || true)"
      break
    fi
  fi
  sleep 2
done

deep_link=""
log "Generating Telegram pairing link..."
set +e
tg_out="$("$ADAOS_PY" -m adaos dev telegram 2>&1)"
tg_rc=$?
set -e
if [[ $tg_rc -eq 0 ]]; then
  deep_link="$(printf '%s\n' "$tg_out" | sed -n 's/^[[:space:]]*deep_link:[[:space:]]*//p' | head -n 1 | tr -d '\r' || true)"
fi
if [[ -z "${deep_link:-}" ]]; then
  warn "Telegram pairing link not generated automatically. Run: ${ADAOS_PY} -m adaos dev telegram"
fi

print_next_steps "$SERVE_HOST" "$SERVE_PORT" "$ROLE" "$deep_link" "$connected_to_hub"
