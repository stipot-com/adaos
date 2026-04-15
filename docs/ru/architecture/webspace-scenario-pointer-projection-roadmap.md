# Переход Переключения Сценариев Webspace К Pointer/Projection

Этот документ фиксирует целевую архитектуру и дорожную карту миграции
переключения сценариев в Yjs-backed webspace.

Цель: перевести подсистему от legacy-модели `materialize-and-copy` к модели
`pointer + resolved projection`, не ломая текущий renderer, recovery-потоки и
операторские control surface.

Цель этого перехода не в том, чтобы убрать rebuild как класс операции.
Цель — сохранить semantic rebuild как backend-owned reconcile primitive,
убрать его из latency-critical части полного копирования payload и эволюционно
довести его до fragmented, phase-aware materialization.

## Статус

- Текущая реализация: `pointer_only` по умолчанию; semantic rebuild владеет
  effective runtime branches, а switch-time compatibility cache writes убраны
  из hot path
- Целевая реализация: `pointer + resolved projection`
- Режим миграции: поэтапный, с совместимостью по умолчанию

## Implementation Anchors

- Runtime resolver + semantic rebuild owner:
  `src/adaos/services/scenario/webspace_runtime.py`
- Проекция сценария в compatibility cache:
  `src/adaos/services/scenario/manager.py`
- Bootstrap seed + rebuild nudge:
  `src/adaos/services/yjs/bootstrap.py`
- Диагностика и benchmark:
  `src/adaos/apps/api/node_api.py`,
  `src/adaos/apps/cli/commands/node.py`
- Регрессионное покрытие:
  `tests/test_webspace_phase2.py`,
  `tests/test_node_yjs_phase2.py`,
  `tests/test_push_registry_sync.py`,
  `tests/test_yjs_bootstrap.py`

Следующие PR по разделам 3, 5 и 6 должны ссылаться на эту roadmap и явно
указывать, какие checklist-пункты они двигают.

## Формулировка проблемы

Сейчас `switch_webspace_scenario()` совмещает две разные ответственности:

- выбор активного сценария для рантайма
- материализацию payload сценария в Yjs

На практике одно переключение сценария сейчас:

- загружает целевой `scenario.json`
- пишет payload сценария в совместимые ветки Yjs:
  `ui.scenarios`, `registry.scenarios`, `data.scenarios`
- пишет активные runtime-ветки:
  `ui.application`, `registry.merged`, `data.catalog`
- затем запускает rebuild, который повторно вычисляет и пишет это effective
  состояние

Это создаёт несколько проблем:

- latency переключения растёт вместе с размером payload сценария
- Yjs получает лишний churn
- на обычном switch path одни и те же данные пишутся дважды
- размываются границы между canonical, derived и live state
- rebuild остаётся связан с legacy materialized cache внутри Yjs

## Целевая архитектура

Целевая модель разделяет pointer state и resolved runtime state.

Она также перестаёт считать видимый интерфейс единым all-or-nothing объектом
materialization и вводит фазовую готовность runtime state.

### Canonical и live inputs

Авторитетными входами semantic rebuild для webspace должны быть:

- `WebspaceManifest` и связанная persistent metadata
- `home_scenario`
- `ui.current_scenario` как live runtime selection
- содержимое сценария из `scenario.json`
- UI-contribution навыков из `webui.json`
- projection-конфигурация из `scenario.yaml` и `skill.yaml`
- persistent overlay и текущий runtime state там, где это применимо

### Derived outputs

Следующие ветки являются runtime projection, а не primary storage:

- `ui.application`
- `ui.application.structure`
- `ui.application.data`
- `data.catalog`
- `data.installed`
- `data.desktop`
- `registry.merged`

Это пока концептуальное разделение, а не требование к немедленному жёсткому
schema break. Важны сами границы:

- shell/structure могут становиться ready раньше
- data-heavy и enrichment-heavy части могут приходить позже
- off-focus страницы, вкладки, панели и модалки могут гидратироваться
  отложенно

### Compatibility caches

Во время миграции ветки ниже могут существовать, но больше не должны
рассматриваться как обязательный source of truth:

- `ui.scenarios`
- `registry.scenarios`
- `data.scenarios`

Они становятся только compatibility cache.

## Управляющие правила

1. Переключение активного сценария должно прежде всего менять pointer, а не
   материализовывать весь payload сценария.
2. Effective UI и catalog state должны вычисляться одним semantic rebuild
   pipeline.
3. Rebuild должен уметь собирать сценарий напрямую из loader-backed canonical
   sources, не завися от materialized payload в Yjs.
4. Compatibility-ветки могут сохраняться во время миграции, но rebuild не
   должен в них нуждаться.
5. Yjs остаётся live collaborative medium; этот план меняет ownership и
   rebuild semantics, но не transport layer.
6. Переключение сценария должно оптимизироваться не только по
   `time_to_fully_materialized_state`, но и по `time_to_first_structure`.
7. Renderer contract должен допускать phased readiness для staged и
   многостраничных сценариев, где часть поверхностей не находится в фокусе.
8. Целевое состояние для scenario switch — это fragmented rebuild:
   один semantic owner, но с раздельным применением focused и deferred slices.

## Таксономия источников

### Canonical

- persistent metadata webspace
- `scenario.json`
- skill `webui.json`
- projection-декларации в `scenario.yaml` / `skill.yaml`

### Live

- `ui.current_scenario`
- workflow/runtime state
- ephemeral collaborative state

### Derived

- `ui.application`
- structure-first compatibility projections
- deferred data/enrichment projections
- `data.catalog`
- `data.installed`
- `data.desktop`
- `registry.merged`
- опциональные compatibility cache под `ui.scenarios`, `registry.scenarios`,
  `data.scenarios`

## Целевой контракт switch

В целевой архитектуре `desktop.scenario.set` должен делать только следующее:

1. Проверить, что запрошенный сценарий существует в нужном source space.
2. Обновить `ui.current_scenario`.
3. При необходимости обновить `home_scenario`.
4. Запустить или запланировать semantic rebuild.
5. Быстро вернуть ответ вместе с rebuild state metadata.

Switch не должен eagerly копировать весь payload сценария в active runtime
tree.

## Целевой контракт rebuild

Semantic rebuild становится единственным владельцем materialization effective
runtime state.

Его ответственность:

1. Разрешить source mode для целевого webspace.
2. Разрешить активный сценарий из explicit override или `ui.current_scenario`.
3. Загрузить содержимое сценария напрямую из loader-backed canonical source.
4. Загрузить skill UI contributions и overlay state.
5. Обновить projection rules как явный ordered step.
6. Вычислить phased resolved runtime state с явным разделением на
   structure-first и deferred hydration slices.
7. Применить first-paint structure и critical interaction state в
   compatibility Yjs branches.
8. Применять deferred data, enrichments и off-focus surfaces тогда, когда они
   становятся eligible.
9. Публиковать rebuild status и completion signals вместе с phase-aware
   metadata.

Целевая backend-операция здесь ближе к cancellable reconcile pipeline, чем к
одному монолитному rebuild-вызову:

1. pointer update / scenario selection
2. structure-first projection
3. interactive essentials
4. deferred hydration для background и off-focus surfaces

Внутренняя очередь может использоваться для coalescing, cancellation и
подавления stale work, но она не должна становиться архитектурным центром.

Transport recovery и semantic recovery здесь тоже должны оставаться разными
контурами:

- autoreconnect websocket/provider остаётся transport-ответственностью
- frontend recovery controls могут запрашивать semantic reload, но не должны
  агрессивно пересоздавать sync provider при каждом кратком disconnect

## Модель фокуса и стадий

Целевой switch/rebuild path должен поддерживать staged и многостраничные
интерфейсы в духе Prompt IDE:

- только текущая страница, pane или активный shell segment должны блокировать
  first-paint readiness
- неактивные tabs, background pages, secondary panels и unopened modals могут
  materialize позже
- сценарий может быть частично ready, и это должно быть явно отражено в
  readiness contract

Предпочтительная лестница readiness:

- `pending_structure`
- `first_paint`
- `interactive`
- `hydrating`
- `ready`
- `degraded`

Это пока contract-level модель. Точные имена полей могут уточняться вместе с
эволюцией renderer и ABI.

## Стратегия миграции

Миграция должна сохранять совместимость для текущих UI/runtime consumers до
тех пор, пока новый resolver path не станет стабильным.

### Phase A: Loader-first resolve

Rebuild начинает читать scenario content прежде всего из loader-backed
canonical sources и использует materialized Yjs scenario branches только как
legacy fallback.

### Phase B: Pointer-first switch

Переключение сценария перестаёт писать полный payload в active runtime
branches и обновляет только active pointer и minimal metadata.

### Phase C: Demotion legacy cache

Legacy-ветки materialization сценария становятся опциональными cache и больше
не нужны для normal rebuild operation.

### Phase D: Projection caching и diff apply

Запись resolved projection становится более выборочной:

- пропускать write, если значение не изменилось
- делать top-level diff application там, где это дёшево и надёжно
- кешировать resolver inputs и/или outputs по стабильному version key

Временные operator-facing diagnostics уже начали публиковать timing snapshots
в switch/rebuild results и rebuild status surfaces, чтобы тяжёлые сценарии
можно было сравнивать до более глубоких cache/diff изменений.

Control surface теперь также публикуют provisional `phase_timings_ms`
(`time_to_accept`, `time_to_first_structure`,
`time_to_interactive_focus`, `time_to_full_hydration`), вычисленные на основе
пока ещё в основном монолитного runtime-контракта. Эти числа пока намеренно
best-effort, но уже позволяют сравнивать pointer-first acceptance и полный
rebuild latency на реальных сценариях.

Загрузка scenario content/manifest и skill `webui.json` теперь также опирается
на file-stamp-aware in-process cache, что уменьшает повторную стоимость
парсинга при частых rebuild, но при этом позволяет подхватывать изменения на
диске без перезапуска процесса.

### Phase E: Structure/data split и focus-aware hydration

Контракт rebuild становится явно фазовым:

- определить structure-first и deferred branches
- формализовать readiness signals для partially available UI
- разрешить off-focus page/panel/modal materialization отставать от first
  render
- сохранить compatibility paths на время миграции frontend-контрактов

## Дорожная карта

Используй этот checklist как основной трекер прогресса миграции.

### 0. Архитектурная база

- [x] Зафиксировать целевую архитектуру и правила миграции в отдельном
  документе.
- [x] Привязать все связанные implementation notes и будущие PR к этой
  дорожной карте.

### 1. Развязка resolver

- [ ] Провести аудит всех чтений `ui.scenarios`, `registry.scenarios` и
  `data.scenarios`.
- [ ] Найти consumers, которым нужен canonical loader-backed read вместо
  materialized payload из Yjs.
- [ ] Переписать сбор resolver inputs так, чтобы loader-backed scenario content
  стал primary source.
- [ ] Сохранить materialized scenario branches в Yjs только как fallback.
- [ ] Добавить тесты, доказывающие, что rebuild проходит без
  pre-materialized scenario payload в Yjs.

### 2. Упрощение контракта switch

- [x] Ввести pointer-first switch path под feature flag.
- [ ] Сделать так, чтобы `switch_webspace_scenario()` проверял существование
  сценария без eager full payload copy.
- [ ] Ограничить writes в switch до `ui.current_scenario` и minimal metadata.
- [ ] Сохранить поведение `set_home` и dev-webspace policy.
- [ ] Сделать rebuild status наблюдаемым во время asynchronous switch
  reconcile.

### 3. Единый владелец semantic rebuild

- [x] Сделать semantic rebuild единственным владельцем effective writes в
  `ui.application`, `data.catalog`, `data.installed`, `data.desktop`,
  `registry.merged`.
- [ ] Убрать duplicated writes между switch и rebuild paths.
- [ ] Сохранить синхронизацию workflow/runtime state с rebuilt active
  scenario.
- [ ] Проверить, что reload/reset/restore/switch используют одинаковые source
  resolution rules.
- [ ] Переформатировать rebuild в phase-aware reconcile steps вместо одного
  монолитного предположения "all ready at once".

### 4. Совместимость и безопасность миграции

- [ ] Сохранить читаемость compatibility cache во время миграции.
- [ ] Добавить явные debug-сигналы, когда используется legacy fallback path.
- [ ] Определить критерии удаления legacy scenario materialization.
- [ ] Проверить, что текущие frontend contracts продолжают работать с
  pointer-first switch.
- [ ] Описать failure modes и rollback path, если новый switch contract даст
  регрессию.
- [ ] Определить compatibility contract для partially ready UI, чтобы
  renderer отличал degraded state от intentional deferred hydration.

### 5. Производительность и hardening

- [x] Добавить per-stage timing вокруг switch, scenario load, projection
  refresh, resolve и apply.
- [x] Добавить `skip-if-unchanged` для больших derived writes.
- [ ] Добавить cache keys для scenario content, skill UI contributions и
  overlay snapshot там, где это полезно.
- [ ] Добавить diff-apply для top-level resolved branches там, где реализация
  остаётся простой и безопасной.
- [ ] Мерить тяжёлые сценарии вроде `infrascope` до и после каждого среза.
- [x] Добавить метрики `time_to_first_structure`,
  `time_to_interactive_focus` и `time_to_full_hydration`.
- [x] Коалесить stale background hydration work, если его уже supersede'нул
  новый scenario switch.

### 5.5. ABI и renderer readiness contract

- [x] Расширить `src/adaos/abi/webui.v1.schema.json` intent-level readiness
  hints вместо протаскивания в ABI backend scheduler details.
- [x] Ввести coarse-grained load semantics для page/widget/modal/catalog
  сегментов: например eager, visible, interaction и deferred.
- [x] Держать granularity выше leaf-node уровня, чтобы runtime не превратился
  в micro-scheduler для каждого control.
- [x] Разделить hints жизненного цикла structure и data, чтобы staged
  rendering оставался управляемым.
- [x] Описать, как staged и многостраничные сценарии сообщают off-focus
  readiness, не делая вид, что весь shell обязан быть заблокирован.

### 6. Очистка legacy

- [ ] Перестать считать `ui.scenarios`, `registry.scenarios` и
  `data.scenarios` обязательными runtime inputs.
- [ ] Понизить эти ветки до optional compatibility cache.
- [x] Удалить obsolete switch-time materialize-and-copy code paths.
- [ ] Обновить архитектурные и операторские документы под новую ownership
  model.

## Критерии успеха

Миграцию можно считать успешной, когда выполняются все условия ниже:

- latency переключения сценария больше не зависит линейно от полной
  materialization payload
- `time_to_first_structure` улучшается независимо от
  `time_to_full_hydration`
- rebuild работает от canonical loader-backed source даже если Yjs scenario
  cache отсутствует
- effective runtime state записывается одним semantic rebuild owner
- control surface по-прежнему позволяют диагностировать asynchronous rebuild
- текущее frontend/runtime поведение остаётся совместимым или имеет явный
  migration path
- staged и многостраничные сценарии могут рендерить focused surface до того,
  как off-focus surfaces закончат hydration

## Что не входит в этот план

Этот roadmap не предлагает:

- замену Yjs как live collaborative runtime medium
- редизайн `scenario.json` или skill `webui.json`
- обязательное описание fine-grained scheduler details в каждом UI
  contribution
- отказ от semantic rebuild как operator/recovery primitive
- одномоментную замену current renderer contract
- single-shot rewrite `WebspaceScenarioRuntime`

Путь остаётся эволюционным:

> Сначала перенести ownership, потом убрать дублирование, потом ускорять.
