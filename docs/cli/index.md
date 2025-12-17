# AdaOS CLI

AdaOS CLI — интерфейс командной строки для работы с системой навыков, сценариев и инфраструктурой.  
Основан на [Typer](https://typer.tiangolo.com/) и интегрирован с API и root-сервером.

---

## Установка

```bash
pip install -e ".[dev]"
````

---

## Основные группы команд

| Команда     | Назначение                                           | Документация                 |
| ----------- | ---------------------------------------------------- | ---------------------------- |
| `api`       | запуск HTTP API (FastAPI)                            | [api.md](api.md)             |
| `skills`    | управление локальными и опубликованными навыками     | [skills.md](skills.md)       |
| `scenarios` | управление сценариями                                | [scenarios.md](scenarios.md) |
| `dev`       | рабочее пространство разработчика (subnet)           | [dev.md](dev.md)             |
| `tests`     | запуск тестов в песочнице                            | [tests.md](tests.md)         |
| `runtime`   | управление слотами, метаданными и активацией навыков | [runtime.md](runtime.md)     |
| `misc`      | справка и версия CLI                                 | [misc.md](misc.md)           |

---

## Примеры

```bash
# запуск API
adaos api serve --host 127.0.0.1 --port 8777

# создание нового навыка
adaos skills scaffold my-skill

# публикация сценария из dev-пространства
adaos dev scenario publish weather-dashboard --bump minor

# активация слота B
adaos runtime activate weather-skill --slot B
```

---

## Install / Update

```bash
# Install default preset (scenarios + skills) into the default webspace (idempotent).
adaos install --preset default

# Update: pull sources, refresh skill runtimes, sync scenarios into Yjs webspace.
adaos update --pull --sync-yjs --migrate-runtime
```

## Autostart

```bash
adaos autostart status
adaos autostart enable --host 127.0.0.1 --port 8777
adaos autostart disable
```

## Status (progress vs remote)

```bash
# All installed skills / scenarios (runtime + git status).
adaos skill status
adaos scenario status

# One item + diff vs remote tracking branch.
adaos skill status weather_skill --fetch --diff
adaos scenario status web_desktop --fetch --diff

# Compare dev workspace vs hub draft state (Root API, requires hub mTLS keys in `node.yaml`).
adaos skill status --space dev
adaos skill status --space dev weather_skill --fetch --diff
```
