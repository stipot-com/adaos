# src\adaos\integrations\inimatic\deployment\update_apps.sh
docker compose -f ./deployment/docker-compose.yaml --project-directory ./  --env-file ./deployment/.env build
