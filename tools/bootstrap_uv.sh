#!/usr/bin/env bash
# tools/bootstrap_uv.sh — bootstrap via uv (Linux/macOS)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

log()  { printf '\033[36m[*] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Repo root
cd "$(dirname "$0")/.." || die "cannot cd to repo root"

# 1) uv
if ! have uv; then
  log "Installing uv..."
  curl -fsSL https://astral.sh/uv/install.sh | sh || die "uv install failed"
  export PATH="$HOME/.local/bin:$PATH"
fi

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
if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  ok ".env created from .env.example"
fi

# 5) Convenience PATH for current shell session
if [[ -d ".venv/bin" ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

# 6) Default webspace content (scenarios + skills) via built-in `adaos install`
ADAOS_BASE_DIR="$PWD/.adaos"
mkdir -p "$ADAOS_BASE_DIR"
export ADAOS_BASE_DIR

log "Installing default webspace content (adaos install)..."
if ! uv run adaos install; then
  warn "adaos install failed (check output above)"
fi

echo
ok "Bootstrap completed."
echo "Quick checks:"
echo "  uv --version"
echo "  uv run python -V"
echo "  uv run adaos --help"
echo
echo "To run the API:"
echo "  uv run adaos api serve --host 127.0.0.1 --port 8777 --reload"
