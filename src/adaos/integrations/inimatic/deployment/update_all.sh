git reset --hard
git pull
docker rm -f $(docker ps -a -q) # stop all containers
# docker system prune --all
docker builder prune
docker compose -f ./deployment/docker-compose.yaml --project-directory ./  --env-file ./deployment/.env build
docker compose -f ./deployment/docker-compose.yaml --project-directory ./  --env-file ./deployment/.env up -d
