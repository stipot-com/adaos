# единое адресное пространство данных (DAI)

## 1) унифицированный URI

```
ada://<scope>/<resource>?<query>
```

* **scope** — где «живет» источник:

  * `vm` — память сценария (viewModel)
  * `user` — долговременная память пользователя
  * `skill` — контракт навыка (тонкий прокси к внешним API/БД)
  * `media` — встроенное меди-хранилище
  * `tmp` — эфемерные наборы (вью-кеш, результаты вычислений)
* **resource** — имя коллекции/объекта (snake_case)
* **query** — плоские параметры (строки/числа/булевы), сложные — в `params` тела запроса

Примеры:

* `ada://vm/tasks` — список задач в памяти сценария
* `ada://user/prefs` — пользовательские предпочтения
* `ada://skill/tasks.list` — контракт «выдай задачи»
* `ada://media/objects` — поиск медиа-объектов

> замечание: `skill/<contract>` всегда **глагол.сущ**: `orders.list`, `orders.create`, `tasks.update_status`.

## 2) минимальный протокол поверх URI (CQRS-лайт)

* **Чтение**: `Data.get(uri, adaql?)` → `DataPage`
* **Команда**: `Data.exec(uri, payload)` → `Ack | Error`
* **Поток (опц.)**: `Data.subscribe(uri, adaql?)` → патчи коллекции

```ts
type DataPage<T=any> = { items: T[], cursor?: string, total?: number, stale?: boolean };
type Ack = { ok: true, id?: string, patch?: any };
type Err = { ok: false, code: string, message: string, details?: any };
```

## 3) AdaQL/0.1 (MVP)

декларативный JSON-запрос, одинаковый для всех scope’ов:

```json
{
  "pick": ["id","title","status","priority","due","assignee"],
  "filter": [{"field":"status","op":"in","value":["todo","doing"]}],
  "sort": [{"field":"priority","dir":"desc"},{"field":"updatedAt","dir":"desc"}],
  "page": {"size": 50, "cursor": null},
  "groupBy": null,
  "calc": {"overdue": {"expr": "due && now()>due"}}
}
```

* исполняется **как можно ближе к источнику**; что не умеет источник — дорежем на клиенте.
* одинаков для `vm`, `user`, `skill`, `media`.

## 4) проекции (views)

один и тот же результат AdaQL можно отобразить по-разному, меняя только projection:

```json
{
  "renderer": "Table|Cards|Kanban",
  "projection": {
    "cards": {"title":"title","subtitle":"assignee","chips":["status","priority"]},
    "kanban": {"columnBy":"status","order":["todo","doing","done"],"swimlanesBy":"assignee"}
  }
}
```

переключатель вида не меняет `uri` и `adaql`, только `renderer+projection`.

# роли навыков и реестр контрактов

## 5) навыки как провайдеры

* каждый контракт навыка — **функция данных** с сигнатурой:

  * Query: `input params → DataPage | Stream`
  * Command: `input payload → Ack`
* навык сам решает, идти в REST/GraphQL/DB/S3; фронту всё равно.

## 6) реестр контрактов (manifest)

централизовано объявляем «что есть»:

```yaml
# contracts.yaml
contracts:
  - name: tasks.list
    scope: skill
    input_schema: ref:schema/tasks_list_in.json
    output_schema: ref:schema/tasks_list_out.json
    capabilities: [server_sort, server_filter, cursor]
    rate_limit: { burst: 5, per_minute: 60 }
  - name: tasks.update_status
    scope: skill
    input_schema: ref:schema/tasks_update_status.json
    command: true
```

рендерер/SDK читают манифест для валидации и DX (подсказки llm).

# встроенные пространства и хранилище

## 7) `vm://` и `user://`

* `vm` — реактивная память сценария; поддерживает `get/patch`, выборки AdaQL по объектам/массивам.
* `user` — KV/коллекции в IndexedDB + синк на сервер по возможности.

URI короткие алиасы:

* `ada://vm/tasks` = `vm://tasks`
* в сценариях можно писать коротко — движок нормализует в `ada://…`

## 8) `media://` (минимальный медиасервер)

* объектное хранилище + индекс в БД
* контракты:

  * `media.search` → `ada://media/objects`
  * `media.put` / `media.get` / `media.delete`
* адреса объектов: `media://obj/<sha256>`; выдаём подписанный URL для проигрывания
* AdaQL фильтры по `kind`, `mime`, `tags`, `meta.*`, датам; сортировка по `created_at`, `size`

# производительность, кеш и консистентность (без перегруза)

## 9) трёхуровневый кеш

* **L1 (RAM)** — результат последнего `get` по ключу `(uri, adaqlHash)`
* **L2 (IndexedDB)** — пин-кеш per-scenario (флажок `cache:"persistent"`)
* **L3 (server/edge)** — по метке `cacheable` в контракте (опционально)
  политики: `staleMs`, `revalidateOnFocus`, `replayOffline` для команд.

## 10) курсоры и патчи

* все провайдеры уважают `page.size` и возвращают `cursor`
* обновления коллекций — через `ui.patch` с операциями `add/replace/remove` (JSON Patch)

## 11) ошибки и деградация

единая модель ошибок:

```
code: TIMEOUT | UNAUTHORIZED | RATE_LIMITED | BAD_REQUEST | BACKEND_UNAVAILABLE | VALIDATION_FAILED | CONFLICT
```

UI-движок показывает «человечные» сообщения и предлагает повтор/смену вида (например, с Kanban на List при перегрузе).

# безопасность и доступы

## 12) scope & ACL

* сценарий объявляет в `scenario.yaml` только те контракты, что использует → минимальный scope
* токены/секреты **только на стороне навыка**; фронт видит лишь `ada://skill/<contract>`
* rate-limit и audit-лог на шине `ui.query/ui.action`

# DX для llm-программиста

## 13) единый SDK

```ts
// чтение
const page = await Data.get('ada://skill/tasks.list', {
  filter:[{field:'status',op:'in',value:['todo','doing']}],
  sort:[{field:'priority',dir:'desc'}],
  page:{size:50}
});
// команда
await Data.exec('ada://skill/tasks.update_status', { id, status:'doing' });
```

* один импорт, один способ обращаться к данным — независимо от источника
* в USDL/ULM достаточно указать `data: { uri, adaql, cache }`

## 14) шаблоны (cookbook)

1. **Список задач (Table)**: `uri=ada://skill/tasks.list` + AdaQL + `renderer: Table`
2. **Карточки**: те же `uri+adaql`, другая `projection.cards`
3. **Канбан**: те же `uri+adaql`, `projection.kanban` + `Data.exec(tasks.update_status)` на dnd

# MVP-чеклист (что реально делаем сейчас)

* [ ] Нормализатор URI + резолвер провайдеров (`vm`, `user`, `skill`, `media`)
* [ ] `Data.get/exec` + простейший AdaQL (pick/filter/sort/page)
* [ ] L1-кеш и курсоры, единая ошибка
* [ ] USDL поля `data.uri`, `data.adaql`, `renderer`, `projection`
* [ ] Три рендерера: `Table`, `Cards`, `Kanban` (без излишеств)
* [ ] Реестр контрактов + валидация входа/выхода
* [ ] Мини-`media_service` с `media.search` и `media.get`
* [ ] Переключатель вида (ViewSwitch) — без перезапроса, если не требуется

> всё остальное (IndexedDB-кеш, live-патчи, агрегации) — следующими итерациями, без слома API.
