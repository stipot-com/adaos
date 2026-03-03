#!/usr/bin/env bash
# tools/bootstrap.sh — bootstrap via venv + pip (Linux/macOS)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

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
     adaos api serve --host 127.0.0.1 --port 8777 --reload
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
if ! adaos install; then
  warn "adaos install failed (check output above)"
fi

ok "Bootstrap completed."
printf "\nTo activate venv:\n  source .venv/bin/activate\n\n"
open_subshell_help
