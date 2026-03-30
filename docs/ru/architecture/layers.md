# Слои

## Структура кода

AdaOS использует практическое разбиение на слои:

- `apps`: исполняемые entry point, такие как CLI и API
- `services`: orchestration, lifecycle, registry, update flow и runtime-логика
- `sdk`: публичные модули для навыков и внутреннего tooling
- `adapters`: конкретные IO-реализации
- `ports`: абстракции для инфраструктурных зависимостей
- `domain`: базовые типы и helper'ы предметной области

## Зачем это нужно

Такое деление не дает операционной логике утечь в слой команд:

- CLI-команды остаются тонкими и в основном делегируют
- API-router'ы маппят HTTP-запросы на те же сервисы
- адаптеры содержат platform-specific и external-system поведение
- SDK-модули дают стабильные точки входа для навыков и tooling

## Типичные пути

- жизненный цикл навыка: `apps/cli/commands/skill.py` -> `services/skill/*` -> `adapters/db`, `adapters/git`, runtime state
- жизненный цикл сценария: `apps/cli/commands/scenario.py` -> `services/scenario/*`
- операции с узлом: `apps/cli/commands/node.py` и `apps/api/node_api.py` -> node/subnet services
- поднятие локального API: `apps/cli/commands/api.py` -> `apps/api/server.py`
