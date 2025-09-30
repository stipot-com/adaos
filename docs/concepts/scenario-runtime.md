# план развития runtime-инструментария

## P0 — базовые возможности (сразу после greet_on_boot)

**1) Модель исполнения сценариев**

* **ExecutionUnit**: «актер» сценария с собственной почтой и контекстом.
* **Шедулер**: очередь FIFO с приоритетами (`LOW|NORM|HIGH`) + лимиты на параллелизм.
* **Кооперативная отмена**: `cancel_token`, `timeout` на шаг/сценарий.
* **Контракты**

  * `POST /api/scenario/run {id, ctx?, priority?, force?}` → `{run_id}`
  * `GET /api/scenario/status?run_id=...` → `{state, step, started_at, ...}`
  * `POST /api/scenario/cancel {run_id}`

**2) Гранулированность шага**

* Шаги — атомарные, **идемпотентные** при повторном запуске (ключ шага: `scenario_id/run_id/step_name`).
* Таймаут/ретраи: `retries: {max, backoff}` на уровне шага.
* **Саги/компенсации (минимум)**: `compensate: call: ...` для «эффектных» шагов (отправки/записи).

**3) Рутирация ввода/вывода**

* Матрица I/O в `ctx.io`: `in: ["voice","text"]`, `out: ["console","voice","tg", ...]`.
* Порты: `io.router.send(channel, text)`, `io.router.await(prefer="voice", timeout)`.
* Политики деградации: если канал недоступен — запись в `inbox.local`.

**4) Планировщик (таймеры)**

* Встроенный cron-подобный планировщик на sqlite (или file-based):
  `schedule.create({scenario_id, cron:"*/15 * * * *", ctx})`.
* Однотипные «будильники»: `at: "2025-09-21T07:00:00+02:00"`.

**5) Триггеры «по событию и условиям»**

* Подписки на `bus` (локальный): `subscribe(topic, filter?) -> run scenario`.
* Условия: минимальный expression-eval на JSON-пути (`$.payload.city == "Berlin"`).

**6) Интенты (минимум)**

* **Правила/шаблоны**: таблица `intent_rules`: `"погода {city}" -> scenario.run(weather_now, {city})`.
* Нормализация языка (ru/en), токенизация + шаблонный парсер; без ML на первом шаге.
* Контракты:

  * `POST /api/intent/recognize {text, ctx?}` → `{intent, slots}`

**7) Наблюдаемость**

* `observability.event(name, props)` → лог + метрики.
* Трассировка: `trace_id` сквозной по шагам, журнал шагов (started/ok/error, latency).
* `GET /api/scenario/runs?scenario_id=...` — список последних прогонов.

**8) Профиль/память/секреты**

* Единая точка: `memory.get/set("user.name")`, `secrets.get("weather.api_key")`.
* Версионирование `memory` (опционально): журнал изменений для отладки.

---

## P1 — устойчивость, масштаб и UX разработчика

**9) Параллельный запуск и изоляция**

* Пулы исполнителей: `pool:cpu` (блокирующие) и `pool:io` (сетевые).
* Ограничения ресурсов: `max_concurrent_per_scenario`, `max_concurrent_per_node`.
* Блокировки на ресурсы: `locks: ["mic","tts"]` в шагах, чтобы не говорить одновременно.

**10) Режимы «dry-run» и «replay»**

* `dry_run:true` — не выполнять «эффектные» шаги, только симулировать.
* `replay(run_id)` — повтор с теми же входами; для отладки.

**11) DSL сценариев — стабилизация**

* Валидация YAML схемой (jsonschema).
* Макросы: `include: ./fragments/common.yaml#ask_name`.
* Вычисления: безопасные выражения `${expr}`, доступ к `ctx`, `bag`.

**12) Диагностика/дебаггер**

* Пошаговый режим: `step_over`, `breakpoints` по `name:` шага.
* Снимок `bag` после каждого шага (обрезка секретов).

**13) E2E-тесты сценариев**

* Фрейм заглушек: `with io.stub("voice.stt", returns={"text":"Ада"}): run(...)`.
* «Золотые» снепшоты выходных сообщений.

**14) Простая цена/квоты**

* Счетчики «стоимости» по шагам (HTTP-вызовы, TTS секунды).
* Лимит на сутки по ноде/пользователю.

---

## P2 — продвинутое

**15) Распределенное исполнение**

* Шина задач: `queue` (Redis/NATS/AMQP) с ack/retry/backoff.
* Маршрутизация по нодам: `constraints: ["has:mic","region:eu"]`.

**16) Продвинутые интенты**

* Смешанный пайплайн: правила → синонимы/слоты → маленькая ML-модель (fastText/ONNX).
* Память-диалоги: хранить последние N команд пользователя, контекст.

**17) Безопасность**

* `permissions` на шаг/порт: кто может вызывать TTS/STT, доступ к секретам.
* Подпись сценариев: `scenario.yaml` + `manifest.sig` (в привязке к вашему root/PKI).
* Политики данных: PII в `memory` помечается и не уходит в логи.

**18) Транзакционность/саги (полно)**

* Гарантии «хотя бы один раз» на уровень сценария с компенсациями.
* Таблица «outbox» для идемпотентной доставки событий.

---

### короткие интерфейсы (чтобы сразу пилить)

**Сценарии**

```python
run_id = scenario.run(id="greet_on_boot", ctx={}, priority="NORM")
status = scenario.status(run_id)
scenario.cancel(run_id)
```

**Планировщик**

```python
job_id = schedule.create(scenario_id="morning_brief", cron="0 8 * * *", ctx={})
schedule.delete(job_id)
```

**Интенты**

```python
intent, slots = intent.recognize("погода берлин")
if intent == "weather.now": scenario.run("weather_now", {"city": slots["city"]})
```

**I/O маршрутизация**

```python
io.router.send(channel="voice", text=msg)      ## tts
ans = io.router.await(prefer="voice", timeout=20_000)  ## stt
```

**События**

```python
bus.publish("node.online", {...})
bus.subscribe("nlp.intent.*", handler=...)
```

---

### приоритеты и «готово, когда...»

**P0 «готово» когда:**

* Есть `POST /api/scenario/run`, `status`, `cancel`.
* Сценарий с шагами и ретраями, `io.router` работает (console+voice).
* Планировщик по cron поднимает сценарий.
* Событие `bus.publish` может триггерить сценарий по фильтру.
* Интенты по правилам запускают нужный сценарий.
* Телеметрия показывает тайминги шагов и итог статуса.

**P1 «готово» когда:**

* Параллельные запуски с блокировками ресурсов «mic/tts».
* Dry-run, replay и снепшоты сообщений.
* DSL валидируется схемой; есть дебаг-пошаговый.

**P2 «готово» когда:**

* Есть распределенный раннер через очередь.
* Включены разрешения/политики и подписи сценариев.

---

### разбиение задач по людям (намёк)

* **Core runtime/шедулер:** P0-1.
* **I/O router + voice mocks/real adapters:** P0, затем harden.
* **Intent rules + NLU-lite:** P0→P2.
* **Scheduler/cron:** P0.
* **Event bus:** P0 локальный → P2 распределённый.
* **Observability:** P0 метрики/логи → P1 дебаггер.
* **Security/PKI/permissions:** старт P2, дизайн заранее.

если хочешь, дам минимальные skeleton-модули (`scenario_engine`, `scheduler_cron`, `io_router`, `intent_rules`, `bus_local`) с короткими интерфейсами и парой тестов — под текущую `.adaos/` структуру.
