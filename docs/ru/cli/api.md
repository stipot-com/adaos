# API-сервис

## Запуск и остановка

```bash
adaos api serve --host 127.0.0.1 --port 8777
adaos api stop
adaos api restart
```

Команда `api` управляет локальным FastAPI-процессом и хранит pidfile в runtime state. При остановке и перезапуске сначала выполняется graceful shutdown, затем при необходимости используется принудительное завершение.

## Health и status

Неаутентифицированные endpoints:

- `GET /health/live`
- `GET /health/ready`

Аутентифицированные примеры:

- `GET /api/status`
- `GET /api/services`
- `GET /api/node/status`
- `GET /api/observe/stream`

## Аутентификация

Большинство operational endpoints требуют:

```http
X-AdaOS-Token: <token>
```

CLI обычно подставляет этот токен автоматически.

## Основные зоны API

- `/api/services/*`: supervision service-type skills
- `/api/skills/*`: список навыков, install/update, runtime prepare/activate, uninstall
- `/api/scenarios/*`: install, sync, push и uninstall сценариев
- `/api/node/*`: status, reliability, role, members, media, Yjs webspace
- `/api/observe/*`: ingest, tail и SSE stream
- `/api/subnet/*`: register, heartbeat, context, nodes, deregister
- `/api/admin/*`: drain, shutdown, lifecycle и orchestration обновлений ядра

## Замечания

- `/api/say` и `/api/io/console/print` в коде помечены как deprecated в пользу bus-driven flow.
- На сервер также монтируются router'ы для join flow, STT, NLU teacher и external IO webhooks.
