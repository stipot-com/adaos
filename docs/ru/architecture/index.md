# Архитектура

AdaOS построен как local-first runtime с многослойной Python-кодовой базой и
небольшим управляющим surface:

- CLI поднимает и использует общий `AgentContext`
- FastAPI-сервер открывает тот же runtime по HTTP
- сервисы управляют навыками, сценариями, состоянием узла и Yjs webspace
- адаптеры изолируют файловую систему, базу данных, git, аудио, секреты и
  внешние интеграции

## Основные строительные блоки

- `src/adaos/apps`: CLI, API, launcher и process entry points
- `src/adaos/services`: orchestration и runtime-логика
- `src/adaos/sdk`: публичные helper-модули для навыков и сценариев
- `src/adaos/adapters`: реализации IO
- `src/adaos/ports`: контракты для инфраструктурных зависимостей
- `src/adaos/domain`: базовые типы и доменные helper'ы

## Модель runtime

В текущей реализации:

- узел работает как `hub` или `member`
- локальный API публикует маршруты для node, skill, scenario, observe, subnet,
  join и services
- service-type skills управляются через supervisor и health-aware API
- Yjs-backed webspace дают синхронизированное состояние сценариев и desktop
- autostart и core-update встроены в lifecycle runtime

Страницы в этом разделе в первую очередь описывают уже реализованную
архитектуру. Если документ явно помечен как roadmap или target-state design,
он фиксирует планируемую эволюцию control plane, совместимую с текущим
runtime.

Текущие target-state расширения control plane описаны в:

- [Infrascope](infrascope.md): human-facing архитектура control plane поверх
  canonical system model
- [Root MCP Foundation](root-mcp-foundation.md): root-hosted agent-facing
  foundation для будущих MCP development и operations surfaces
- [Pointer/Projection roadmap для переключения сценариев webspace](webspace-scenario-pointer-projection-roadmap.md):
  целевая архитектура и чеклист миграции от materialize-and-copy к
  pointer-first semantic rebuild
