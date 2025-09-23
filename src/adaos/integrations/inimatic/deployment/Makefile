PROJECT_DIR := $(abspath ..)
COMPOSE_FILE := $(PROJECT_DIR)/deployment/docker-compose.yaml
ENV_FILE := $(PROJECT_DIR)/deployment/.env
PROFILE ?= dev
COMPOSE := docker compose --project-directory $(PROJECT_DIR) --env-file $(ENV_FILE) -f $(COMPOSE_FILE) --profile $(PROFILE)
BACKEND_SERVICE := $(if $(filter $(PROFILE),prod),backend,backend-dev)
FRONTEND_SERVICE := $(if $(filter $(PROFILE),prod),frontend,frontend-dev)

.PHONY: build pull up down ps logs restart backend frontend config

build:
	$(COMPOSE) build

pull:
	$(COMPOSE) pull

up:
	$(COMPOSE) up -d --wait --remove-orphans

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f

restart:
	$(COMPOSE) restart

backend:
	$(COMPOSE) logs -f $(BACKEND_SERVICE)

frontend:
	$(COMPOSE) logs -f $(FRONTEND_SERVICE)

config:
	$(COMPOSE) config
