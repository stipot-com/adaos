# AdaOS Resource Locator (ARL) — Specification v0.1

**Дата:** 2025-10-29
**Автор:** Inimatic / AdaOS Core Team
**Статус:** Draft for implementation
**Назначение:** Унифицированная система адресации и доступа к ресурсам внутри подсети AdaOS.

---

## 1. Цели

ARL обеспечивает:

1. единый способ ссылки на любые ресурсы (файлы, БД, API, навыки, очереди, секреты);
2. позднее связывание и маршрутизацию через резолверы;
3. универсальный SDK-интерфейс (`fetch`, `write`, `open_stream`, `secret`, `as_table`);
4. поддержку двухступенчатого каталога (`overview → details`) для LLM-программирования;
5. строгую политику безопасности и согласованное логирование доступа.

---

## 2. Общая модель

ARL определяет **идентификатор ресурса** (URI-подобный синтаксис), **описатель ресурса** (descriptor) и **резолвер**, который обеспечивает операции `resolve`, `plan`, `execute`.

```
[Навык / узел] → ARL URI → Resolver → Descriptor → Plan → DataEnvelope
```

---

## 3. Синтаксис URI

```
ada://<scope>/<ns>/<path>[?<query>][#<fragment>]
```

### 3.1 Компоненты

| Компонент    | Описание                                                                                      |
| ------------ | --------------------------------------------------------------------------------------------- |
| **scheme**   | Всегда `ada://`                                                                               |
| **scope**    | Область: `root`, `hub:<id>`, `member:<id>`, `any`                                             |
| **ns**       | Пространство имён (тип источника): `file`, `db`, `kv`, `api`, `bus`, `skill`, `secret`, `tmp` |
| **path**     | Свободный путь внутри пространства имён (см. ниже)                                            |
| **query**    | Опциональные параметры (`limit`, `filter`, `columns`, `asof`, и т.п.)                         |
| **fragment** | Версия или хеш (`#rev=1.2.0`, `#sha256=...`)                                                  |

### 3.2 Примеры

```text
ada://root/db/pg:main/public.users?limit=100
ada://any/file/projects/a.csv#sha256=abcd
ada://hub:phone1/api/http:GET/https%3A%2F%2Fapi.weather.com%2Fv1%2Ftoday
ada://member:tvroom/skill/media.search?q=title%3D"Dune"
ada://root/bus/nats:js/subjects/tg.input.*
ada://secret/vault:kv/telegram_bot_token
```

---

## 4. Ресолверы как плагины

- интерфейс: can_resolve(arl) → bool, resolve(arl) → ResourceDescriptor, execute(plan, args) → DataEnvelope
- базовые плагины: file, db(pg, sqlite), kv(redis), api(http), bus(nats), secret(vault/.env), skill (экспортируемые эндпоинты навыков).
- skill-resolver: мост к навыкам как к провайдерам данных: ada://…/skill/<skill_name>.<export> с типами и схемами.

Каждый `ns` реализуется отдельным **резолвером**, поддерживающим стандартный интерфейс:

```python
class Resolver:
    def can_resolve(uri: str) -> bool: ...
    def resolve(uri: str) -> ResourceDescriptor: ...
    def execute(plan: ResourcePlan, *args, **kw) -> DataEnvelope: ...
```

Базовые резолверы AdaOS:

| Namespace | Назначение                                      |
| --------- | ----------------------------------------------- |
| `file`    | локальные/удалённые файлы, каталоги             |
| `db`      | базы данных (`pg`, `sqlite`, `duckdb`, `arrow`) |
| `kv`      | key-value хранилища (`redis`, `etcd`)           |
| `api`     | HTTP/REST/GraphQL endpoints                     |
| `bus`     | очереди сообщений (`nats`, `mqtt`, `amqp`)      |
| `skill`   | экспортируемые функции навыков                  |
| `secret`  | секреты из vault или .env                       |
| `tmp`     | временные буферы и кеши                         |

---

## Готовность «по требованию»

- lazy-prepare: резолвер может «доводить до готовности» (например, прогреть кэш, создать материализованные вьюхи, поднять соединение) по флагу ?prepare=1.
- health: у каждого резолвера health() с классами ok|degraded|down и SLA-метриками.
- capability negotiation: если источник умеет предикаты — делаем pushdown (?filter=…&columns=…); иначе —fallback.

## Согласованность и версии

- неизменяемые артефакты: #sha256= в артефактных ARL.
- для потоков/таблиц: ?asof=2025-10-29T08:00Z или ?txid=....
- договор о форматах: Arrow/JSONL как «проволочный» стандарт внутри подсети.

## 5. Модель данных

### 5.1 ResourceDescriptor

Описание ресурса, возвращаемое `resolve()`:

```json
{
  "uri": "ada://any/db/pg:main/public.orders",
  "kind": "tabular",
  "schema": {
    "fields": [
      {"name": "id", "type": "int"},
      {"name": "created_at", "type": "timestamp"},
      {"name": "amount", "type": "numeric"}
    ]
  },
  "capabilities": ["scan", "filter", "project", "aggregate", "limit"],
  "requirements": {"auth": "pg:main", "latency_ms": 500},
  "immutability": "mutable",
  "version": "2025-10-29T09:00:00Z"
}
```

### 5.2 DataEnvelope

Универсальный формат результата:

```json
{
  "format": "arrow|jsonl|bytes|event",
  "meta": {"source": "uri", "ver": "hash-or-ts", "rows": 100},
  "data": "..."     // сериализованные данные
}
```

---

## 6. SDK-интерфейс (для узлов и навыков)

```python
from ada import arl, fetch, write, open_stream, secret, as_table

users = fetch(arl("ada://any/db/pg:main/public.users?limit=100"))
tbl = as_table(users).filter("name ILIKE '%ann%'").limit(10)
write("ada://hub:phone1/file/tmp/sample.parquet", tbl)
token = secret("ada://secret/vault:kv/telegram_bot_token")
```

Основные функции:

| Функция                   | Назначение                            |
| ------------------------- | ------------------------------------- |
| `arl(uri)`                | Создаёт объект ARL                    |
| `fetch(uri, opts?)`       | Чтение ресурса                        |
| `write(uri, data, opts?)` | Запись ресурса                        |
| `open_stream(uri, opts?)` | Подписка на поток                     |
| `secret(uri)`             | Доступ к секрету                      |
| `as_table(env)`           | Преобразование данных в табличное API |

---

## 7. Каталог ресурсов (overview → details)

Каталог хранит метаданные обо всех зарегистрированных ARL-ресурсах и используется для LLM-программирования навыков.

### 7.1 Overview-запись

```json
{
  "id": "res:db.pg.main.public.orders",
  "uri": "ada://any/db/pg:main/public.orders",
  "kind": "tabular",
  "tags": ["orders","sales"],
  "shape": {"rows_est": "1e7","cols": 18},
  "schema_digest": ["id:int","created_at:ts","user_id:int","amount:num","status:str"],
  "cap": ["scan","filter","aggregate"],
  "cost_hint": {"latency_ms_p50": 120},
  "examples": [{"task":"daily revenue","ops":["filter","agg"]}],
  "owner": "team.analytics"
}
```

### 7.2 Details-карточка

```json
{
  "id": "res:db.pg.main.public.orders",
  "uri": "ada://any/db/pg:main/public.orders",
  "columns": [
    {"name":"id","type":"int","pk":true},
    {"name":"user_id","type":"int","fk":"public.users.id"},
    {"name":"created_at","type":"timestamp"},
    {"name":"amount","type":"numeric(12,2)"}
  ],
  "usage": {
    "snippets": [
      {"engine":"sql","code":"SELECT date(created_at) d, sum(amount) rev FROM ..."},
      {"engine":"ir","code":"scan→filter(status='paid')→agg(day,sum(rev))"}
    ]
  },
  "access": {"policy":"rbac:analyst-read"}
}
```

### 7.3 API каталога

| Метод                                             | Описание                       |
| ------------------------------------------------- | ------------------------------ |
| `catalog.search(text?, vector?, facets?) → ids[]` | поиск по тексту и вектору      |
| `catalog.overview(ids?) → [overview...]`          | получение кратких описаний     |
| `catalog.details(ids[]) → [details...]`           | получение подробных карточек   |
| `catalog.register(descriptor)`                    | добавление/обновление записи   |
| `catalog.heartbeat(uri)`                          | обновление времени доступности |

Каталог реализует стратегию **budgeted drill-down**: при ограничении окна LLM сначала получает несколько `overview`, затем выбирает ресурсы для детализации.

---

## 8. Безопасность

- ARL сам по себе не содержит секретов.
- Все учётные данные и политики доступа хранятся в `ada://secret/...`.
- Backend (root/hub) проверяет RBAC-правила по `scope/ns/path`.
- Действия фиксируются в аудите (`access_log`, `trace_id`).

---

## 9. Версионирование и неизменяемость

| Атрибут        | Назначение                            |
| -------------- | ------------------------------------- |
| `#sha256=`     | контрольный хеш неизменяемого ресурса |
| `?asof=`       | временной срез (для данных)           |
| `version`      | ISO-timestamp последней модификации   |
| `immutability` | `mutable`, `append_only`, `immutable` |

---

## 10. Расширяемость

- новые `ns` регистрируются через плагин-манифест (`entry_points` или `@register_resolver`);
- каждый резолвер обязан объявить:

  - поддерживаемые схемы (`scheme`, `ns`);
  - операции (`read`, `write`, `stream`);
  - сериализации (`jsonl`, `arrow`, `bytes`);
  - health-метрики.

---

## 11. Взаимодействие с LLM (Dev-mode)

- LLM получает `catalog.overview()` → выбирает `ids` → получает `catalog.details()`;
- контекст ограничен бюджетом токенов (`budgeted_context()`);
- модель генерирует **IR-план** (`scan → filter → agg → …`) или код сценария;
- IR сохраняется в `plan.yaml` и исполняется без LLM в продакшене.

---

## 12. Минимальные требования реализации (MVP)

1. Базовые резолверы: `file`, `db(pg/sqlite)`, `api(http)`, `skill`, `secret`.
2. Объекты: `ResourceDescriptor`, `DataEnvelope`, `ResolverRegistry`.
3. SDK-интерфейс (Python): `arl`, `fetch`, `write`, `open_stream`, `secret`, `as_table`.
4. Каталог (overview/details) с хранилищем в PostgreSQL/Redis и текстовым поиском. Пример сценария: «медиасервер» — навык через ARL забирает списки из db/http, а выводит в browser-io.
5. CLI-утилиты:

   - `adaos arl list` — список ресурсов;
   - `adaos arl inspect <uri>` — descriptor/details;
   - `adaos arl register` — регистрация новых ресурсов.
6. Метрики: `resolve_seconds`, `execute_seconds`, `bytes_transferred`, `cache_hits`.
7. Документация: `arl.md`, `catalog.md`, примеры SDK-вызовов.

---

## 13. Версионность спецификации

| Версия     | Изменения                                           |
| ---------- | --------------------------------------------------- |
| 0.1        | базовый синтаксис, резолверы, SDK, overview/details |
| 0.2 (план) | транзакции, async streaming, policy-hooks           |

да — причём лучше **только на этапе разработки навыка** (compile-time), а не в рантайме. идея: LLM генерит не произвольный код, а **план преобразований** (IR), который затем транслируется в безопасные бэкенды (SQL/Arrow/Polars) и кладётся в репозиторий навыка. так мы получаем «кодовый метод доступа», который можно чейнить как pipeline, но без рисков рантайм-кодгена.

# LLM генерация arl

LLM на этапе разработки навыка (compile-time), а не в рантайме детализирует arl. Идея: LLM генерит не произвольный код, а план преобразований (IR), который затем транслируется в безопасные бэкенды (SQL/Arrow/Polars) и кладётся в репозиторий навыка. так мы получаем «кодовый метод доступа», который можно чейнить как pipeline, но без рисков рантайм-кодгена.

```python
# skill: analytics/orders_skill.py
from ada import arl, plan

src = arl("ada://any/db/pg:main/public.orders")
# llm.compile() доступен ТОЛЬКО при сборке навыка (dev-mode)
p = plan.compile_with_llm(
    source=src,
    intent="""
      выбери за последние 30 дней заказы по категории 'Electronics',
      отфильтруй возвраты, рассчитай revenue=item_price*qty, сгруппируй по дню,
      верни top-7 дней по выручке
    """,
    constraints={"engine": ["sql", "arrow"], "max_cost_ms": 5000}
)
# p — это декларативный IR; его можно .optimize().to_sql() или .to_arrow_ops()
RESULT_SCHEMA = p.schema()
def orders_top7(ctx, **kw):
    return p.execute(ctx)  # рантайм уже НЕ зовёт LLM
```

## ключевая архитектура

- **IR (intermediate representation)**: безопасная модель плана: `scan → filter → project(expr) → join → aggregate → window → sort → limit`.
- **транспилеры**: `IR → SQL (pushdown)` и/или `IR → Arrow/Polars ops`.
- **llm.compile()**: превращает natural language → IR, плюс объяснение плана и тесты. работает только в dev-режиме.
- **чейнинг**: `.pipe(filter=...), .pipe(project=...), .pipe(aggregate=...)` возвращают новый IR; можно комбинировать с руками написанными шагами.
- **артефакты в репозитории**: `plan.yaml` (IR), `plan.sql` (если применимо), `tests/*.golden.json`, `explain.md`.

### простой IR (yaml)

```yaml
version: 1
source: "ada://any/db/pg:main/public.orders"
ops:
  - filter: "order_date >= now() - interval '30 day' AND category = 'Electronics' AND returned = false"
  - project:
      - "order_date::date AS day"
      - "item_price * quantity AS revenue"
  - aggregate:
      keys: ["day"]
      metrics: [{"expr":"sum(revenue)", "as":"rev"}]
  - sort: [{"by":"rev","desc":true}]
  - limit: 7
schema:
  fields: [{name:"day",type:"date"},{name:"rev",type:"float"}]
```

## безопасность и контроль

- **dev-only LLM**: флаг сборки `ADA_DEV=1`; в проде LLM недоступен.
- **белый список операций**: только IR-операции; никаких `eval`, UDF — только из реестра.
- **policy**: ARL-policy решает, к каким источникам можно делать pushdown; секреты не попадают в промпты.
- **детерминизм**: IR зафиксирован, есть `hash`; в рантайме исполняется проверенная версия.
- **аудит**: `plan.explain` + сохранённый промпт и ответ LLM для трассировки.

## UX для навыка (TS/Python SDK)

```ts
// typescript (hub/root)
const src = arl("ada://hub:phone1/file/data/orders.parquet");
const p = await plan.compileWithLLM({
  source: src,
  intent: "оставь только EU, посчитай GMV по неделям, top-5 недель",
  constraints: { engine: ["arrow"], max_cost_ms: 3000 }
});
// ручной чейн поверх LLM-плана
const p2 = p.pipe({ filter: "country = 'EU'" }).pipe({ limit: 5 });
await plan.save(p2, "./plans/gmv_top5.yaml");
```

## тестирование (DoD)

1. **golden tests**: для каждого `plan.yaml` — входные фикстуры и ожидаемый JSONL/Arrow, `make test` гоняет на SQL и Arrow и сравнивает.
2. **equivalence check**: `IR → SQL → IR` равен исходному (структурно).
3. **pushdown**: при наличии pg — проверка, что фильтры/агрегации ушли в SQL.
4. **limits**: гарантированные таймауты/row-caps; проверка отказов.
5. **observability**: метрики `plan_compile_seconds`, `plan_exec_seconds`, `pushdown_ratio`.

## когда звать LLM ещё раз

- миграция схем (schema drift): `llm.rewrite(ir, new_catalog)` под PR-контролем.
- генерация **explain.md** и **docstring** для экспортов навыка.
- автогенерация **property-based** тестов по схеме.
