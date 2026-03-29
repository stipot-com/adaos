#!/usr/bin/env bash
# scripts/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${STATE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ACTIVE_FILE="${ACTIVE_FILE:-$STATE_DIR/.active_slot}"
ENVF="${ENVF:-$STATE_DIR/.env}"

DEFAULT_PROJECT_DIR="$STATE_DIR"
DEFAULT_BASE="$DEFAULT_PROJECT_DIR/docker-compose.yml"
if [[ ! -f "$DEFAULT_BASE" && -f "$STATE_DIR/docker/compose/docker-compose.yml" ]]; then
  DEFAULT_PROJECT_DIR="$STATE_DIR/docker/compose"
  DEFAULT_BASE="$DEFAULT_PROJECT_DIR/docker-compose.yml"
fi

PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_PROJECT_DIR}"
BASE="${BASE:-$DEFAULT_BASE}"

source "$SCRIPT_DIR/utils.sh"

echo "[deploy] ENVF = $ENVF"
require_file "$ENVF" ".env not found"

# export vars from ENVF for docker compose and our script logic
set -a
. "$ENVF"
set +a

mkdir -p "$STATE_DIR"
cd "$PROJECT_DIR"

# --- sanity on critical env/paths ---
require_var APP_DOMAIN
require_var API_DOMAIN
require_var DEFAULT_EMAIL
require_var COMPOSE_PROJECT_NAME

bash "$SCRIPT_DIR/render_tls_overrides.sh"

# secrets that must exist inside the host (as mounted in compose)
require_file /opt/inimatic/secrets "secrets folder missing"
require_file /opt/inimatic/runtime/ssh/forge_ssh_key "forge ssh key missing"
require_file /opt/inimatic/runtime/ssh/known_hosts "known_hosts missing"

# dev logs sink (used by logtap + /v1/dev/log_tail endpoints)
mkdir -p /opt/inimatic/runtime/logs || true
chmod +x /opt/inimatic/scripts/logtap.sh 2>/dev/null || true

# optional Telegram bots: if defined, ensure files exist
if [[ -n "${TG_BOTS:-}" ]]; then
  IFS=',' read -r -a __bots <<<"$TG_BOTS"
  for b in "${__bots[@]}"; do
    upname=$(echo "$b" | tr '[:lower:]' '[:upper:]')
    tok_var="TG_${upname}_BOT_TOKEN_FILE"
    sec_var="TG_${upname}_BOT_SECRET_FILE"
    tok_path="${!tok_var:-}"
    sec_path="${!sec_var:-}"
    # Accept container-style /run paths by resolving to host when validating
    htok_path="$(resolve_host_path "$tok_path")"
    hsec_path="$(resolve_host_path "$sec_path")"
    require_file "$htok_path" "missing token file for bot $b ($tok_var)"
    require_file "$hsec_path" "missing secret file for bot $b ($sec_var)"
  done
fi

# --- blue/green slot detection ---
active="A"
if [[ -f "$ACTIVE_FILE" ]]; then
  active=$(tr '[:lower:]' '[:upper:]' < "$ACTIVE_FILE" || echo "A")
fi

if [[ "$active" == "A" ]]; then
  new="B"
  OLD_FRONT="frontend_a"; OLD_BACK="backend_a"
  NEW_FRONT="frontend_b"; NEW_BACK="backend_b"
else
  new="A"
  OLD_FRONT="frontend_b"; OLD_BACK="backend_b"
  NEW_FRONT="frontend_a"; NEW_BACK="backend_a"
fi

echo "[deploy] Active slot: $active  ->  Deploying slot: $new"

# login to GHCR (idempotent)
bash /opt/inimatic/scripts/ghcr_login_via_app.sh

# 0) ensure reverse-proxy & acme are up-to-date
docker compose --env-file "$ENVF" -f "$BASE" up -d reverse-proxy acme redis postgres nats nats_init logtap
wait_healthy reverse-proxy || { echo "[deploy] reverse-proxy not healthy"; exit 1; }
docker exec reverse-proxy nginx -t
docker exec reverse-proxy nginx -s reload || true

# 1) pull new images first
docker compose --env-file "$ENVF" -f "$BASE" pull "$NEW_FRONT" "$NEW_BACK" || true

# 2) start new slot
docker compose --env-file "$ENVF" -f "$BASE" up -d "$NEW_FRONT" "$NEW_BACK"

# 3) wait for health
wait_healthy "$NEW_FRONT"
wait_healthy "$NEW_BACK"

# 4) optional: sync Telegram webhooks (if backend doesn't auto-register, or to force refresh)
if [[ "${TG_SYNC_WEBHOOKS:-0}" == "1" ]] && [[ -n "${TG_BOTS:-}" ]]; then
  echo "[deploy] Syncing Telegram webhooks..."
  sync_telegram_webhooks
fi

# 5) stop & remove old slot
docker compose --env-file "$ENVF" -f "$BASE" rm -sf "$OLD_FRONT" "$OLD_BACK" || true

# 6) set new active slot
echo "$new" | tee "$ACTIVE_FILE" >/dev/null

# 7) cleanup dangling images (quiet)
docker image prune -f >/dev/null || true
echo "[deploy] Blue-Green switch complete. Active: $new"
