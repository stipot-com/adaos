#!/usr/bin/env bash
# tools/bootstrap_uv.sh — bootstrap на uv (Linux/macOS)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

log()  { printf '\033[36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# 0) рабочая директория = корень репозитория (скрипт можно вызывать откуда угодно)
cd "$(dirname "$0")/.." || die "cannot cd to repo root"

# 1) uv
if ! have uv; then
  log "Installing uv..."
  curl -fsSL https://astral.sh/uv/install.sh | sh || die "uv install failed"
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2) Python deps через uv (автообновление lock при рассинхроне)
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

# 3) Frontend deps (pnpm если есть, иначе npm ci -> npm install)
if [[ -d "$SUBMODULE_PATH" ]]; then
  log "Installing frontend dependencies in $SUBMODULE_PATH ..."
  pushd "$SUBMODULE_PATH" >/dev/null || die "cannot enter $SUBMODULE_PATH"
  if have pnpm; then
    pnpm install || die "pnpm install failed"
    USED_PKG_CMD="pnpm install"
  else
    set +e
    npm ci --no-audit --fund=false
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      USED_PKG_CMD="npm ci"
    else
      warn "npm ci failed; updating lock with npm install..."
      npm install --no-audit --fund=false || die "npm install failed"
      USED_PKG_CMD="npm install"
    fi
  fi
  ok "Frontend dependencies installed ($USED_PKG_CMD)"
  popd >/dev/null
else
  warn "Frontend path not found: $SUBMODULE_PATH (skipped)"
fi

# 4) .env
if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  ok ".env created from .env.example"
fi

# 5) Короткая команда: добавим .venv/bin в PATH для текущей сессии
if [[ -d ".venv/bin" ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

# 6) Default webspace content
DEFAULT_SCENARIOS=("web_desktop", "prompt_engineer_scenario")
DEFAULT_SKILLS=("weather_skill", "web_desktop_skill", "prompt_engineer_skill", "profile_skill")
ADAOS_BASE_DIR="$PWD/.adaos"
mkdir -p "$ADAOS_BASE_DIR"
export ADAOS_BASE_DIR

log "Installing default webspace content..."
for scn in "${DEFAULT_SCENARIOS[@]}"; do
  log "  adaos scenario install $scn"
  if ! uv run adaos scenario install "$scn"; then
    warn "scenario '$scn' install failed (maybe already installed)"
  fi
done
for skill in "${DEFAULT_SKILLS[@]}"; do
  log "  adaos skill install $skill"
  if ! uv run adaos skill install "$skill"; then
    warn "skill '$skill' install failed (maybe already installed)"
  fi
done
# 6) Сводка
echo
log "Bootstrap completed."
echo "Quick checks:"
echo "  uv --version"
echo "  uv run python -V"
echo "  adaos --help     # короткая команда должна работать в этой сессии"
echo
echo "To run the API:"
echo "  uv run adaos api serve --host 127.0.0.1 --port 8777 --reload"

