# Навыки (Skills)

**Навык** (skill) — это переиспользуемая, версионируемая единица функциональности,
которая предоставляет tools и/или обработчики событий для рантайма AdaOS.
На файловой системе каждый навык живёт в своей директории с манифестом
`skill.yaml` и модулем обработчиков `handlers/main.py`.

---

## Структура директории

Минимальная структура навыка в workspace:

```text
skills/
  <skill-name>/
    handlers/
      main.py          # обработчики навыка (подписчики на события, tools)
    i18n/
      ru.json          # локализованные строки (опционально)
      en.json
    prep/
      prep_prompt.md   # подготовительный запрос к LLM (опционально)
      prepare.py       # подготовительный код (опционально)
    tests/
      conftest.py      # тесты (опционально)
    .skill_env.json    # значения окружения по умолчанию для навыка (опционально)
    config.json        # конфигурация навыка (опционально)
    prep_result.json   # результат подготовки (опционально)
    skill_prompt.md    # запрос на генерацию кода для LLM (опционально)
    skill.yaml         # манифест навыка (обязательно)
```

Слоты рантайма (`.adaos/workspace/skills/.runtime/...`) управляются платформой
и обычно не требуют ручного редактирования: они получаются из skills‑директорий
через `adaos dev skill activate` или обновление рантайма.

---

## Манифест (`skill.yaml`)

Файл `skill.yaml` — единственный источник правды о манифесте навыка.
Он валидируется схемами:

- `src/adaos/abi/skill.schema.json` — публичная ABI‑схема для IDE и LLM‑инструментов.
- `src/adaos/services/skill/skill_schema.json` — внутренняя схема для валидатора и рантайма навыков.

### Минимальный пример

```yaml
name: weather_skill
version: 2.0.0
description: Simple weather demo skill that shows current conditions on the desktop and reacts to city changes.
runtime:
  python: "3.11"
dependencies:
  - requests>=2.31
events:
  subscribe:
    - "nlp.intent.weather.get"
    - "weather.city_changed"
  publish:
    - "ui.notify"
default_tool: get_weather
tools:
  - name: get_weather
    description: Resolve current weather for the requested city and return a basic summary for the UI.
    entry: handlers.main:get_weather
    input_schema:
      type: object
      required: [city]
      properties:
        city: { type: string, minLength: 1 }
    output_schema:
      type: object
      required: [ok]
      properties:
        ok: { type: boolean }
        city: { type: string }
        temp: { type: number }
        description: { type: string }
        error: { type: string }
```

### Ключевые поля

- `name` — стабильный идентификатор навыка, используется в capacity (`node.yaml`), SDK и CLI (`adaos dev skill ...`).
- `version` — семантическая версия пакета навыка (см. `src/adaos/abi/skill.schema.json` для точного паттерна).
- `description` — человекочитаемое описание, используется в UI и как подсказка для LLM‑программиста.
- `runtime` — требования к окружению исполнения (сейчас в первую очередь версия `python`).
- `dependencies` — зависимости рантайма (обычно pip‑строки).
- `events` — статические подсказки о подписках и публикуемых событиях;
  реальные подписки описываются через `@subscribe` в `handlers/main.py`.
- `tools` — публичные инструменты навыка; каждый элемент должен соответствовать `@tool`
  в `handlers/main.py` и описывать согласованные `input_schema`/`output_schema`.
- `default_tool` — опциональное имя дефолтного инструмента для control plane и SDK‑хелперов.
- `exports.tools` — опциональный явный список tools, которые видны внешним вызовам и LLM‑агентам.

---

## Запуск навыка

Через CLI:

```bash
adaos skill run weather_skill
adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city": "Berlin"}'
```

Из Python:

```python
from adaos.services.skill.runtime import run_skill_handler_sync

result = run_skill_handler_sync(
    "weather_skill",
    "nlp.intent.weather.get",
    {"city": "Berlin"},
)
print(result)
```

Для разработки и тестирования удобнее использовать более высокоуровневые SDK‑хелперы
из `adaos.sdk.manage.skills.*` и `adaos.sdk.skills.testing`.

