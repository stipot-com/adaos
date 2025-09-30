# event management

минимальном, прагматичном объёме — как скелет оркестрации навыков/сценариев и связки узлов hub↔member↔root. без него начнутся жёсткие связи и неотлаживаемый зоопарк коллбеков.

что именно включить (mvp)

1. единый шина-интерфейс: publish/subscribe, request/reply (команды), broadcast. единые Envelope-поля: event_name, version, ts, source, correlation_id, causation_id, tenant, headers.
2. реестр событий и схем: json schema для каждого event_name+version; валидация на входе/выходе. хранить в abi/schemas/events.
3. соглашения по именам:

   * команды (запросы на действие): namespace.verb (например, nlp.intent.detect, scenario.run, repo.skill.install)
   * события (факты): namespace.noun.past (nlp.intent.detected, scenario.completed, repo.skill.installed)
4. доставка и надёжность: минимум — at-least-once + idempotency_key у обработчиков, retry с backoff, dead-letter queue (DLQ).
5. планировщик: генерация timer.* событий по cron/once/interval. это покрывает «запуск по времени/условию».
6. трассировка и аудит: correlation_id, метрики публикаций/ошибок, «tail» лог по теме, опциональный persistent log для отладки/replay.
7. права: кто может publish/subscribe на какие темы (простые policy на префиксах тем).

архитектурно по этапам

* этап А (локально и просто):
  in-process event bus + адаптер к Redis Streams (или NATS JetStream — полиглотность и лёгкость деплоя). выбрать один, но сделать transport pluggable.
* этап B (узлы и облако):
  hub использует тот же интерфейс с транспортом NATS/Redis; добавляем DLQ, ретраи, простейший реестр схем; root проксирует/федератит темы между подсетями.
* этап C (удобство и отладка):
  replay по времени, сохранение выборочных потоков (event store для сценариев), opentelemetry спаны, web-вьювер «живых» событий.

вписывание в текущую структуру

* src/adaos/core/events/: Envelope, типы Event/Command, Result, ошибки.
* src/adaos/ports/events/: интерфейсы Bus, Subscription, Scheduler, SchemaRegistry.
* src/adaos/services/events/: LocalBus, RedisBus/NatsBus, CronScheduler, JsonSchemaRegistry.
* src/adaos/abi/schemas/events/: *.schema.json (+ версии).
* src/adaos/apps/cli/events.py: tail, publish, subscribe, topics, replay, dlq.
* конфиг транспорта: get_ctx().config.events (transport: local|redis|nats, urls, auth).

минимальные CLI-команды

* adaos events publish <event_name> --data '{}'
* adaos events tail <prefix> [--pretty]
* adaos events topics
* adaos events dlq list|replay|purge
* adaos timer add "*/5* ** *" --event scenario.run --data '{"name":"morning"}'

yaml в навыках/сценариях (совместимо с твоим стилем)

```yaml
# skill.yaml
events:
  subscribe:
    - "nlp.intent.weather.get"
  publish:
    - "ui.notify"
  schemas:
    nlp.intent.weather.get@1: "abi/schemas/events/nlp.intent.weather.get.v1.schema.json"
    ui.notify@1: "abi/schemas/events/ui.notify.v1.schema.json"
```

```yaml
# scenario.yaml
triggers:
  - event: "system.boot.completed"
  - event: "timer.tick"   # от планировщика
effects:
  - publish: "ui.notify"
```

правила обработчиков

* все хендлеры идемпотентны по (event_id|idempotency_key).
* тайм-ауты и ограничение конкуренции: max_concurrency per topic, чтоб сценарии не душили друг друга.
* код хендлера не бросает «голые» ошибки — только типовые (Retryable/NonRetryable).

что взять как транспорт сейчас

* если нужен минимум внешних зависимостей: Redis Streams (быстро поднять, простые ретраи, группы потребителей).
* если важна кросс-языковая лёгкость и будущая федерация: NATS JetStream (темы-сабджекты, durable consumer, лёгкий бинарник).
  оба вписываются. сделай Transport абстракцию — выбор переключается конфигом.

риски/антипаттерны

* сразу лезть в «чистый event sourcing» — рано; оставим как опцию для 1-2 доменов.
* «exactly-once» на распределённой сети — не гонимся, держим at-least-once + идемпотентность.
* без схем быстро наступит ад — поэтому schema-first, даже если поначалу схемы простые.

итог
нужен. начнём с лёгкой шины + реестра схем, таймера и базовых гарантий доставки. это даст нам: слабую связанность, параллелизм сценариев, удобную отладку и понятный DevOps по подсетям — без оверхеда «большого» ESB.

## Реестр событий

Как «единый словарь» для людей, и как машинный контракт для llm-программиста.

**где хранить:**

* `src/adaos/abi/events/registry.yaml` — индекс.
* `src/adaos/abi/events/<namespace>/<name>/v<major>/schema.json` — json schema тела.
* `src/adaos/abi/events/<namespace>/<name>/v<major>/examples/*.json` — примеры.
* автоген: `src/adaos/abi/events/_dist/registry.json` (слияние для машин).

**минимальная запись в реестре (yaml):**

```yaml
events:
  - fqdn: "nlp.intent.detected"     # namespace.name
    version: 1
    kind: "event"                    # event|command|reply
    summary: "распознанное намерение пользователя"
    producers: ["skill.nlp"]
    consumers: ["scenario.*", "ui.*"]
    schema: "nlp/intent.detected/v1/schema.json"
    required_headers: ["correlation_id","ts","source"]
    semantics:
      delivery: "at-least-once"
      idempotency_key: "intent_id"
    status: "stable"                 # draft|stable|deprecated
    example: "nlp/intent.detected/v1/examples/basic.json"
```

**каркас schema.json (json schema draft 2020-12):**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "nlp.intent.detected.v1",
  "type": "object",
  "required": ["intent", "slots", "locale"],
  "properties": {
    "intent": {"type": "string", "minLength": 1},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "slots": {"type": "object", "additionalProperties": {"type": "string"}},
    "locale": {"type": "string"}
  },
  "additionalProperties": false
}
```

**обязательный Envelope (общий для всех):**

```json
{
  "event_name": "nlp.intent.detected",
  "version": 1,
  "ts": "2025-09-23T16:20:00Z",
  "source": "skill.nlp@hostA",
  "correlation_id": "c-7f8b...",
  "causation_id": "e-19ab...",
  "tenant": "default",
  "headers": {"idempotency_key": "intent:123"}
}
```

**правила версий:**

* несовместимые изменения → `v{major+1}` в новой папке; старую помечаем `deprecated` в `registry.yaml`.
* минорные и патчи не отражаем в fqdn; фиксируем в `schema.json` через `$id` и `x-version: "1.2.0"`.

**что отдать llm-программисту (contract pack):**

* `registry.json` (машинный индекс всех событий).
* `schemas/*` (json schema по путям из индекса).
* `prompts/llm_hints.md` — короткие подсказки по именованию и примерам.
* `tests/fixtures/*.json` — валидные/невалидные payloads.
* `openapi-like` зеркало для команд: `src/adaos/abi/commands/openapi.yaml` (чтобы llm понимал request/reply).

**cli для работы:**

* `adaos events registry lint` — валидация index + ссылок.
* `adaos events scaffold <namespace.name> --kind event --version 1` — создать каркас папок, schema, пример, запись в реестр.
* `adaos events validate <event.json>` — проверить payload по schema + envelope.
* `adaos events docs build` — сгенерить md/справочник в `docs/events/`.

**интеграция в навыки/сценарии:**

```yaml
# skill.yaml
events:
  subscribe: ["nlp.intent.detected@1"]
  publish: ["ui.notify@1"]
  schemas_from: "abi/events"   # корень
```

```yaml
# scenario.yaml
triggers:
  - event: "system.boot.completed@1"
effects:
  - publish: "ui.notify@1"
```

**для llm-подсказок (встраиваемый шаблон):**

> «используй только события из `registry.json`. для каждого publish/subscribe укажи fqdn и версию. ориентируйся на примеры. поля envelope заполняй автоматически. все payload’ы должны проходить json-schema в `schema` из реестра.»

**ci:**

* pre-commit: `registry lint` + `validate` всех `examples/*.json`.
* при `status: deprecated` — ворнинг в ci и в `docs`.

**быстрый старт (первые 8 событий):**

* `system.boot.completed@1`
* `timer.tick@1`
* `repo.skill.installed@1` (event)
* `repo.skill.install@1` (command) → reply `repo.skill.install.result@1`
* `scenario.run@1` (command) / `scenario.completed@1` (event)
* `nlp.intent.detected@1`
* `ui.notify@1`

с этим набором llm-программист уже сможет уверенно генерировать хендлеры и сценарии без «угадалок».
