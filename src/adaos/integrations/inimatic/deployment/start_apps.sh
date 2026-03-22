# src\adaos\integrations\inimatic\deployment\start_apps.sh
set -e
docker compose -f ./deployment/docker-compose.yml --project-directory ./ --env-file ./deployment/.env up -d
