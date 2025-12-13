# Сценарии (Scenarios)

**Сценарий** (scenario) — декларативное описание того, как AdaOS должна вести себя
в заданном контексте: какие навыки участвуют, как между ними течёт данные и
какой UI (если нужен) должен быть отрисован.

Сценарии описываются манифестами `scenarios/<id>/scenario.yaml` и, для desktop‑UI,
соответствующими файлами `scenario.json`, которые содержат модель UI на базе Yjs.

---

## Расположение в workspace

В workspace сценарии лежат под `.adaos/workspace/scenarios/` (см. также `docs/dev-notes/workspace.md`):

```text
.adaos/workspace/scenarios/
  web_desktop/
    scenario.yaml
    scenario.json      # исходный UI для web_desktop
  prompt_engineer_scenario/
    scenario.yaml
    scenario.json      # UI и workflow для Prompt IDE
  ...
```

`scenario.yaml` — редактируемый человеком источник правды для идентичности,
зависимостей и high‑level‑метаданных сценария.
`scenario.json` — более богатый UI‑seed, который может генерироваться или
редактироваться инструментами.

---

## Манифест (`scenario.yaml`)

Структура `scenario.yaml` описана схемой:

- `src/adaos/abi/scenario.schema.json` — публичная ABI‑схема для IDE и LLM‑инструментов.

### Минимальные примеры

#### Web Desktop

```yaml
id: web_desktop
version: 0.0.1
title: Web Desktop
description: Desktop shell scenario that defines the main UI layout, registry and base catalog.
type: desktop
depends:
  - web_desktop_skill
updated_at: "2025-11-14T18:14:36+00:00"
data_projections:
  - scope: current_user
    slot: profile.settings
    targets:
      - backend: kv
      - backend: yjs
        path: data/skills/profile/{user_id}/settings
  - scope: subnet
    slot: weather.snapshot
    targets:
      - backend: yjs
        path: data/weather
```

#### Prompt IDE

```yaml
id: prompt_engineer_scenario
version: "0.1.0"
title: Prompt IDE
description: Prompt engineering IDE workspace for dev skills and scenarios.
type: desktop
depends:
  - prompt_engineer_skill
updated_at: "2025-11-14T18:14:36+00:00"
```

Детальный workflow Prompt IDE (TZ / TZ addenda) и desktop‑UI описаны
в соответствующем `scenario.json` и используются
`ScenarioWorkflowRuntime` и веб‑клиентом.

---

## `data_projections`

Секция `data_projections` в `scenario.yaml` описывает, как логические пары
`(scope, slot)` сопоставляются с конкретными backend’ами хранения
(Yjs, KV, SQL). На рантайме это загружается `ProjectionRegistry`
и используется высокоуровневыми SDK‑хелперами (`ctx.*`) для маршрутизации чтения/записи.

Подробнее — в `docs/concepts/scenario-first-launch.md`.

