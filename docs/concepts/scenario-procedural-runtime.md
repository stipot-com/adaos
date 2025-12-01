# Scenario runtime (procedural engine)

Этот документ описывает **процедурный рантайм сценариев** в AdaOS — «движок шагов», который запускает сценарии как последовательности действий, управляет их жизненным циклом, планированием и наблюдаемостью.

Он **не** про Yjs, webspace и web-десктоп. Эти задачи решает подсистема сценариев + `ScenarioManager` + `web_desktop_skill`. Здесь мы говорим только о том, как сценарий выполняется как **flow**: шаги, таймауты, cron, интенты.

---

## 0. Текущее состояние (L0 — baseline)

Сейчас уже есть минимальный процедурный runtime:

- `ScenarioModel` / `ScenarioStep` — модель сценария и шага (имя, `when`, `call`, `args`, `set`, `do`).
- `ScenarioRuntime` — синхронный интерпретатор YAML:
  - подставляет переменные (`${vars.foo}`, `${steps.prev.result}`, строки с `${...}` внутри),
  - вызывает обработчики через `ActionRegistry` (`route: args → result`),
  - пишет состояние в `bag` (`vars`, `steps`, `meta`),
  - ведёт лог сценария (через `setup_scenario_logger`).
- `ActionRegistry` — реестр действий:
  - `io.console.print`
  - `io.voice.tts.speak`, `io.voice.stt.listen` (mock/voice)
  - `profile.get_name`, `profile.set_name`
  - `skills.run` / `skills.handle` (синхронный вызов скилла).
- `ensure_runtime_context(base_dir)` — поднимает упрощённый `AgentContext` с:
  - in-memory KV/Secrets,
  - локальной event-шиной (`LocalEventBus`),
  - базовыми путями и настройками.

Это хороший фундамент для **единовременного запуска** сценария: `ScenarioRuntime.run(model)` или `run_from_file(path)`.

Дальше мы строим вокруг этого полноценный **scenario engine**.

---

## 1. P0 — базовые возможности scenario engine

Цель P0 — превратить текущий интерпретатор в управляемый рантайм с идентификаторами прогонов, статусами, cron и простым I/O router.

### 1.1. Модель исполнения сценария

**ExecutionUnit** — логический «актёр» сценария:

- поля:
  - `run_id` — уникальный идентификатор прогона,
  - `scenario_id` — id сценария (из `scenario.yaml`),
  - `ctx` — начальный контекст (user/subnet/webspace, io-профиль, locale и т.п.),
  - `priority: LOW|NORM|HIGH`,
  - `state: PENDING|RUNNING|DONE|FAILED|CANCELED|TIMEOUT`,
  - `current_step`, `started_at`, `finished_at`, `trace_id`.
- хранение:
  - sqlite-таблица `scenario_runs` (минимум) или KV-обёртка.

**Шедулер**:

- очередь `scenario_runs` со статусами:
  - новые прогоны попадают туда со `state=PENDING`,
  - воркер-процесс выбирает задания по приоритету и времени создания,
  - ограничение параллелизма: `max_concurrent_total`, `max_concurrent_per_scenario`.
- исполнение:
  - для каждого `run_id` создаётся `ScenarioRuntime` с привязанным `AgentContext`,
  - шаги выполняются последовательно, как сейчас, но с записью прогресса в `scenario_runs`.

**Кооперативная отмена и таймауты**:

- у `ExecutionUnit` есть:
  - `cancel_token` (флаг в KV/таблице),
  - `timeout_scenario` (общий лимит времени),
  - `timeout_step` (по умолчанию/на уровне шага).
- `ScenarioRuntime` перед каждым шагом:
  - проверяет `cancel_token`,
  - проверяет, не истёк ли лимит по сценарию/шагу.

**Контракты** (API поверх двигателя):

HTTP:

- `POST /api/scenario/run`
  - body: `{ "id": "greet_on_boot", "ctx": {...}, "priority": "NORM", "force": false }`
  - ответ: `{ "run_id": "..." }`
- `GET /api/scenario/status?run_id=...`
  - ответ: `{ "state": "...", "scenario_id": "...", "current_step": "...", "started_at": "...", "finished_at": "...", "error": "...?" }`
- `POST /api/scenario/cancel`
  - body: `{ "run_id": "..." }`

Python-обёртка:

```python
run_id = scenario.run(id="greet_on_boot", ctx={}, priority="NORM")
status = scenario.status(run_id)
scenario.cancel(run_id)
```

---

### 1.2. Гранулированность шага

Здесь цель — сделать шаг **атомарным** и максимально **идемпотентным**.

- Идентификатор шага:

  - `step_key = f"{scenario_id}/{run_id}/{step_name}"`.
- Для шагов с побочными эффектами рекомендуется:

  - либо использовать idempotency key (например, HTTP-запрос с `Idempotency-Key`),
  - либо хранить «отметку» об успешном выполнении шага (`step_key → "ok"`).

**Ретраи / backoff**:

- на уровне DSL для шага:

  ```yaml
  steps:
    - name: send_welcome
      call: mail.send
      args: {...}
      retries:
        max: 3
        backoff: "exp:1s..1m"
  ```

* рантайм:

  - при ошибке:

    - если остались попытки — планирует повторный запуск шаг/сценария с задержкой по backoff;
    - если нет — помечает `state=FAILED` и пишет ошибку.

**Минимальные саги/компенсации**:

- для «эффектных» шагов DSL позволяет указать компенсацию:

  ```yaml
  - name: reserve_slot
    call: calendar.reserve
    args: {...}
    compensate:
      call: calendar.release
      args:
        reservation_id: "${steps.reserve_slot.result.id}"
  ```

- на P0 достаточно:

  - выполнять компенсации **в обратном порядке** при явной отмене сценария.

---

### 1.3. Маршрутизация ввода/вывода (I/O router)

Нужен единый слой `io.router` поверх уже существующих сервисов (`io_console`, voice-mock, telegram и т.п.).

**Модель I/O профиля**:

- в `ctx.io` хранится:

  - `in: ["voice","text","tg"]`,
  - `out: ["console","voice","tg"]`,
  - fallback-политики.

**Интерфейс**:

```python
io.router.send(channel="voice", text=msg)        # tts → пользователь
ans = io.router.await(prefer="voice", timeout=20000)  # stt / текстовый ответ
```

- реализация:

  - внутри — вызовы `tts_speak`, `stt_listen`, отправка в tg, запись в консоль;
  - если канал недоступен → запись в локальный inbox (`inbox.local`) или деградация на другой канал.

На P0 достаточно:

- `console` + `voice_mock` (как сейчас),
- простая стратегия деградации (voice → console).

---

### 1.4. Планировщик (cron / будильники)

Встроенный **планировщик заданий** поверх sqlite/KV.

**Модель задания**:

- поля:

  - `job_id`,
  - `scenario_id`,
  - `cron` **или** `at` (ISO-datetime),
  - `ctx` (начальные данные при запуске),
  - `enabled: bool`.

**Интерфейс**:

Python:

```python
job_id = schedule.create(scenario_id="morning_brief", cron="0 8 * * *", ctx={})
schedule.delete(job_id)
```

HTTP:

- `POST /api/schedule/create`
- `POST /api/schedule/delete`
- `GET /api/schedule/list`

Поведение:

- отдельный воркер считывает задания:

  - cron-парсер определяет ближайшие срабатывания,
  - для истекших `at`/cron-слотов создаёт новые `scenario_runs` (через engine).

---

### 1.5. Триггеры «по событию и условиям»

Связка с локальной event-шиной (`EventBus`).

**Подписки**:

- правило: «по событию и фильтру — запустить сценарий».

Пример описания подписки:

```yaml
triggers:
  - topic: "weather.requested"
    scenario: "weather_now"
    when: '$.payload.city == "Berlin"'
    ctx:
      user_id: "${payload.user_id}"
```

Механика:

- сервис подписывается на темы `bus` (`*.event`),
- при каждом событии:

  - проверяет условия (expression-eval по JSON пути, типа mini-JQ),
  - если `true` → создаёт `scenario_run`.

---

### 1.6. Интенты (минимум, без ML)

P0 — **правиловой recognizer**.

**Правила**:

- таблица `intent_rules`:

  | pattern               | intent             | slots |
  | --------------------- | ------------------ | ----- |
  | "погода {city}"       | "weather.now"      | city  |
  | "какая завтра погода" | "weather.tomorrow" | —     |

- обработка:

  - нормализация текста (ru/en), lower-case, простая токенизация,
  - шаблонный парсер вытаскивает слоты.

**Контракты**:

HTTP:

```json
POST /api/intent/recognize
{ "text": "погода берлин", "ctx": {...} }

→ { "intent": "weather.now", "slots": { "city": "берлин" } }
```

Bindings:

- правило связывает intent со сценарием:

  ```yaml
  intents:
    - id: "weather.now"
      scenario: "weather_now"
  ```

Пайплайн:

1. `intent.recognize(text)` → `{intent, slots}`.
2. если найден `intent` → `scenario.run(scenario_by_intent, ctx_with_slots)`.

---

### 1.7. Наблюдаемость (observability)

Всё, что делает engine, должно быть **прослеживаемо**.

**Событийная модель**:

- `observability.event(name, props)`:

  - пишет в лог,
  - может отправлять метрики / трейсинг.

Минимальный набор:

- `scenario.start` / `scenario.finish`,
- `step.start` / `step.finish`,
- `step.error`,
- `io.send` / `io.receive`,
- `intent.detected`.

**Структура логов и статусов**:

- `scenario_runs`:

  - `run_id`, `scenario_id`, `state`, `current_step`, `error`, `started_at`, `finished_at`, `trace_id`.
- журнал шагов:

  - `run_id`, `step_name`, `status`, `latency_ms`, `error`.

HTTP:

- `GET /api/scenario/runs?scenario_id=...`

  - список последних прогонов с ключевыми полями.

---

### 1.8. Профиль, память, секреты

Единая точка входа поверх уже существующих сервисов.

**Память**:

- `memory.get("user.name")`, `memory.set("user.name", "Ада")`,
- ключи вида `user.*`, `session.*`, `subnet.*` и т.п.

**Секреты**:

- `secrets.get("weather.api_key")`,
- в DSL шаги не видят секреты напрямую; они уходят в экшены.

Опционально (на будущее):

- версионирование `memory` для отладки:

  - таблица `memory_journal`: ключ, старое значение, новое, `run_id`, `step`.

---

## 2. P1 — устойчивость, масштаб и UX разработчика

На этом уровне мы усиливаем устойчивость и удобство отладки.

### 2.1. Параллелизм и изоляция

- **пулы исполнителей**:

  - `pool:cpu` — блокирующие задачи,
  - `pool:io` — сетевые I/O.
- лимиты:

  - `max_concurrent_per_scenario` — чтобы один сценарий не съел всё,
  - `max_concurrent_per_node` — глобальный предел.
- блокировки ресурсов:

  - в DSL шаг может объявить:

    ```yaml
    locks: ["mic", "tts"]
    ```

  - движок гарантирует, что два шага с `locks: ["mic"]` не говорят одновременно.

### 2.2. Режимы dry-run и replay

- `dry_run: true`:

  - шаги помеченные как «эффектные» не выполняются (HTTP-запросы, записи),
  - engine логирует, **что было бы сделано**, но не меняет внешний мир.
- `replay(run_id)`:

  - повторяет прогоны с теми же входами (ctx, события, ответы I/O),
  - полезно для отладки нестабильных сценариев.

### 2.3. DSL сценариев — стабилизация

- строгая схема (jsonschema) для `scenario.yaml`,

- макросы/фрагменты:

  ```yaml
  include: ./fragments/common.yaml#ask_name
  ```

- безопасные выражения:

  - `${vars.city}`,
  - `${not vars.has_name}`,
  - без произвольного Python.

### 2.4. Диагностика и «дебаггер»

- пошаговый режим:

  - `step_over`, `breakpoints` по `name` шага,
- снимок `bag` после каждого шага:

  - с обрезкой секретных полей (`secrets.*`, PII),
- интеграция с UI:

  - просмотр последнего `run`, навигация по шагам.

### 2.5. E2E-тесты сценариев

- каркас тестирования:

  ```python
  from adaos.testing.scenarios import run_scenario, io_stub

  with io_stub("voice.stt", returns={"text": "Ада"}):
      result = run_scenario("greet_on_boot", ctx={})
      assert result["vars"]["user_name"] == "Ада"
  ```

- «золотые» снепшоты:

  - сценарии фиксируют ожидаемую последовательность `io.send` / `io.receive`,
  - тесты сравнивают фактическую последовательность с ожидаемой.

### 2.6. Цена/квоты

- счётчики «стоимости»:

  - HTTP-запросы (кол-во, суммарное время),
  - TTS-секунды, STT-секунды,
  - обращения к LLM (если есть).
- квоты:

  - лимит на сутки / неделю по:

    - ноде,
    - пользователю,
    - сценарию.

---

## 3. P2 — продвинутые возможности

Здесь engine становится распределённым и более «умным».

### 3.1. Распределённое исполнение

- шина задач (Redis / NATS / AMQP):

  - `queue:scenario_runs` с `ack/retry/backoff`,
- маршрутизация по нодам:

  - в описании сценария — `constraints`:

    ```yaml
    constraints: ["has:mic", "region:eu"]
    ```

  - engine выбирает подходящий узел под эти ограничения.

### 3.2. Продвинутые интенты

- смешанный пайплайн:

  1. правила,
  2. словари/синонимы,
  3. маленькая ML-модель (fastText/ONNX).
- контекст диалога:

  - хранить последние N команд пользователя и ответы,
  - использовать при распознавании интента (например, уточнение города).

### 3.3. Безопасность и политики

- `permissions` на шаг/порт:

  - кто может вызывать `TTS/STT`, какие секреты доступны,
- подпись сценариев:

  - `scenario.yaml` + `manifest.sig`, привязанные к PKI root,
- политики данных:

  - PII, хранящиеся в `memory`, помечаются и не попадают в логи/трейсы.

### 3.4. Полные саги и транзакционность

- гарантии «хотя бы один раз» на уровне сценария:

  - шаги с компенсациями и outbox-таблица для доставки событий,
- `outbox`:

  - сценарий пишет намерение отправить событие в локальную таблицу,
  - отдельный процесс доставляет события идемпотентно и помечает их как доставленные.

---

## 4. Короткие интерфейсы (для кода и API)

### 4.1. Сценарии

Python:

```python
run_id = scenario.run(id="greet_on_boot", ctx={}, priority="NORM")
status = scenario.status(run_id)
scenario.cancel(run_id)
```

HTTP (обёртки):

```http
POST /api/scenario/run
GET  /api/scenario/status
POST /api/scenario/cancel
```

### 4.2. Планировщик

```python
job_id = schedule.create(scenario_id="morning_brief", cron="0 8 * * *", ctx={})
schedule.delete(job_id)
```

### 4.3. Интенты

```python
intent, slots = intent.recognize("погода берлин")
if intent == "weather.now":
    scenario.run("weather_now", {"city": slots["city"]})
```

### 4.4. I/O маршрутизация

```python
io.router.send(channel="voice", text=msg)
ans = io.router.await(prefer="voice", timeout=20_000)
```

### 4.5. События

```python
bus.publish("node.online", {...})
bus.subscribe("nlp.intent.*", handler=...)
```

---

## 5. Критерии готовности по этапам

### P0 «готово», когда

- есть `POST /api/scenario/run`, `status`, `cancel`,
- сценарий с шагами и ретраями работает через `ExecutionUnit` и очередь,
- cron-планировщик поднимает сценарий по расписанию,
- событие на локальной шине может триггерить сценарий по фильтру,
- простые intent-rules запускают нужный сценарий,
- телеметрия показывает тайминги и итоговый статус прогона.

### P1 «готово», когда

- есть параллельные запуски с блокировками ресурсов («mic/tts»),
- реализованы `dry_run`, `replay` и снепшоты сообщений,
- DSL сценариев валидируется схемой, есть пошаговый дебаг.

### P2 «готово», когда

- есть распределённый раннер через очередь задач,
- включены разрешения/политики и подписи сценариев,
- реализованы саги и outbox-паттерн для гарантированной доставки.

---
