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
- `/api/node/*`: status, reliability, control-plane projections, role, members, media, Yjs webspace
- `/api/observe/*`: ingest, tail и SSE stream
- `/api/subnet/*`: register, heartbeat, context, nodes, deregister
- `/api/admin/*`: drain, shutdown, lifecycle и orchestration обновлений ядра
- `/v1/root/*`: root bootstrap, join-code flows и Root MCP Foundation skeleton

Текущие control-plane projection facades под `/api/node/*` намеренно узкие и агрегатные:

- `GET /api/node/control-plane/objects/self`
- `GET /api/node/control-plane/projections/reliability`
- `GET /api/node/control-plane/projections/inventory`
- `GET /api/node/control-plane/projections/neighborhood`

Текущие Root MCP Foundation skeleton endpoints вынесены отдельно от `/api/node/*`:

- `GET /v1/root/mcp/foundation`
- `GET /v1/root/mcp/contracts`
- `GET /v1/root/mcp/targets`
- `POST /v1/root/mcp/call`
- `GET /v1/root/mcp/audit`

## Позиционирование относительно Root MCP Foundation

Текущий FastAPI surface — это локальный runtime и node-control API. Это не будущая `Root MCP Foundation`.

Архитектурное позиционирование для Phase 0 такое:

- локальный HTTP остается полезным для runtime, browser, CLI и node operations
- широкий agent-facing MCP должен появиться на `root`, а не за счет превращения каждого node в открытый infrastructure endpoint
- operational access к managed targets должен идти через skill-mediated surfaces, например будущий `infra_access_skill`
- текущие `/api/node/control-plane/*` endpoints должны оставаться aggregate-focused и совместимыми с SDK-first control-plane contracts

Так AdaOS не смешивает между собой:

- local node API
- human-facing web control surfaces
- future root-hosted agent-facing MCP surfaces

## Замечания

- `/api/say` и `/api/io/console/print` в коде помечены как deprecated в пользу bus-driven flow.
- На сервер также монтируются router'ы для join flow, STT, NLU teacher и external IO webhooks.
