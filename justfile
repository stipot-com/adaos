set shell := ["bash", "-cu"]

bootstrap:
    tools/bootstrap.sh

api:
    . .venv/bin/activate && adaos api serve --host 127.0.0.1 --port 8777 --reload

backend:
    if [ -d src/adaos/integrations/adaos-backend ]; then
        cd src/adaos/integrations/adaos-backend && npm run start:api-dev
    else
        echo "Missing optional private module src/adaos/integrations/adaos-backend"
        echo "Run: git submodule update --init --recursive src/adaos/integrations/adaos-backend"
        exit 1
    fi

web:
    if [ -d src/adaos/integrations/adaos-client ]; then
        cd src/adaos/integrations/adaos-client && npm run start
    else
        echo "Missing optional private module src/adaos/integrations/adaos-client"
        echo "Run: git submodule update --init --recursive src/adaos/integrations/adaos-client"
        exit 1
    fi

redis-up:
    docker run --rm -p 6379:6379 --name inimatic-redis redis:7-alpine
