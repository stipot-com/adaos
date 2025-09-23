#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-inimatic}"
COMPOSE_ARGS=(
  --project-directory "$ROOT_DIR"
  --env-file "$ROOT_DIR/deployment/.env"
  -f "$ROOT_DIR/deployment/docker-compose.yaml"
  --profile prod
)

docker compose "${COMPOSE_ARGS[@]}" pull || true
docker compose "${COMPOSE_ARGS[@]}" up -d --remove-orphans --wait
docker compose "${COMPOSE_ARGS[@]}" ps
