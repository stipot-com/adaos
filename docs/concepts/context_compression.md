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
