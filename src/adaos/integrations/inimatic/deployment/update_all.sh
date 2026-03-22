git reset --hard
git pull
docker rm -f $(docker ps -a -q) # stop all containers
# docker system prune --all
docker builder prune
set -e
COMPOSE_FILE=./deployment/docker-compose.yml
docker compose -f "$COMPOSE_FILE" --project-directory ./ --env-file ./deployment/.env build
docker compose -f "$COMPOSE_FILE" --project-directory ./ --env-file ./deployment/.env up -d
