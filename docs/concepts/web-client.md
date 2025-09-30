# Веб интеграция

### что фиксируем как принцип

* **веб-представление живёт у сценария**, а навыки — это «сервисы» и «виджеты», которые сценарий вызывает.
* сценарий может быть **многостраничным/многокомпонентным**: у него есть своё дерево маршрутов, состояние, права, тема.
* единый рендер-движок (ULM/USDL/UMF из прошлого ответа) остаётся, но **точкой входа становится сценарий**, а не навык.

### 1) три уровня UI (по сложности)

1. **UI-Markdown (ULM)** — сверхпростой, безопасный, сервер-драйвенный

   * подмножество markdown + мини-директивы (`:::card`, `:::form`, `@[action:id]`)
   * идеален для LLM: текст+кнопки+простые формы без верстки и js
   * рендерится в стандартные компоненты (Ionic/shadcn) автоматически

2. **UI-Schema (USDL)** — декларативный JSON (по духу: AdaptiveCards/Vega-Lite)

   * компонентная схема + биндинги к `viewModel` (JSONPath/JMESPath)
   * поддержка форм, таблиц с пагинацией, чартов (через vega-lite), шагов, модалок
   * дифф-обновления через JSON Patch/JSON Merge Patch
   * устойчиво к версионированию, хорошо валидируется JSON-Schema

3. **Микро-фронт (UMF)** — опционально для «тяжёлых» навыков

   * изолированный web component/Micro-frontend (iframe/cust. element)
   * capability-handshake + строгое API событий
   * реже нужен; позволяет «нарисовать всё», не ломая общую модель

> правило: каждый навык **обязан** уметь говорить на уровне 1, может повышать детальность до 2, и только при необходимости — 3.

---

### 2) событийная шина UI

унифицированные темы (названия условные):

* `ui.render` — от навыка → клиенту (ULM/USDL/UMF дескриптор)
* `ui.patch` — частичные обновления состояния/документа
* `ui.action` — от клиента → навыку (клик/submit/shortcut)
* `ui.query` — ленивые данные (таблицы/деревья/поиск)
* `ui.toast`/`ui.notify` — лёгкие уведомления
* `ui.route` — навигация (внутри «рабочих областей»: main, side, modal)
* `ui.state` — синхронизация viewModel (опционально, двунаправленно)

формат действия (минимум):

```json
{
  "type": "ui.action",
  "action_id": "save",
  "payload": {"form": {"title": "..." }},
  "context": {"skill": "notes", "path": "sheet/1", "session": "…"}
}
```

---

### 3) слой разметки (layout) и capability-negotiation

* **слоты**: `header`, `main`, `aside`, `modal`, `toast` — навык адресует, где отрисовать
* **capabilities** (от клиента): темы, шрифты, a11y, offline, vega-lite, maps
  навык может деградировать: если нет чартов → рендерит таблицу, если нет aside → вкладка в main.
* **theme tokens**: цвет/типографика как дизайн-токены, без произвольного CSS навыка

---

### 4) LLM-friendly ограничения

* без произвольного HTML/JS от навыка на уровнях 1–2
* фиксированный словарь компонентов + строгие схемы
* короткие, предсказуемые action_id и поля форм
* авто-валидация по JSON-Schema с понятными сообщениями об ошибках

---

### 5) мини-спека USDL (v0)

```json
{
  "version": "usdl/0.2",
  "layout": {"slot": "main"},
  "viewModel": {"items": [], "filter": ""},
  "components": [
    {"type": "Card", "title": "Список",
     "body": [
       {"type": "Input", "label": "поиск", "bind": "$.filter", "debounceMs": 300,
        "on": [{"event": "change", "action": "search"}]},
       {"type": "Table", "columns": [
          {"key": "name", "title": "Название"},
          {"key": "status", "title": "Статус"}
        ],
        "data": {"source": "lazy", "query": "items.list",
                 "params": {"q": "$.filter"}, "pageSize": 25},
        "on": [{"event": "rowClick", "action": "open"}]
       },
       {"type": "Button", "text": "Добавить", "action": "create", "variant": "primary"}
     ]
    }
  ],
  "actions": {
    "search": {"emit": "ui.query", "target": "items.list"},
    "open":   {"emit": "ui.action"},
    "create": {"emit": "ui.action"}
  }
}
```

* `bind` — JSONPath в `viewModel`
* `data.source: "lazy"` — клиент сам будет вызывать `ui.query` при скролле/странице
* чарт: `{"type":"Chart","spec":{"$vegaLite":{…}},"data":{"bind":"$.series"}}`

---

### 6) UI-Markdown (ULM) — пример

```
# заметки

:::card
введите заголовок

:::form id="new-note"
- title: input(required)
- tags: chips
[создать](@action:create submit="new-note")
:::

:::table query="notes.list" pageSize=20
* {{name}} — {{status}}
:::
```

клиент преобразует в USDL, вешает обработчики и генерит `ui.action/ui.query`.

---

### 7) жизненный цикл и состояние

* **server-driven UI**: навык шлёт первый `ui.render` (ULM/USDL), далее — `ui.patch`
* **viewModel** живёт на клиенте; навык присылает патчи (JSON Patch) для реактивности
* долгие операции → `progress`/`spinner` + отмена (`action: cancel`)

---

### 8) безопасность и изоляция

* уровни 1–2: без пользовательского JS; XSS практически исключён
* уровень 3 (UMF): iframe + CSP + postMessage API, список разрешённых capability
* запрет прямого доступа к токенам/куки; всё через событийную шину

---

### 9) доступность, локализация, офлайн

* все базовые компоненты имеют ARIA-роли; клавиатурная навигация по умолчанию
* `i18n` ключи внутри USDL: `"text": {"i18n": "notes.add"}`
* офлайн-кеш USDL/viewModel и отложенные `ui.action` (replay)

---

### 10) интеграция с текущим фронтом

* **рендерер**: React + Ionic/shadcn/ui (адаптер компонентов под USDL)
* **чарты**: vega-lite рендерер (без произвольного код-инъекта)
* **сборка**: один «UI-движок» в приложении; навыки ничего не билдят
* **DevTools**: правый инспектор USDL/ULM, лог `ui.*` событий, визуальный diff patch

---

### 11) мини-гайд для навыка (LLM-программист)

* всегда поддерживай **ULM** (на крайний случай)
* если нужна форма/таблица/чарт — отдавай **USDL** v0.2
* отправляй **краткие** `ui.patch`, не перерисовывай всё
* используй короткие `action_id` и валидируй вход по JSON-Schema
* для больших наборов — `ui.query` с пагинацией/фильтрами

---

### 12) пример: навык отвечает USDL

```json
{
  "type": "ui.render",
  "skill": "weather",
  "payload": {
    "version": "usdl/0.2",
    "layout": {"slot":"main"},
    "viewModel": {"city": "Berlin", "now": null},
    "components": [
      {"type":"Form","id":"f","fields":[
        {"name":"city","label":"город","component":"Input","bind":"$.city","required":true}
      ],
       "actions":[{"text":"показать","action":"fetch"}]
      },
      {"type":"Card","title":"погода сейчас",
       "body":[
         {"type":"Progress","when":"$.now==null"},
         {"type":"Markdown","when":"$.now!=null",
          "text":"**{{$.city}}**: {{$.now.temp}}°C, {{$.now.desc}}"}
       ]
      }
    ],
    "actions":{
      "fetch":{"emit":"ui.action","payload":{"op":"fetch","city":"$.city"}}
    }
  }
}
```

клиент отправит `ui.action(fetch)` → навык вернёт `ui.patch`:

```json
[
  {"op":"replace","path":"/viewModel/now","value":{"temp":14.5,"desc":"cloudy"}}
]
```

---

### 13) версионирование и эволюция

* `version: usdl/x.y` + capability-negotiation; рендерер умеет понизить фичи
* строгие JSON-Schema для каждой версии; автогенерация шпаргалок для LLM
* совместимость: старший минор — назад-совместим, мажор — через feature flags

## модель исполнения

* **Scenario App** = `scenario.yaml` + набор UI-документов (ULM/USDL) + статические ассеты (опционально UMF).
* **Router** на фронте монтирует сценарий в рабочую область (`/s/{scenarioId}/...`) и управляет его маршрутами.
* **ViewModel** сценария: единое дерево состояния; навыки получают/меняют только свои неймспейсы.
* **Event bus** остаётся: `ui.action/ui.query/ui.patch` идут от/к сценарию; сценарий проксирует к нужным навыкам.

### навигация и состояние

* сценарий объявляет **меню/дерево** (как «варианты использования»), а рендерер автоматически строит навигацию.
* маршруты: **страницы** и **вью-узлы** (компоненты). каждый узел имеет:

  * `usdl`/`ulm` документ,
  * `bind` к кусочку `viewModel`,
  * `guards` (права/условия видимости),
  * `prefetch` (ленивая загрузка).

#### композиция с навыками

* сценарий подтягивает навыки через **контракты**:

  * **action contracts**: `orders.create`, `orders.update`
  * **query contracts**: `orders.list`, `orders.byId`
  * **widget contracts** (если нужны встраиваемые готовые компоненты навыка)
* виджеты навыков встраиваются как **USDL-компоненты с типом `Widget`**, а данные идут по `ui.query`.

### схема сценария (черновик)

```yaml
# .adaos/scenarios/orders/scenario.yaml
name: orders
version: 0.3.0
title: «заказы»
entry: routes.list
theme:
  tokens: default
permissions:
  - skill: orders_service
    contracts: [orders.list, orders.create, orders.update]
  - skill: customers_service
    contracts: [customers.search]
viewModel:
  initial:
    filters: { q: "", status: "any" }
routes:
  list:
    path: /list
    title: «список»
    usdl: ui/list.usdl.json
    prefetch:
      - query: orders.list
        params: { q: $.filters.q, status: $.filters.status, page: 1 }
  details:
    path: /:orderId
    title: «детали»
    usdl: ui/details.usdl.json
    guards:
      - hasParam: orderId
    prefetch:
      - query: orders.byId
        params: { id: $route.orderId }
  create:
    path: /create
    title: «новый заказ»
    usdl: ui/create.usdl.json
menu:
  - route: routes.list
  - route: routes.create
```

### пример USDL-страницы (листинг)

```json
{
  "version": "usdl/0.2",
  "layout": {"slot":"main"},
  "viewBind": "$.filters",
  "components": [
    {"type":"Toolbar","items":[
      {"type":"Input","label":"поиск","bind":"$.q","debounceMs":300,
       "on":[{"event":"change","action":"reload"}]},
      {"type":"Select","label":"статус","bind":"$.status",
       "options":[{"id":"any","text":"любой"},{"id":"open","text":"открыт"},{"id":"closed","text":"закрыт"}],
       "on":[{"event":"change","action":"reload"}]},
      {"type":"Button","text":"создать","navigate":{"to":"routes.create"}}
    ]},
    {"type":"Table","id":"tbl",
     "columns":[
       {"key":"id","title":"№"},
       {"key":"customer","title":"клиент"},
       {"key":"total","title":"сумма"},
       {"key":"status","title":"статус"}
     ],
     "data":{"source":"lazy","query":"orders.list","params":{"q":"$.q","status":"$.status"}},
     "on":[{"event":"rowClick","navigate":{"to":"routes.details","params":{"orderId":"$row.id"}}}]
    }
  ],
  "actions":{
    "reload":{"emit":"ui.query","target":"orders.list","params":{"q":"$.q","status":"$.status"}}
  }
}
```

### права и безопасность

* сценарий явно **декларирует** какие контракты навыков использует → это и есть его **scope**.
* фронт применяет **CSP** и sandbox для UMF-страниц (если есть).
* доступ к данным — только через шину; прямых HTTP-ключей в USDL/ULM нет.

### мультисценарность

* одновременно можно открыть несколько сценариев в разных **workspaces** (вкладки/сплиты).
* глобальный «центральный» стор охраняет **изоляцию**: `viewModel` и кеши per-scenario.

### офлайн/история/глубокие ссылки

* отдельный **persist layer** per-scenario (краткоживущий кеш и pinned state).
* deep links: `/s/orders/123` восстанавливает нужный маршрут, делает `prefetch`.
* replay для действий, сделанных офлайн (маркируем действия идемпотентными, где возможно).

### i18n и темы

* все пользовательские строки в USDL — через i18n-ключи; ключи хранятся у сценария.
* тема сценария — палитра токенов; системные темы можно переопределять частично.

### DX: как с этим живёт llm-программист

* генерирует **scenario.yaml** (маршруты/меню/контракты/guards).
* пишет ULM/USDL файлы по маршрутам (чистая декларативка).
* описывает контракты навыков через JSON-Schema (вызовы `ui.action/ui.query`).
* рендерер и SDK дают **шаблоны** и автодополнение; ошибки валидации — человекочитаемые.

### автогенерация UI «по умолчанию»

* если у сценария есть только **контракты** и **schemas**, движок может **собрать черновик UI**:

  * генерация таблиц/форм по схемам,
  * базовое меню из списка маршрутов,
  * далее сценарий постепенно заменяет автосгенерированные страницы своими USDL.
