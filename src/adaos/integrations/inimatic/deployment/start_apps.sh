# src\adaos\integrations\inimatic\deployment\start_apps.sh
docker compose -f ./deployment/docker-compose.yaml --project-directory ./  --env-file ./deployment/.env up -d
