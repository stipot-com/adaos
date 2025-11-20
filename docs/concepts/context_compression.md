# слой компрессии контекста

для навыков — без нового неймспейса, всё через `adaos skill …`. Ключевая идея: **ссылки вместо «простынь»** + детерминированные краткие «карточки» + дельты между итерациями.

## Context Compression Layer (CCL)

### 1) Alias‑реестр (унифицированные короткие ссылки)

Файл: `.ado/aliases.json`

```json
{
  "S.entities":  {"path":"schemas/summarize_entities.json","sha":"a1b2c3d4"},
  "H.main":      {"path":"handlers/main.py","sha":"9f8e7d6c"},
  "P.options":   {"path":"prep/options.json","sha":"0aa11223"},
  "P.score":     {"path":"prep/scorecard.json","sha":"bbcc3344"},
  "A.ADR1":      {"path":"prep/ADR-0001-decision.md","sha":"d00dad42"},
  "PR.default":  {"path":"profiles/default.yaml","sha":"cafe1010"},
  "T.contract":  {"path":"tests/test_contract.py","sha":"bead2222"},
  "D.readme":    {"path":"README.md","sha":"eeddbb99"}
}
```

Правила алиасов:

* префиксы: `S` (schemas), `H` (handlers), `P` (prep), `A` (ADR), `PR` (profiles), `T` (tests), `D` (docs).
* всегда указываем короткий **идентификатор + SHA8** содержимого (контроль от галлюцинаций).

#### Команды

* `adaos skill ctx alias add <skill> <alias> <path>`
* `adaos skill ctx alias list <skill>`
* `adaos skill ctx alias verify <skill>` (проверка SHA)

---

### 2) Карточки (детерминированные краткие саммари)

Генерируем «index‑cards» — компактные, до 6–10 строк, без воды.

Примеры:

* `cards/A.ADR1.md` — **decision card** (цель, выбранный вариант, что исключили, ограничения).
* `cards/P.options.md` — **option matrix card** (O1/O2/O3: 1‑строчное резюме + несовместимости).
* `cards/S.entities.md` — **schema card** (только поля и типы, без описаний).

Правила карточек:

* только маркеры `-` и короткие строки (≤100 символов),
* без примеров/цитат, без форматирования,
* стабильная структура, чтобы их можно было всегда «склеивать».

#### Команды

* `adaos skill ctx cards build <skill>`
* `adaos skill ctx cards show <skill> <alias>`

---

### 3) Пакер контекста (Compact Prompt)

Собираем **context pack** на вход LLM: минимально, ссылочно, с макросами.

Файл: `.ado/prompt.pack`

```
## skill: my_llm_skill  ver: A=1 B=0 C=0
## include by alias (alias@sha8), model may request expansion via tool
ADR: A.ADR1@d00dad42
OPTS: P.options@0aa11223 (conflicts resolved by ADR)
SCHEMA: S.entities@a1b2c3d4 (json-only)
PROFILE: PR.default@cafe1010
TASK: generate handlers; obey schema; output json only; no path leakage.

If you need full text of an alias, call tool: get_alias("<alias>").
```

Модель видит **только карточки и ссылки**. Когда ей нужно «развернуть» артефакт — она вызывает tool `get_alias(alias)` (внутренний инструмент AdaOS под капотом `adaos.sdk.llm.tools`). Так мы держим **базовый промпт коротким**, а длинные файлы подгружаются **точечно**.

#### Команды

* `adaos skill ctx pack <skill>`
* `adaos skill ctx pack --show` (предпросмотр)
* `adaos skill ctx expand <skill> <alias>` (ручная подстановка)

---

### 4) Дельты вместо повторов (Delta Prompts)

На итерациях пересылаем **только изменения** с прошлой успешной сборки:

* `.ado/ctx/base.sha` — контрольная версия контекста,
* `.ado/ctx/delta.patch` — унифицированный diff по алиасам (изменились `H.main`, `S.entities`).

Промпт на refine:

```
BASE: ctx@5c2f9a1b
DELTA:
- S.entities: schema field "language" added (enum: en,de,ru)
- ADR note: keep O1, reject O3 (streaming) for now
ACTION: regenerate validators and tests only
```

#### Команды

* `adaos skill ctx diff <skill>`
* `adaos skill gen code --delta`

---

### 5) Микро‑компрессии (без магии)

* **minify** JSON/YAML при упаковке (без удаления смыслов),
* **strip comments** и пустые строки в коде при «просмотре» (не в исходниках),
* **section caps**: лимит токенов per‑section в pack (например, ADR ≤ 300 токенов, options ≤ 200),
* **vocab** коротких терминов: `.ado/vocab.json`
  `{ "latency_ms":"L", "rate_limit":"RL", "backoff":"BO" }`
  (используем *только* в карточках/pack, не в исходных файлах).

---

### 6) Пример: как выглядит компактный промпт

```
[CONTEXT]
ADR: A.ADR1@d00dad42
- Goal: summarize user text to short JSON
- Choice: O1 (json-tool-only), reject O3 (streaming)
- Constraints: JSON-only, 2 keys: summary, bullets
- Negative: empty input -> friendly error
SCHEMA: S.entities@a1b2c3d4
- text: string(required), lang: enum[en,de,ru](opt)
PROFILE: PR.default@cafe1010
- temperature=0.2, max_tokens=800

[INSTRUCTION]
Generate/repair handlers & tests.
Keep current aliases; if you need full file, call tool get_alias().

[DELTA]
- add "lang" support; default=auto.
```

---

### 7) Всё это остаётся в текущем CLI (без нового неймспейса)

* `adaos skill ctx alias …`
* `adaos skill ctx cards …`
* `adaos skill ctx pack …`
* `adaos skill ctx diff …`
* `adaos skill gen code --use-pack`
* tool для LLM: `get_alias(alias)` (встроенный)

# Двухступенчатый каталог ресурсов

Для этапа программирования навыка/сценария LLM используем локатор имеющихся ресурсов **overview → details** с жёстким бюджетом токенов и детерминированной итерацией.

## цели

* дать модели «карту местности» (ширину) без утопления в деталях;
* по запросу — дозаружать «карточки ресурсов» (глубину) с примерами использования;
* держать всё в пределах окна за счёт резюме, семплинга и пагинации.

## 1) структура каталога

### 1.1 overview-запись (очень дёшево)

```json
{
  "id": "res:db.pg.main.public.orders",
  "uri": "ada://any/db/pg:main/public.orders",
  "kind": "tabular|kv|api|bus|file|skill",
  "title": "orders (pg.main)",
  "tags": ["orders","sales","pii:none","fresh:realtime"],
  "shape": {"rows_est": "1e7", "cols": 18},
  "schema_digest": ["id:int","created_at:ts","user_id:int","amount:num","status:str"],
  "cap": ["scan","filter","project","aggregate","limit"],
  "cost_hint": {"latency_ms_p50": 120, "egress_mb": 0.4},
  "examples": [{"task":"daily revenue","ops":["filter","agg"]}],
  "owner": "team.analytics",
  "updated_at": "2025-10-29T08:30:00Z"
}
```

### 1.2 details-карточка (дороже, запрашивается точечно)

```json
{
  "id": "res:db.pg.main.public.orders",
  "uri": "ada://any/db/pg:main/public.orders",
  "columns": [
    {"name":"id","type":"int","nn":true,"pk":true},
    {"name":"user_id","type":"int","fk":"public.users.id"},
    {"name":"created_at","type":"timestamp","index":"btree"},
    {"name":"amount","type":"numeric(12,2)","unit":"USD"}
  ],
  "constraints": {"pii":"none","retention":"365d"},
  "pushdown": {"filter":true,"project":true,"aggregate":true,"window":false},
  "usage": {
    "snippets": [
      {"engine":"sql","code":"SELECT date(created_at) d, sum(amount) rev ..."},
      {"engine":"ir","code":"scan→filter(status='paid')→project→agg(day,sum(rev))"}
    ],
    "anti_patterns": ["select *", "без лимита > 1e6 строк"]
  },
  "access": {"policy":"rbac:analyst-read", "secrets":["ada://secret/pg:main"]},
  "quality": {"freshness_sla_s": 120, "null_rate": {"user_id":0.01}}
}
```

## 2) протокол обзора и детализации

### 2.1 API каталога (на ноде; хранить в pg/kv)

* `catalog.overview(query?, page?, page_size?) → [overview...] + page_info`
* `catalog.details(ids[]) → [details...]`
* `catalog.search(vector?, text?, facets?) → ids[]` (векторный поиск по заголовкам/описаниям/примерам)

### 2.2 связка с ARL/Resolver

* каждый `ResourceDescriptor` при регистрации/скане порождает overview (и details кеш).
* навык может объявлять экспорт как ресурс `kind:"skill"` с mini-schema и snippets.

## 3) как балансировать ширину и глубину (алгоритм «budgeted drill-down»)

пусть у нас окно `B` токенов; выделим:

* `b_head` — системная часть промпта (фикс),
* `b_overview` — список кандидатов (ширина),
* `b_details` — карточки выбранных (глубина),
* `b_task` — формулировка задачи.

стратегия:

1. **recall-ширина**: достаём `k` overview по текстовому + векторному поиску (BM25 ∪ ANN), сортируем по `relevance_score = text*0.5 + vector*0.5 + recency*0.2 + cap_match*0.3 - cost_penalty`.
2. **контроль бюджета**: вычисляем среднюю стоимость одной overview `c_o` и одной details `c_d`. подбираем `k` и `m` (m ≤ k) такие, чтобы `k*c_o + m*c_d ≤ b_overview + b_details`.
3. **LLM-выбор**: даём модели только `k` overview и просим вернуть `m` `ids` для детализации (обяз. обоснование короткими маркерами).
4. **детализация**: подтягиваем `details` для `m` id, отдаём модели финальный контекст для генерации **IR-плана** или кода сценария.
5. **итерация (опц.)**: если модель пометила «нужно ещё», повторяем шаги 1–4 с обновлёнными отрицательными примерами (do-not-repeat ids) и урезанным бюджетом.

микро-псевдокод:

```py
ctx = build_system_prompt()
candidates = catalog.search(text=q, vector=embed(q), facets=opts)
k, m = fit_budget(B, c_o, c_d)
ov = catalog.overview(ids=[c.id for c in candidates[:k]])
ids_detail = llm_pick(ov, m, task=q)         # короткий ответ: ["res:...","res:..."]
cards = catalog.details(ids_detail)
plan = llm_compile_plan(task=q, context=ov+cards)  # выдаём IR/SQL/snippets
```

## 4) сокращение токенов (правила)

* overview хранить и отдавать **в сжатом JSONL** с короткими ключами:

  * `id, u, k, t, g, sh, sd, cap, cost, ex, own, ts`
* details отдавать **по полям**, фрагментируя: `columns` (batched, по 8–10), `usage.snippets` (до 2), `pushdown`, `quality`.
* всегда включать **snippets** и **anti_patterns** — высокий КПД для модели.
* «shape/schema_digest» всегда обрезаны до 5 полей (частоты/TF-IDF от описаний).

## 5) devops навыков и сценариев

## 5.1 жизненный цикл

* **scan/register**: при деплое/миграциях резолвер публикует/обновляет overview; details — в кеш.
* **CI job** `catalog-verify`: проверяет доступность URIs, актуальность `schema_digest`, линтит snippets.
* **docs**: из деталей автогенерируем `resources.md` и карточки в UI разработчика.

## 5.2 версия и диффы

* `schema_hash` и `cap_hash` в overview; при изменении — bump `rev` и запись «what changed».
* три уровня стабильности: `stable | evolving | experimental` в теге `lifecycle:`.

## 6) интерфейсы SDK (узел python)

```python
from ada.catalog import search, overview, details, budgeted_context
from ada.llm import compile_plan   # dev-only

# 1) найти ресурсы
cand = search(text="ежедневная выручка по city", facets={"kind":["tabular","skill"]})
ctx = budgeted_context(task="GMV by city per day, last 30d", candidates=cand, budget=8000)
plan = compile_plan(task="...", context=ctx)  # IR/SQL на выходе

# 2) использовать план с ARL
result = plan.execute()
```

## 7) примеры промптов

**pick-ids (узкий ответ):**

```
выбери до {m} ресурсов из списка, которые достаточны для задачи: "{task}".
верни массив id. если сомневаешься — предпочитай skill-kind с готовой агрегацией.
```

**compile-plan:**

```
используй только карточки деталей и overview ниже. сгенерируй IR-план.
не пересекайся с анти-паттернами из usage. учитывай pushdown.
если нужен join — укажи ключи из fk/pk. верни IR yaml.
```

## 8) безопасность

* overview по умолчанию общедоступен внутри подсети (не содержит секретов);
* details gated по RBAC (скрываем PII/колонки/anti_patterns при недостаточных правах);
* подсказки `anti_patterns` и `cost_hint` помогают экономить и не утекать в «дорогие» источники.

## 9) интеграция с yjs/веб

* браузер не ходит в каталог; LLM генерит **view-schema**, опираясь на каталог на этапе разработки.
* узел публикует проекции (yview) уже выбранных ресурсов/планов.

## 10) DoD для MVP

1. хранение `overview.jsonl` + `details/*` (kv/pg), индексы: text + vector (FAISS/pgvector), фасеты.
2. API: `search/overview/details`, токен-бюджетёр `fit_budget`.
3. генерация **IR** для 3 кейсов (db, api, skill), unit-тесты эквивалентности.
4. CI для каталога: верификация URIs, обновление schema_digest, отчёт diffs.
5. dev-UI: список ресурсов, карточка details, копировать snippets.
