#!/usr/bin/env bash
# tools/bootstrap.sh — bootstrap via venv + pip (Linux/macOS)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

log()  { printf '\033[36m[*] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
fail() { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

discover_python() {
  local found=0
  if have pyenv; then
    while read -r v; do
      [[ -z "$v" || "$v" == system* ]] && continue
      local p
      p="$(pyenv which -a python 2>/dev/null | grep "/$v/" | head -n1 || true)"
      [[ -x "$p" ]] && { echo "$v $p"; found=1; }
    done < <(pyenv versions --bare 2>/dev/null)
  fi
  for x in 3.12 3.11 3.10 3.9; do
    if have "python$x"; then
      echo "$x $(command -v python$x)"; found=1
    fi
  done
  if have python3; then
    local v
    v="$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" || true
    [[ -n "${v:-}" ]] && echo "$v $(command -v python3)" && found=1
  fi
  if [[ $found -eq 0 ]] && have python; then
    local v
    v="$(python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" || true
    [[ -n "${v:-}" ]] && echo "$v $(command -v python)"
  fi
}

choose_python() {
  mapfile -t CANDS < <(discover_python | sort -Vr) || true
  [[ ${#CANDS[@]} -eq 0 ]] && fail "Python not found. Install Python 3.11+ and re-run."

  log "Available Python:"
  local i=0
  for line in "${CANDS[@]}"; do
    printf "  [%d] %s\n" "$i" "$line"
    ((i = i + 1))
  done

  local def_idx=0
  for idx in "${!CANDS[@]}"; do
    [[ "${CANDS[$idx]}" =~ ^(3\.11|3\.12) ]] && { def_idx=$idx; break; }
  done

  read -r -p "Pick index for .venv (Enter = ${def_idx}): " CHOICE
  [[ -z "${CHOICE:-}" ]] && CHOICE=$def_idx
  [[ "$CHOICE" =~ ^[0-9]+$ ]] || CHOICE=$def_idx

  local sel="${CANDS[$CHOICE]}"
  PY_VER="${sel%% *}"
  PY_BIN="${sel#* }"
  log "Using Python ${PY_VER} -> ${PY_BIN}"
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

log "Choosing Python..."
choose_python

log "Creating venv (.venv)..."
if [[ -d .venv ]]; then
  VENV_VER="$(. .venv/bin/activate >/dev/null 2>&1 && python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' || true)"
  if [[ -n "${VENV_VER:-}" && "$VENV_VER" != "$PY_VER" ]]; then
    warn "Existing .venv is $VENV_VER; recreating for $PY_VER..."
    rm -rf .venv
  fi
fi
[[ -d .venv ]] || "$PY_BIN" -m venv .venv

log "Installing Python deps (editable)..."ffront
. .venv/bin/activate
python -m pip install -U pip >/dev/null
python -m pip install -e .[dev] || fail "pip install -e .[dev] failed"

log "Bootstrapping .env..."
[[ -f .env || ! -f .env.example ]] || cp .env.example .env

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
