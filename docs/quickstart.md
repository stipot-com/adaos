# Быстрый старт

## Установка

```bash
git clone -b rev2026 https://github.com/stipot/adaos.git
cd adaos

# Опционально: приватные модули для web/backend/infra разработки
git submodule update --init --recursive \
  src/adaos/integrations/adaos-client \
  src/adaos/integrations/adaos-backend \
  src/adaos/integrations/infra-inimatic

# установка в режиме разработки
pip install -e ".[dev]"

# mac/linux:
bash tools/bootstrap.sh
# windows (PowerShell):
./tools/bootstrap.ps1
. .\.venv\Scripts\Activate.ps1
```

Core bootstrap не зависит от приватных submodule. Они нужны только если вы локально разрабатываете клиент, backend или infra.

## Запуск

```bash
# API
make api
# API: http://127.0.0.1:8777

# Backend (опционально, нужен submodule adaos-backend)
make backend

# Frontend (опционально, нужен submodule adaos-client)
make web
```

## CLI

```bash
adaos --help

# запустить API отдельно
adaos api serve --host 127.0.0.1 --port 8777

# запустить тесты в песочнице
adaos tests run
```

## Install default content

```bash
# Install default scenarios/skills into the local webspace (idempotent).
adaos install

# Pull workspace + refresh runtimes + sync scenarios into Yjs.
adaos update
```
