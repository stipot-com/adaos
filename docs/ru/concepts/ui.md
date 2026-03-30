# AdaUI v0 — UI как данные для навыков и сценариев (без iframe)

## 0) цель и границы

AdaUI v0 даёт способ визуализации навыков/сценариев без встраивания удалённого кода (iframe/web-components).
Навык публикует **два документа**: `view` (описание интерфейса) и `state` (данные).
SPA-клиент локально рендерит UI по декларативной схеме и живёт на **кэше** при офлайне.

---

## 1) ключевые принципы

* **декларативно:** UI — это JSON (`view`) + JSON-состояние (`state`) по фиксированной схеме «AdaUI v0».
* **локальный рендер:** рендерер в браузере (общий компонент-кит), никаких удалённых скриптов навыков.
* **двухуровневое хранилище:** навыки пишут только в **hub-store**, клиент читает/пишет только в **browser-store**.
* **реактивная синхронизация:** hub-store ⇄ browser-store через Yjs (CRDT), офлайн-устойчивость «из коробки».
* **ветвление документов:** `main`, `owner/*`, `device/*`, `ab/*`, `preview/*`; клиент собирает оверлей.
* **безопасность по ключам:** ACL на документ, короткоживущие токены, навыки не видят device-ид.
* **минимальный словарь компонентов:** чтобы LLM легко генерировал корректные `view.json`.

---

## 2) модель данных

### 2.1 ключ документа

```
doc_key = "{owner_id}/{skill_id}/{doc_type}:{branch}"
doc_type ∈ {view, state}
branch   ∈ {"main","owner/{id}","device/{id}","ab/A","ab/B","preview/{token}"}
```

### 2.2 документ (минимальная форма)

```json
{
  "key": "owner1/weather_skill/state:main",
  "rev": "l42-v3",                // логический ревизионник (для индекса/аудита)
  "etag": "sha256:...",           // контроль содержимого snapshot (служебно)
  "meta": {
    "schema": "adaui/v0",
    "ttl": "8h",
    "acl": ["OWNER:read", "ROLE:SCENARIO:read", "SKILL:weather:write"]
  },
  "content": { /* произвольный JSON */ }
}
```

### 2.3 оверлей веток на клиенте (приоритет)

`device/*` → `owner/*` → `ab/*` → `main` (для каждого `doc_type` отдельно).

---

## 3) синхронизация и хранилища

### 3.1 выбор механизма

**Yjs** (CRDT) + `y-websocket` для транспорта.
Hub хранит Y-документы на диске и ведёт `docs_index` в SQLite для ACL/TTL/поиска.

### 3.2 hub-store

* персист: `y-leveldb`/`y-sqlite` для содержимого;
* индекс: таблица SQLite `docs_index(key TEXT PK, acl TEXT, schema TEXT, ttl_sec INT, updated_at, etag TEXT)`;
* задачи: проверка доступа, TTL-GC/снапшоты, аудит.

### 3.3 browser-store

* персист: `y-indexeddb` (по `doc_key`);
* сервис `syncClient`: подключение к `/sync`, подписки на комнаты (по ключам/префиксам), ACK, реконнект.

### 3.4 протокол (в общих чертах)

* `WS /sync` (совместим с y-websocket).
* «комната» = `doc_key`; join допускается только после ACL-проверки.
* изменения текут как CRDT-операции; офлайн-мердж — автоматический.

---

## 4) контракт навыка

### 4.1 manifest.json (фрагмент)

```json
{
  "id": "weather_skill",
  "version": "2.1.0",
  "capabilities": {
    "visual": true,
    "ui": {
      "schema": "adaui/v0",
      "title": "Погода",
      "icon": "sun-cloud",
      "modes": ["modal", "panel"],
      "sizeHints": { "w": 480, "h": 360 }
    },
    "streams": {
      "supportsDeclarativeInterest": true
    }
  },
  "permissions": { "ownerOnly": false }
}
```

### 4.2 взаимодействие навыка с хранилищем

* навык **пишет** `view` (обычно через сервис registry/деплой) и **обновляет** `state` в **hub-store**:
  получает серверный `Y.Doc` по ключу и вносит изменения через Yjs API;
* подписывается на свои ключи (если нужно реактивно читать/реагировать на команды).

---

## 5) AdaUI v0 — схема `view`

### 5.1 JSON-схема (сокращённая)

```json
{
  "schema": "adaui/v0",
  "meta": { "title": "..." },
  "layout": { "$ref": "#/$defs/node" },
  "$defs": {
    "node": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": { "enum": ["title","text","badge","metric","stack","row","card","table","chart.line","button","select","empty"] },
        "text": { "type": "string" },
        "label": { "type": "string" },
        "value": { "type": "string" },
        "icon": { "type": "string" },
        "gap": { "type": "number" },
        "title": { "type": "string" },
        "child": { "$ref": "#/$defs/node" },
        "children": { "type": "array", "items": { "$ref": "#/$defs/node" } },
        "columns": { "type":"array","items":{"type":"object","required":["key","label"],"properties":{"key":{"type":"string"},"label":{"type":"string"}}} },
        "rows": { "type": "string" },   // binding: "{{state.rows}}"
        "series": { "type":"array","items":{"type":"object","properties":{
          "label":{"type":"string"}, "x":{"type":"string"}, "y":{"type":"string"}, "data":{"type":"string"}
        }}},
        "bind": { "type": "string" },   // e.g. "{{state.level}}"
        "options": { "type": "array", "items": { "type":"object","properties":{"label":{"type":"string"},"value":{"type":"string"}} } },
        "action": { "type":"object","properties":{
          "type": { "enum": ["http.get","http.post","bus.publish","navigate"] },
          "path": { "type": "string" },
          "body": { "type": "object" },
          "topic": { "type": "string" },
          "payload": { "type": "object" },
          "mode": { "enum": ["modal","panel"] }
        }}
      }
    }
  }
}
```

### 5.2 биндинги и действия

* биндинги: только `{{state.*}}` и `{{meta.*}}`; массивы — `[*]`.
* действия:

  * `http.get|post { path, body? }` — уходит в **command-relay** (см. §7.3), не напрямую в навык;
  * `bus.publish { topic, payload }` — публикация в шину;
  * `navigate { mode }` — локальная навигация SPA.

---

## 6) SPA (frontend) и рендер

### 6.1 компоненты

* `StatusBar` (hub online, latency, reconnect)
* `DesktopScenario` (контейнер рабочего стола)
* `IconGrid` (список визуальных навыков)
* `ModalHost` (диалог/панель; **без iframe**, с локальным рендером AdaUI v0)
* `AdaUIRenderer` (рантайм рендера по `view`+`state`)

### 6.2 состояние

* `authState`: AUTH/PAIRED/ONLINE
* `desktopState`: icons[], pinned[], order[] (читается из store)
* `modalState`: { open, skill_id, sizeHints } (контролы SPA)

### 6.3 офлайн-поведение

* `view/state` берутся из browser-store;
* `http.post`-действия попадают в `ops_outbox` (очередь), реплеятся при онлайне;
* «живые» обновления восстанавливаются после реконнекта по CRDT.

---

## 7) системные сервисы (hub)

### 7.1 sync-endpoint

* `WS /sync` (y-websocket совместимо)
* аутентификация токеном; сопоставление `doc_key → room`; ACL-проверка на join

### 7.2 docs_index API (минимум)

* `GET /docs/index?prefix={owner_id}/...` → список ключей с метаданными (для discovery)
* TTL-обслуживание (GC/снэпшоты) — фоновые задачи

### 7.3 command-relay

* SPA-действия `http.get|post` не бьют по навыку напрямую; кладём **команду** в `owner/{id}/commands/state:device/{device_id}` или вызываем системный REST, а обработчик на hub/skill меняет соответствующий `state`.
* ответ пользователю приходит как изменение `state` по текущему ключу — реактивно.

---

## 8) сценарий «Рабочий стол» и «Менеджер иконок»

### 8.1 ключи документов

```
owner/{id}/desktop/view:main
owner/{id}/desktop/state:main

owner/{id}/icon_manager/view:main
owner/{id}/icon_manager/state:main     // порядок, пины, группы
```

### 8.2 API icon_manager (через store)

* список визуальных навыков формируется по реестру манифестов + ACL → записывается в `icon_manager/state`.
* пин/порядок меняются экшенами SPA → через command-relay корректируется `icon_manager/state`.

---

## 9) пример навыка: weather_skill

### 9.1 ветки

```
owner/{id}/weather_skill/view:main
owner/{id}/weather_skill/state:main       // агрегированный срез
owner/{id}/weather_skill/state:device/{device_id}  // локальные предпочтения
```

### 9.2 пример `view` (сжато)

```json
{
  "schema":"adaui/v0",
  "meta":{"title":"Погода"},
  "layout":{"type":"stack","gap":12,"children":[
    {"type":"title","text":"{{meta.title}}"},
    {"type":"row","gap":12,"children":[
      {"type":"metric","label":"сейчас","value":"{{state.now.temp_c}} °C","icon":"thermometer"},
      {"type":"badge","text":"{{state.now.conditions}}","icon":"cloud"}
    ]},
    {"type":"card","title":"прогноз 6 ч","child":{
      "type":"chart.line",
      "series":[{"label":"T, °C","x":"{{state.forecast[*].ts}}","y":"{{state.forecast[*].temp_c}}"}]
    }},
    {"type":"row","justify":"end","children":[
      {"type":"button","text":"обновить","action":{"type":"http.post","path":"/v1/refresh"}}
    ]}
  ]}
}
```

### 9.3 декларативный интерес (дополнительно)

```
POST skills/weather_skill/v1/interest
{ "streams":[{ "topic":"weather.now", "area":{...}, "period":"5m", "ttl":"8h", "format":"compact" }] }
```

Навык сам обновляет `state` в hub-store; клиент ничего напрямую у навыка не запрашивает.

---

## 10) безопасность

* доступ к `doc_key` даёт только hub после проверки ACL (`OWNER`, роли сценариев, skill-scopes).
* навыки видят только `owner_id` и свой `skill_id`; **никаких device-ид**.
* все клиентские действия — через командные документы/шину, а не прямой HTTP в навык.
* `docs_index` подписывает `view` (etag, sig) — защита от подмены.

---

## 11) LLM-дружелюбность

* фиксированный словарь компонентов и ключей;
* `view.json` и пример `state.json` легко генерируются;
* патчи — малы и понятны (CRDT-операции или JSON Patch, если нужен экспорт);
* валидация по JSON Schema с понятными ошибками.

---

## 12) MVP-беклог (минимум)

**hub**

* [ ] `WS /sync` (y-websocket совместимый) + ACL-обёртка
* [ ] `docs_index` (SQLite) + TTL-GC
* [ ] command-relay (командный документ + обработчик)

**browser**

* [ ] `store/yjs`: y-indexeddb, подключение к `/sync`, подписки по ключам/префиксам
* [ ] overlay-резолвер веток
* [ ] очередь `ops_outbox` (повтор экшенов после онлайна)

**spa**

* [ ] `AdaUIRenderer` (компоненты v0: title, text, badge, metric, stack, row, card, table, chart.line, button, select, empty)
* [ ] DesktopScenario, IconGrid, ModalHost (без iframe)
* [ ] офлайн-режим (render из кэша; экшены в очередь)

**skills**

* [ ] weather_skill: `view/state` в hub-store + обновление `state` по расписанию/интересу
* [ ] icon_manager: формирование списка визуальных навыков и prefs в `state`

**tests**

* [ ] авторизация → desktop с иконками
* [ ] клик по «Погоде» → модалка, график, метрика
* [ ] офлайн → UI из кэша; онлайн → дельты догоняются
* [ ] owner-only иконки недоступны не-owner

---

## 13) заметки по эволюции

* **компоненты v0.1:** tabs, form, chart.bar, progress
* **двусторонние биндинги:** локальный `ui.local` для форм и редактирования
* **подписки по префиксу:** `owner/{id}/*/state:*` для DesktopScenario
* **CRDT-экспорт в snapshots:** периодическая компакция больших документов
