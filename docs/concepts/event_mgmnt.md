# event management

# NATS на root-server

NATS на **root-server** —это «магистраль» управления и обмена событиями между всеми хабами/агентами/сервисами AdaOS.

* **Единый шиной слой** для всего проекта: pub/sub + request/reply для команд, событий и RPC между сервисами (API, LLM-NLU, Telegram I/O, skills).
* **Федерация хабов**: хабы подключаются как **leafnode** к root-кластеру → локально работают автономно, а при связи — синхронизируются через root.
* **Изоляция и безопасность**: nkeys/accounts, права на **subjects** по ролям (owner/hub/member), TLS/mTLS — удобно совместить с вашей PKI.
* **Надёжность через JetStream**: at-least-once доставка, персистентные стримы, **replay**, ретраи с backoff; можно организовать DLQ-паттерн.
* **Масштабирование**: **queue groups** для горизонтали воркеров (STT/TTS, NLU, интеграции), естественный backpressure.
* **Наблюдаемость**: varz/connz/routez + метрики Prometheus (у вас уже заведены счётчики tg_updates_total и пр.) для мониторинга трафика и лагов.
* **Контрольный «контур»**: быстрое, лёгкое и дешёвое по ресурсам ядро управления (в отличие от тяжёлых очередей/логов), идеально для команд/телеметрии.
* **Кросс-хаб маршрутизация**: root знает, где какой хаб/агент, и умеет адресовать сообщения по схемам subjects.

минимальная схема топиков (пример):

* `ada.root.*` — системные события/регистрация/пейринг
* `ada.hub.{hubId}.>` — всё локальное хаба
* `ada.agent.{agentId}.cmd` / `.evt` — команды и события агента
* `ada.skill.{skill}.{evt}` — события навыков
* `ada.io.tg.*` — Telegram I/O
* `telemetry.*` — метрики/логи

где держать NATS:

* **root**: кластер NATS (+ JetStream) как центральный брокер и точка федерации.
* **hub**: опционально локальный NATS, подключённый leafnode-ом к root, чтобы работать офлайн и буферизовать события.


# Скелет маршрутизации для подсети AdaOS

Далее описание с ролями hub/member, центром интерпретации, резолвером и сценариями

## ядро: модель событий и шагов

```yaml
## inbound event (из hub/member/browser-io)
type: chat.input|voice.input|action.input|system.signal
source: agent://member/<id>|agent://hub/<id>|agent://browser/<id>
payload:
  raw: "...",  # текст/аудио/кнопка
  modality: text|voice|click|sensor
ctx:
  user_id: ...
  session_id: ...
  policies: [low-latency, private, low-cost]   # подсказки планировщику
```

```yaml
## результат интерпретации (центр интерпретации в подсети)
intent:
  name: media.find_and_show
  slots: { title: "Inception" }
  confidence: 0.84
directives:
  addressed_skill: null | skill://member/<id>/media.search   # «адресный навык»
  prefer_agent: hub|member:<id>|any                          # «активный у меня»
  plan_hint: [find, stream, display]                         # грубая декомпозиция
```

```yaml
## план к выполнению
plan:
  steps:
    - id: find
      need: capability://media.search
    - id: stream
      need: capability://media.playback
      depends_on: [find]
    - id: display
      need: capability://ui.render.video
      depends_on: [stream]
  constraints:
    policies: [low-latency]
    data_residency: local|cloud-ok
```

## реестр ресурсов и доступностей

каждый агент (hub/member/browser-io) публикует «манифест возможностей» и поддерживает heartbeat:

```yaml
agent: agent://member/plex1
kind: member
prio_base: 70                  # статический приоритет
capabilities:
  - id: capability://media.search
    prio: 85
    cost: medium
    probe: GET /health/search  # URL или локальный чек
  - id: capability://media.playback
    prio: 90
    outputs: [hls, dash]
    probe: GET /health/playback
availability:
  status: up|degraded|down
  ttl: 10s                     # lease/обновление статуса
telemetry:
  load: 0.42
  rtt_to:                      # сетевые латентности внутри подсети
    hub: 12ms
    browser: 25ms
```

browser-io тоже публикует приёмники:

```yaml
agent: agent://browser/abc
kind: browser
capabilities:
  - id: capability://ui.render.video
    prio: 95
    inputs: [hls, dash, mp4]
    probe: ping (ws)
```

## резолвер: как выбирается маршрут

1. матчинг: для каждого `plan.steps[*].need` выбираем все агенты с такой способностью и `availability.status==up`.
2. скоринг кандидатов:

```
score = w_prio*cap.prio
      + w_base*agent.prio_base
      - w_load*agent.load
      - w_rtt*latency_to_adjacent_steps
      - w_cost*cap.cost
      + w_policy*policy_fit
      + w_addressed*1(if addressed_skill matches)
```

3. ограничения: форматы io (hls/dash), права/политики, data_residency.
4. строим цепочку (DAG) с учётом совместимости входов/выходов.
5. резервирование ресурсов (lease на шаг), запуск, наблюдение, автоперестроение при деградации.

## четыре сценария исполнения

## 1) всё исполняется на hub (сам)

* когда hub имеет обе способности (например, `tts` + `ui.render.audio`) и политика `private/local`.
* план:

  * `interpret` (hub)
  * `execute` (hub)
  * `render` (hub → локальный speaker/browser-io на hub)
* правило сценария:

```yaml
policy.pin:
  prefer_agent: hub
  fallback: member:any
```

## 2) всё исполняется на member (сам)

* «активный навык в member» или адресный навык:

```yaml
directives:
  addressed_skill: skill://member/m1/media.search
policy.pin:
  prefer_agent: member:m1
  fallback: hub
```

* member делает `interpret` (если у него локальный NLU) ИЛИ отправляет на «центр интерпретации» (hub), но `execute`/`render` остаются на member (например, проигрывание на его локальном дисплее/динамике).

## 3) input на hub, исполнение на member

* типичный случай: голос пришёл на hub-микрофон, а играть нужно на телевизоре-member.
* план:

  * `interpret` (hub)
  * `find` (member: сервер медиа)
  * `stream` (member)
  * `display` (browser-io или телевизор-member)
* резолвер сопоставит форматы и выберет member с лучшим `media.playback`. Передача управления/ссылки (HLS) в `ui.render.video` на browser-io.

## 4) «всё на member (сам)» c подсказкой сценария

* сценарий задаёт приоритетную привязку:

```yaml
steps:
  - id: find
    need: capability://media.search
    prefer: member:any(tag:server)   # например, server-тег
  - id: display
    need: capability://ui.render.video
    prefer: browser:current          # «этот браузер»
```

## пример: «найди фильм <Название> и покажи в браузере»

вход: текст из browser-io → hub

1. hub → interpret → `media.find_and_show(title=...)`
2. план: `find` → `stream` → `display`
3. резолвер:

   * `find`: выбирает `member:plex1 (media.search, prio=85)`
   * `stream`: тот же `plex1 (media.playback, hls)`
   * `display`: `browser:current (ui.render.video, inputs: hls)`
4. исполнение:

   * `plex1` отдаёт HLS URL (подпись/ACL через hub)
   * hub передаёт `display.play({url, title, poster})` в browser-io
5. мониторинг: если `browser-io` отвалился, резолвер предлагает альтернативу (другой браузер/каст на TV-member).

## как ресурсы «публикуются» и проверяются

* **объявление** (при старте/изменении): POST на hub `/registry/announce` с манифестом.
* **heartbeat** каждые 5–10s, включает краткую телеметрию и результаты probe.
* **lease** на шаг: hub выдаёт `exec_token(step_id, ttl)`.
* **приоритеты**:

  * статический (`prio_base`, `cap.prio`) — из конфигурации/сценария.
  * динамический — коэффициенты от `load`, `rtt`, `battery`, `metered_network`.

## политика и директивы сценариев (минимальный DSL)

```yaml
scenario: media_server
defaults:
  policies: [low-latency]
  data_residency: local
routing:
  prefer:
    media.search:  tag:server
    media.playback: tag:server
    ui.render.video: browser:current
  forbid:
    media.playback: network:metered   # не стримить по дорогому каналу
overrides:
  # адресные вызовы
  - when: { addressed_skill: true }
    action: pin_to_addressed
  # «активный навык тут»
  - when: { prefer_agent: member:self }
    action: pin_if_available
```

## интерфейсы ввода/вывода (io contracts)

* `ui.render.video.display.play({url, title, poster}) → ack`
* `media.search.find({title}) → {items:[{id, url, hls, meta}]}`
* `media.playback.stream({id|url, format?}) → {hls|dash|mp4}`

совместимость проверяется до запуска шага: `producer.outputs ∩ consumer.inputs ≠ ∅`.

## отказоустойчивость и перезапланирование

* если probe падает или step не стартует за `startup_sla`, резолвер:

  1. снимает lease,
  2. берёт следующий кандидат по `score`,
  3. публикует `route.changed` в сессию,
  4. при полной недоступности — graceful degrade (только «find», показать постер и трейлер из web).

## безопасность и адресация

* все шаги подписаны `exec_token` (scope: step/ttl/slots).
* «адресный навык» допускается только из сценария/ролей (`member` должен доверять `hub`).
* доступ к потокам — через временные URL (signed) либо через прокси hub.

## минимальный алгоритм резолвера (псевдокод)

```python
def resolve(plan, registry, ctx):
    bindings = {}
    for step in topo_sort(plan.steps):
        candidates = registry.query(step.need, status="up")
        if hint := plan.prefer.get(step.need):
            candidates = prefer(candidates, hint)
        scored = [(c, score(c, step, bindings, ctx)) for c in candidates]
        best = pick_best(scored, constraints=compat_with_neighbors(step, bindings))
        lease(best, step)
        bindings[step.id] = best
    return bindings
```

## первые шаги

* формат манифеста capabilities/availability как выше (YAML/JSON).
* REST/ws на hub: `/registry/announce`, `/registry/heartbeat`, `/resolve`, `/exec`.
* резолвер с простым скорингом и совместимостью форматов.
* DSL сценариев: `routing.prefer/forbid/overrides`, `policies`, `data_residency`.
* три готовых playbook:

  1. **all-on-hub**, 2) **all-on-member**, 3) **hub-input→member-exec→browser-display**.

## Далее

* расширение `policy_fit` (privacy/cost/battery),
* multi-target display (каст на несколько browser-io),
* предвычисление «коридоров маршрутизации» (кэш кандидатов по intent’ам).



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



yaml в навыках/сценариях (совместимо с твоим стилем)

```yaml
## skill.yaml
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
## scenario.yaml
triggers:
  - event: "system.boot.completed"
  - event: "timer.tick"   # от планировщика
effects:
  - publish: "ui.notify"
```

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
## skill.yaml
events:
  subscribe: ["nlp.intent.detected@1"]
  publish: ["ui.notify@1"]
  schemas_from: "abi/events"   # корень
```

```yaml
## scenario.yaml
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
