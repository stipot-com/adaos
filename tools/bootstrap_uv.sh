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

show_qr_if_available() {
  local text="$1"
  [[ -z "${text:-}" ]] && return 0
  have qrencode || return 0
  echo
  echo "     (QR)"
  qrencode -t ANSIUTF8 "$text" 2>/dev/null || true
  echo
}

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
  local tg_pair_code="${6:-}"
  local owner_url="${7:-}"
  local owner_code="${8:-}"

  echo
  ok "Bootstrap completed."
  echo
  echo "Next steps:"
  if [[ -n "${deep_link:-}" ]]; then
    echo "  1) Telegram: open and confirm pairing:"
    echo "     ${deep_link}"
    if [[ -n "${tg_pair_code:-}" ]]; then
      echo "     pair_code: ${tg_pair_code}"
    fi
    show_qr_if_available "${deep_link}"
  else
    echo "  1) Telegram pairing:"
    echo "     ${ADAOS_PY} -m adaos dev telegram"
  fi
  echo "  2) Owner browser:"
  if [[ -n "${owner_url:-}" && -n "${owner_code:-}" ]]; then
    echo "     Open: ${owner_url}"
    echo "     user_code: ${owner_code}"
    show_qr_if_available "${owner_url}"
  else
    echo "     ${ADAOS_PY} -m adaos dev root login"
    echo "     Then open https://app.inimatic.com/owner-auth and enter the code."
  fi
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
  if ! have qrencode; then
    echo
    echo "Tip: install 'qrencode' to show QR codes in terminal."
  fi
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
export ADAOS_API_BASE="$ROOT_URL"

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

if [[ "${ROLE:-}" == "hub" ]]; then
  log "Initializing Root subnet (adaos dev root init)..."
  if ! "$ADAOS_PY" -m adaos dev root init; then
    warn "adaos dev root init failed (check output above)"
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
    # Best-effort start:
    # - Windows (Git-Bash/MSYS): scheduled task is installed but not started automatically.
    # - Linux without systemctl (containers/WSL without systemd): enable writes unit but cannot start it.
    if have schtasks; then
      schtasks /Run /TN "AdaOS" >/dev/null 2>&1 || true
    fi
    # If autostart cannot report "active: true", fall back to background serve.
    set +e
    as_json="$("$ADAOS_PY" -m adaos autostart status --json 2>/dev/null)"
    as_rc=$?
    set -e
    if [[ $as_rc -eq 0 && -n "${as_json:-}" ]]; then
      active="$("$ADAOS_PY" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); v=d.get("active"); print("" if v is None else str(bool(v)).lower())' <<<"$as_json" 2>/dev/null || true)"
      listening="$("$ADAOS_PY" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); v=d.get("listening"); print("" if v is None else str(bool(v)).lower())' <<<"$as_json" 2>/dev/null || true)"
      if [[ "${active:-}" != "true" || "${listening:-}" == "false" ]]; then
        warn "Autostart is enabled but not active; falling back to background run"
        service_installed=0
      fi
    fi
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
tg_pair_code=""
log "Generating Telegram pairing link..."
set +e
tg_out="$("$ADAOS_PY" -m adaos dev telegram 2>&1)"
tg_rc=$?
set -e
if [[ $tg_rc -eq 0 ]]; then
  tg_pair_code="$(printf '%s\n' "$tg_out" | sed -n 's/^[[:space:]]*pair_code:[[:space:]]*//p' | head -n 1 | tr -d '\r' || true)"
  deep_link="$(printf '%s\n' "$tg_out" | sed -n 's/^[[:space:]]*deep_link:[[:space:]]*//p' | head -n 1 | tr -d '\r' || true)"
fi
if [[ -z "${deep_link:-}" ]]; then
  warn "Telegram pairing link not generated automatically. Run: ${ADAOS_PY} -m adaos dev telegram"
fi

owner_url=""
owner_code=""
log "Generating Owner browser pairing code..."
set +e
owner_json="$("$ADAOS_PY" -m adaos dev root login --print-only --json 2>/dev/null)"
owner_rc=$?
set -e
if [[ $owner_rc -eq 0 && -n "${owner_json:-}" ]]; then
  owner_url="$("$ADAOS_PY" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print((d.get("verification_uri_complete") or d.get("verification_uri") or "").strip())' <<<"$owner_json" 2>/dev/null || true)"
  owner_code="$("$ADAOS_PY" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print((d.get("user_code") or "").strip())' <<<"$owner_json" 2>/dev/null || true)"
fi

print_next_steps "$SERVE_HOST" "$SERVE_PORT" "$ROLE" "$deep_link" "$connected_to_hub" "$tg_pair_code" "$owner_url" "$owner_code"
