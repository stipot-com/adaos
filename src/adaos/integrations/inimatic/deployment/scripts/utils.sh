#!/usr/bin/env bash
# scripts/utils.sh
set -euo pipefail

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "[fatal] required env var '$name' is not set"
    exit 1
  fi
}

# Translate container-style runtime paths to host filesystem paths when running
# deployment scripts on the host. If the original path exists, it is returned
# unchanged. Otherwise we try well-known mappings and return the first existing
# alternative; if none exist, return the original path.
resolve_host_path() {
  local p="${1:-}"
  if [[ -z "$p" ]]; then
    echo ""
    return 0
  fi
  if [[ -e "$p" ]]; then
    echo "$p"
    return 0
  fi
  # Map secrets
  if [[ "$p" == /run/inimatic/secrets/* ]]; then
    local suffix="${p#/run/inimatic/secrets}"
    local alt="/opt/inimatic/secrets${suffix}"
    if [[ -e "$alt" ]]; then
      echo "$alt"
      return 0
    fi
  fi
  # Map SSH runtime
  if [[ "$p" == /run/inimatic/ssh/* ]]; then
    local suffix="${p#/run/inimatic/ssh}"
    local alt="/opt/inimatic/runtime/ssh${suffix}"
    if [[ -e "$alt" ]]; then
      echo "$alt"
      return 0
    fi
  fi
  echo "$p"
}

require_file() {
  local path="${1:-}"
  local msg="${2:-required file missing}"
  if [[ -z "$path" ]]; then
    echo "[fatal] $msg (path is empty)"
    exit 1
  fi
  if [[ ! -e "$path" ]]; then
    echo "[fatal] $msg: $path"
    exit 1
  fi
}

wait_healthy() {
  local svc="$1" retries="${2:-150}"  # 150 * 2s = ~5min
  local id status
  id=$(docker compose ps -q "$svc")
  if [[ -z "$id" ]]; then
    echo "[fatal] No container for service $svc"
    return 1
  fi
  while (( retries-- )); do
    status=$(docker inspect --format='{{.State.Health.Status}}' "$id" 2>/dev/null || echo starting)
    if [[ "$status" == "healthy" ]]; then
      echo "[ok] $svc: healthy"
      return 0
    fi
    sleep 2
  done
  echo "[fatal] $svc failed to become healthy in time"
  return 1
}

# Sync Telegram webhooks for all bots listed in TG_BOTS
# Requires ENV: TG_BOTS, TG_<NAME>_BOT_TOKEN_FILE, TG_<NAME>_BOT_SECRET_FILE
# Optional ENV: TG_WEBHOOK_BASE (default https://$API_DOMAIN), TG_WEBHOOK_PATH_PREFIX (default /io/tg/webhook)
sync_telegram_webhooks() {
  if [[ -z "${TG_BOTS:-}" ]]; then
    echo "[webhook] TG_BOTS is empty, skipping"
    return 0
  fi

  local base="${TG_WEBHOOK_BASE:-https://${API_DOMAIN}}"
  local prefix="${TG_WEBHOOK_PATH_PREFIX:-/io/tg/webhook}"
  IFS=',' read -r -a __bots <<<"$TG_BOTS"

  for b in "${__bots[@]}"; do
    local upname tok_var sec_var tok_path sec_path token secret url
    upname=$(echo "$b" | tr '[:lower:]' '[:upper:]')
    tok_var="TG_${upname}_BOT_TOKEN_FILE"
    sec_var="TG_${upname}_BOT_SECRET_FILE"
    tok_path="${!tok_var:-}"
    sec_path="${!sec_var:-}"
    # Resolve to host paths if variables point to container runtime paths
    local htok_path hsec_path
    htok_path="$(resolve_host_path "$tok_path")"
    hsec_path="$(resolve_host_path "$sec_path")"
    require_file "$htok_path" "missing token file for bot $b ($tok_var)"
    require_file "$hsec_path" "missing secret file for bot $b ($sec_var)"
    token="$(<"$htok_path")"
    secret="$(<"$hsec_path")"
    url="${base%/}${prefix%/}/$b"

    echo "[webhook] setWebhook for bot=$b url=$url"
    curl -fsS -X POST "https://api.telegram.org/bot${token}/setWebhook" \
      -d "url=${url}" \
      -d "secret_token=${secret}" \
      -d "drop_pending_updates=true" >/dev/null

    echo "[webhook] ok for $b"
  done
}
