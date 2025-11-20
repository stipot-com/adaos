# 1) Где что лежит

## В кодовой базе AdaOS

```
src/adaos/scenario_templates/          # dev-templates для "Install from template" (локальная песочница)
src/adaos/agent/core/scenario_engine/   # каркас движка сценариев (ниже дам интерфейсы)
src/adaos/agent/api/scenarios.py        # REST-API хаба
```

## На хабе (во время работы)

```
/var/lib/adaos/scenarios/prototypes/    # git clone общего репозитория сценариев (P-слой)
/var/lib/adaos/scenarios/bindings/      # привязки устройств/секреты (на хабе), шифруем
~/.adaps/scenarios/impl/<hub-id>/<user>/ <scenario-id>/<version>/scenario.json   # I-слой (rewrite)
~/.adaps/scenarios/index.json           # быстрый индекс установленных сценариев, версий и привязок
```

> **Templates** из `src/adaos/scenario_templates/` нужны только для «создать новый» и локальной разработки.
> Боевые **prototypes** ставим из отдельного git-репо (например, `github.com/adaos-scenarios/prototypes`).

# 2) Модель: T / P / I

* **Template (T)** — черновик в исходниках (локально).
* **Prototype (P)** — обобщённая реализация в *общем git-репо*; ставится на хаб; обновляется по semver.
* **Implementation (I)** — *персональная специализация* у пользователя на хабе (rewrite). **Только сужение**, без добавления узлов/новых возможностей. LLM модернизирует **P**, не видя I (там персональные данные).

## Правило слияния

Эффективный сценарий = `I ⭡ P` (глубокий merge с ограничениями):

* I может: менять параметры, IO-настройки, упорядочивать/пропускать шаги, ограничивать способности, переопределять биндинги устройств/секретов.
* I не может: добавлять новые шаги, новые слоты, новые якоря/ветвления.

# 3) DSL прототипа (P) — компактно

```json
{
  "id": "morning",
  "version": "1.2.0",
  "name": "Доброе утро",
  "multiUser": true,
  "triggers": [
    { "type": "time", "cron": "0 8 * * *", "runOn": "hub" },
    { "type": "intent", "name": "good_morning", "runOn": "member:self" }
  ],
  "slots": {
    "weather": { "cap": "weather" },
    "news":    { "cap": "news", "optional": true }
  },
  "params": { "city": "${ctx.user.city}" },
  "io": { "input": ["voice","text"], "output": ["voice","text"], "settings": { "output.active": "voice" } },
  "steps": [
    { "id":"hello", "do":"say",  "text":"Доброе утро, ${ctx.user.name}!" },
    { "id":"w",     "do":"call", "slot":"weather", "method":"get", "args":{"city":"${params.city}"}, "alias":"w" },
    { "id":"w.say", "do":"say",  "text":"Сейчас ${w.temp}°C, ${w.description}" },
    { "id":"n",     "do":"call", "slot":"news", "method":"top", "args":{"limit":3}, "alias":"n",
      "if":"${hasSlot('news')}" },
    { "id":"n.for", "do":"for", "each":"${n.articles}", "steps":[ { "do":"say", "text":"— ${item.title}" } ] }
  ],
  "anchors": {
    "after_weather": { "accept": ["skill:calendar"], "after": "w.say" }
  }
}
```

# 4) DSL имплементации (I) — rewrite (сужение)

```json
{
  "rewriteVersion": 1,
  "params": { "city": "Potsdam", "language": "ru" },
  "io": { "settings": { "output.active": "voice", "voice.id": "ru_female" } },
  "bindings": {
    "slots": { "weather": { "skill": "open_weather" }, "news": { "skill": "newsapi" } },
    "devices": { "tts": { "device_id": "livingroom_speaker" } }
  },
  "rewrite": [
    { "match": { "id": "n" },       "action": "drop" },                 // не хочу новости
    { "match": { "id": "w.say" },   "action": "param", "set": { "text": "На улице ${w.temp}°C" } },
    { "match": { "id": "hello" },   "action": "move_after", "ref": "w" } // переставить шаги
  ]
}
```

**Допустимые действия**: `drop`, `move_before`, `move_after`, `param.set`, `io.set`, `cap.narrow` (например, `output` только `voice`).
**Нельзя**: `add_step`, `add_slot`, `add_anchor`.

# 5) Управление жизненным циклом (instances)

Делаем полноценный **контрольный план** на хабе:

* Каждому запуску сценария присваиваем `instance_id`.
* Состояние: `running | paused | stopping | stopped | failed`.
* Команды: `start`, `stop`, `pause`, `resume`, `kill`.
* Метки/активности: шаги могут регистрировать `activityId` (например, `video:livingroom`).

  * Тогда можно `stop?activity=video:livingroom` — и движок пошлёт в соответствующий узел/навык «stop».

### API (agent)

```
GET  /api/scenarios/prototypes                # список P с версиями (из git)
POST /api/scenarios/prototypes/install        # { id, version } -> поставить/обновить P
GET  /api/scenarios/impl/{user}/{id}          # текущая I (rewrite)
PATCH /api/scenarios/impl/{user}/{id}         # обновить I (только допустимые действия)
POST /api/scenarios/{id}/run                  # { user, ioOverride? } -> { instance_id }
POST /api/scenarios/instances/{iid}/stop      # graceful cancel
POST /api/scenarios/instances/{iid}/kill      # без условий
POST /api/scenarios/instances/stopByActivity  # { activity: string }
GET  /api/scenarios/instances                 # активные/последние, метрики, последние ошибки
```

**Важные детали:**

* **CancellationToken** прокидывается в каждый шаг/навык; на bus: `scenario.cancel.<iid>`.
* **Timeouts/heartbeats**: долгие шаги обязаны «пинговать» движок.
* **ConcurrencyPolicy**: `single`, `queue`, `replace` (что делать при повторном триггере).
* **Idempotency**: шаги с побочками получают `idempotency_key` (iid+stepId+seq).
* **DLQ**: неуспешные сообщения/шаги — в отложенную очередь с возможностью «replay».

# 6) Привязки устройств и способностей (binder)

* На хабе есть **реестр устройств/навыков** с `capabilities` (мик, tts, display, camera, weather, news…).
* **Bindings** в I слое сопоставляют слоты/устройства конкретным реализациям.
* При подключении новой ноды движок *не добавляет шаги*, а **перебиндивает** существующие слоты, если `cap` совпал (или предлагает пользователю — через UI — обновить P, если хочется новых возможностей).

# 7) Git-поток для прототипов

* Отдельный репозиторий `adaos-scenarios`:

  ```
  prototypes/<vendor_or_namespace>/<scenario-id>/
    manifest.json   # id, versions, changelog, caps, anchors
    1.2.0/scenario.json
    1.2.0/README.md
  ```
* Хаб держит clone под `/var/lib/adaos/scenarios/prototypes/`.
* Обновления: `fetch → semver select → миграции`:

  * миграционный слой `upgrades.py` проверяет, не ломает ли новый P текущие I-rewrite (валидация «не добавлены ли шаги, которые I пытается удалять/переставлять вне правил»); где нужно — автокоррекция/подсказки.

# 8) Интерфейс в Inimatic (HUB)

* **Scenarios**:

  * список P (с версиями), индикатор «есть локальная I», кнопки `Run / Stop / Update`;
  * редактор I (rewrite) с валидацией (только допустимые действия);
  * мастер **Bindings** (slots → skills/devices, секреты).
* **Instances**:

  * таблица активных/последних: сценарий, пользователь, состояние, длительность, activityId;
  * кнопки: `Stop / Kill / Pause / Resume / Stop by activity`;
  * поток логов/трейсов (tap).

# 9) Как гасить «длящиеся» сценарии (пример «останови видео»)

В шаге, который стартует воспроизведение, задаём `activityId`:

```json
{ "id":"play", "do":"call", "slot":"media", "method":"play",
  "args":{"url":"...","screen":"livingroom"}, "activityId":"video:livingroom" }
```

Движок регистрирует `iid↔activityId`. Команда:

```
POST /api/scenarios/instances/stopByActivity { "activity": "video:livingroom" }
```

Движок находит `iid`, шлёт отмену (cancel token) и при необходимости — «компенсационный» шаг: `call media.stop`.

# 10) Что добавить/уточнить (чтобы ничего не упустить)

* **ACL/Scope**: сценарии и I-персонализации — по пользователям; доступ к управлению — по ролям (admin, adult, kid, guest).
* **Локализация**: `params.language`, `say` учитывает язык/голос из I.
* **Безопасность**: секреты в bindings — шифруем (ключ хаба), LLM не видит I.
* **Тесты**: быстрый dry-run с фиктивными провайдерами (моки `weather/news`).
* **Совместимость**: строгая схема JSON (pydantic), versioned DSL (`dslVersion`), чёткие ошибки валидации.

---

## Мини-каркас кода (куда вставить)

**`src/adaos/agent/core/scenario_engine/dsl.py`** — pydantic-модели `Prototype`, `ImplementationRewrite`, валидаторы ограничений (I без add_step).
**`store.py`** — работа с диском/гитом (P) и локальными I.
**`binder.py`** — резолв слотов по `capabilities`.
**`runtime.py`** — исполнение шагов, токены отмены, idempotency.
**`api/scenarios.py`** — REST эндпойнты, отдаёт IDs/версии, старт/стоп/инстансы.
