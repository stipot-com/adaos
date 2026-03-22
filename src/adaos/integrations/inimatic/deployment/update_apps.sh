# src\adaos\integrations\inimatic\deployment\update_apps.sh
set -e
COMPOSE_FILE=./deployment/docker-compose.yml
docker compose -f "$COMPOSE_FILE" --project-directory ./ --env-file ./deployment/.env build
