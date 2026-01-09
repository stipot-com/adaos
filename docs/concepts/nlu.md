# NLU в AdaOS

Этот документ фиксирует текущую архитектуру NLU в AdaOS и целевую эволюцию. Сейчас в прод‑MVP мы опираемся на Rasa, а Rhasspy и graph‑retriever‑NLU рассматриваем как последующие, более сложные варианты.

---

## 1. Текущий MVP: Rasa‑интерпретатор

### 1.1. Основная идея

- Есть единый **InterpreterWorkspace** (`src/adaos/services/interpreter/workspace.py`), который собирает декларативные описания интентов из:
  - навыков (`skill.yaml["nlu"]`),
  - сценариев (`scenario.json["nlu"]`),
  - вспомогательных файлов `interpreter/intents.yml` внутри навыков (авто‑/ручные описания).
- Workspace генерирует Rasa‑проект (структура `state/interpreter/rasa_project/`) и обучает модель через `RasaTrainer`.
- Обученный интерпретатор отдаёт `nlp.intent.detected` (или эквивалентные события через router‑skill), а hub мапит их в действия сценариев и навыков.

### 1.2. Где хранятся NLU‑данные

**Уровень навыка (`skill.yaml`)**

- В манифесте навыка (`skill.yaml`) поле `nlu` описывает доменные интенты и слоты:

```yaml
nlu:
  intents:
    - name: weather.show
      utterances:
        - "погода"
        - "покажи погоду"
        - "погода в {place?}"
      slots:
        place: { type: location, required: false }
      disambiguation:
        inherit: ["date"]
        defaults: { place: "@home_city" }
  resolvers:
    location: "handlers.main:resolve_location"
```

- Эти данные — **источник правды для навыка**. Отсюда tooling может генерировать:
  - `interpreter/intents.yml` (для Rasa‑workspace),
  - документацию, подсказки для LLM, тестовые датасеты и т.п.

**Уровень сценария (`scenario.json`)**

- В сценариях (`.adaos/workspace/scenarios/<id>/scenario.json`) NLU‑часть живёт в секции `nlu`:

```jsonc
{
  "id": "web_desktop",
  "nlu": {
    "intents": {
      "desktop.open_weather": {
        "description": "Открыть/включить виджет погоды на рабочем столе",
        "scope": "scenario",
        "examples": [
          "погода",
          "покажи погоду",
          "открой погоду"
        ],
        "actions": [
          {
            "type": "callSkill",
            "target": "desktop.toggleInstall",
            "params": {
              "type": "widget",
              "id": "weather",
              "webspace_id": "$ctx.webspace_id"
            }
          }
        ]
      }
    }
  }
}
```

- Здесь сценарий задаёт **поведение уровня desktop/IDE**: какой intent существует в рамках сценария и во что он мапится (на события шины, изменения YDoc и т.п.).

**Рабочее пространство интерпретатора (`state/interpreter`)**

- `InterpreterWorkspace` хранит собранный конфиг в `state/interpreter/config.yaml`:

```yaml
dataset:
  preset: moodbot
intents:
  - intent: desktop.open_weather
    description: "Открыть/включить виджет погоды"
    scenario: web_desktop
    examples:
      - "погода"
      - "покажи погоду"
```

- Дополнительно workspace создаёт датасеты на основе `interpreter/intents.yml` внутри навыков (`skills_auto/`), а также подхватывает предустановленные corpora (например, `moodbot`).

### 1.3. Сборка и обучение

- CLI‑команды интерпретатора (`src/adaos/apps/cli/commands/interpreter.py`):
  - `adaos interpreter status` — показывает, нужно ли переобучение (по fingerprint’у конфигов, навыков, датасетов).
  - `adaos interpreter train --engine rasa` — собирает Rasa‑проект и обучает модель.
  - `adaos interpreter intent add/remove/list` — ручное управление интентами в `config.yaml`.
- `RasaTrainer` (`src/adaos/services/interpreter/trainer.py`):
  - создаёт отдельный venv (`state/interpreter/.rasa-venv`),
  - ставит Rasa нужной версии,
  - выполняет `rasa train nlu` по собранному проекту.

### 1.4. Выполнение в рантайме

- Внешний интерпретатор (router‑skill + Rasa‑модель) на вход получает текст + контекст (webspace, сценарий) и публикует:

```json
{
  "intent": "desktop.open_weather",
  "confidence": 0.93,
  "slots": { "place": "Berlin" },
  "locale": "ru-RU",
  "text": "покажи погоду в Берлине",
  "webspace_id": "default"
}
```

- На hub’е `nlu.dispatcher` (`src/adaos/services/nlu/dispatcher.py`):
  - определяет активный сценарий из `ui.current_scenario` (через YDoc),
  - читает `scenario.json["nlu"].intents[intent_id].actions`,
  - выполняет указанные действия:
    - публикует события (`desktop.toggleInstall`, `weather.city_changed`, `desktop.scenario.set`),
    - в будущем — может обновлять YDoc напрямую (`updateState`, `openModal`, `scenario.workflow.action`).

Так NLU‑план интегрируется с существующей архитектурой сценариев и навыков: навыки описывают свои интенты и слоты, сценарии описывают поведение на уровне desktop/IDE, а Rasa даёт устойчивое распознавание фраз.

---

## 2. Rhasspy (позже / доп. вариант)

Rhasspy остаётся альтернативой/расширением для случаев, когда нужен offline‑режим, глубокая интеграция с голосовым стеком или специфические entity‑extractors.

Идея интеграции:

- Rhasspy используется как **backend** для text‑to‑intent и entity‑extraction.
- Он читает тот же `nlu-bundle` (intents/примеры/слоты), что и Rasa‑интерпретатор, и публикует события в шину:
  - `nlp.intent.detected`,
  - `nlp.intent.unknown` и т.п.
- Далее цепочка обработки такая же: `nlu.dispatcher` + сценарии + навыки.

На текущем этапе Rhasspy не является частью prod‑MVP и рассматривается как опциональный backend, который можно включить, не ломая формат NLU‑данных и события.

---

## 3. Graph‑retriever‑NLU (grr‑nlu), LLM и будущее

Цель следующего поколения NLU в AdaOS — перейти от чисто статистических моделей к **граф‑ориентированному retriever‑NLU** с LLM‑слоем:

- хранить знания о целях (`Goal`), понятиях (`Concept`), surface‑формах и шаблонах в виде графа;
- использовать retriever для поиска кандидатов‑интентов по embedding’ам и DSL‑шаблонам;
- подключать LLM как “учителя”, который:
  - предлагает новые интенты/шаблоны, если NLU не нашёл подходящего,
  - пишет их в локальное NLU‑хранилище (`nlu.intent.proposed`, статус `revision`),
  - после успешной валидации переводит в `stable` и публикует в глобальный NLU‑registry.

С точки зрения текущего кода это означает:

- формат `scenario.json["nlu"]` и `skill.yaml["nlu"]` остаётся основой;
- поверх Rasa можно постепенно внедрять дополнительные backend’ы (retriever, Rhasspy, LLM‑router), не меняя событий `nlp.intent.detected` и DSL действий;
- NLU‑данные становятся “живыми”: LLM может их расширять и предлагать правки, а человек или автотесты — подтверждать.

Для prod‑MVP мы фиксируем:

- основной путь — **Rasa‑интерпретатор + nlu.dispatcher + сценарии**;
- Rhasspy и grr‑NLU — **последующие этапы**, совместимые с текущими форматами данных и событиями.

