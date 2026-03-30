# Обзор runtime

## Поток запроса

Обычный путь управления выглядит так:

1. CLI `adaos` загружает настройки, поднимает `AgentContext` и убеждается, что локальное окружение создано.
2. Команда либо работает напрямую через сервисы, либо вызывает локальный control API.
3. API-сервер публикует то же runtime-состояние через FastAPI router'ы.
4. Сервисы координируют persistence, git-workspace, runtime slots, доставку событий и поведение интеграций.

## Реализованные подсистемы

- `api server`: health checks, status, shutdown/drain, services, skills, scenarios, node operations, observe stream, STT, join flows
- `node runtime`: управление ролями, hub/member координация, reliability-reporting, member updates, join codes
- `skill runtime`: install, validate, update, activate, rollback, service supervision
- `scenario runtime`: install, validate, run и test сценариев
- `webspace runtime`: Yjs-backed webspace, desktop state, переключение сценариев и home scenario
- `developer workflows`: Root login/bootstrap и Forge-style push/publish

## Хранение состояния

Основное локальное состояние размещается внутри активного base directory:

- workspace-копии навыков и сценариев
- SQLite registry data
- runtime state и logs
- Yjs и webspace state
- service diagnostics и doctor reports

## Сетевая модель

Текущая реализация строится вокруг локального control API и опциональной subnet-связности:

- локальный API использует `X-AdaOS-Token`
- hub/member координация опирается на node config и subnet endpoints
- onboarding через join code реализован как в CLI, так и в API
- часть функций интегрируется с Root-hosted инфраструктурой при наличии конфигурации
