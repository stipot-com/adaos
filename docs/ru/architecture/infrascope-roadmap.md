# Infrascope Roadmap

Эта дорожная карта разбивает `Infrascope` на последовательные итерации, которые можно реализовывать поверх текущего кода AdaOS.

Архитектура зафиксирована в [Infrascope](infrascope.md). Здесь описаны порядок, deliverables и критерии готовности.

## Принципы последовательности

1. Сначала контракты, потом экраны.
   Каноническая модель объекта и projections должны появиться раньше специальных UI-виджетов.
2. Переиспользовать текущие runtime producers.
   Node, runtime, Yjs, workspaces и observe-сервисы должны стать источниками нового control plane.
3. Польза для оператора важнее визуальной сложности.
   `overview`, `inventory` и inspector должны появиться раньше амбициозного полного графа.
4. LLM-совместимость с первого дня.
   Каждая итерация должна улучшать машиночитаемый контекст, а не откладывать его на потом.
5. Governance — часть объектной модели.
   Ownership, visibility и policy нельзя переносить на этап после сборки интерфейса.

## Опорные сценарии

Каждая итерация должна улучшать хотя бы один из этих сквозных сценариев:

1. Разобраться, почему перестал работать сценарий.
2. Определить, вызвана ли поломка автоматизации root, hub, member, browser или device.
3. Показать impact перед перезапуском hub или обновлением skill.
4. Объяснить, какой объект превысил LLM или external-service квоту.
5. Подготовить безопасный, policy-aware context packet для LLM assistant.

## Итерация 0: Канонический словарь

### Цель

Создать стабильный контракт объекта и общий словарь статусов, связей и действий.

### Deliverables

- определить `CanonicalObject` и реестр поддерживаемых `kind`
- нормализовать status и health facets для root, hub, member, browser, device, skill, scenario, runtime и quota
- зафиксировать relation kinds вроде `parent`, `depends_on`, `hosted_on`, `connected_to`, `uses` и `affects`
- определить формальные action descriptors с риском и metadata по правам

### Точки опоры

- `src/adaos/services/reliability.py`
- `src/adaos/services/node_config.py`
- `src/adaos/services/workspaces/index.py`
- `src/adaos/services/user/profile.py`
- `src/adaos/apps/api/node_api.py`
- `src/adaos/apps/api/skills.py`
- `src/adaos/apps/api/scenarios.py`

### Готово, когда

- каждый базовый тип объекта можно представить через `id`, `kind`, `title`, `status`, `relations` и `governance`
- одинаковые статусные слова значат одно и то же в node- и runtime-API

### Текущий срез реализации

Первый кодовый срез для Iteration 0 намеренно узкий:

- добавить общий canonical vocabulary и object envelope в `src/adaos/services/system_model/*`
- добавить adapter functions для текущих форм node, skill, scenario, profile и workspace
- добавить SDK entry points для canonical self, skill, scenario и reliability projection objects и оставить API тонким фасадом
- сначала проверить словарь и адаптеры точечными unit tests, а уже потом расширять покрытие API

Текущий checkpoint распространяет тот же envelope на selected reliability snapshots, добавляет canonical coverage для workspace/profile/browser-session/capacity objects и вводит local inventory projection для SDK/LLM use. Единственным новым external facade остаётся aggregate reliability projection, так что current raw responses не ломаются, а более широкий surface остаётся SDK-first.

## Итерация 1: Projection Layer

### Цель

Построить машиночитаемые projections, которые будут кормить и UI, и LLM workflows.

### Deliverables

- сервис object projection
- сервис topology projection
- narrative projection для summary и operator focus
- action projection для формальных безопасных действий
- task packet builder для LLM и automation

### Точки опоры

- `src/adaos/services/scenario/projection_service.py`
- `src/adaos/services/scenario/projection_registry.py`
- `src/adaos/apps/api/observe_api.py`
- `src/adaos/apps/api/subnet_api.py`
- `src/adaos/services/observe.py`
- `src/adaos/services/eventbus.py`

### Готово, когда

- выбранный объект можно получить как `object`, `neighborhood` и `task_packet`
- UI и LLM больше не зависят от отдельных сериализаторов под каждый тип объекта

## Итерация 2: Operator Workspace MVP

### Цель

Поставить первую реально полезную operator-facing версию control plane.

### Deliverables

- `overview` с health strip, active incidents, quota summary, active runtimes и recent changes
- `inventory` tabs для hubs, members, browsers, devices, skills и scenarios
- единый правый `object inspector`
- drill-down от incident к объекту без потери контекста

### Точки опоры

- `src/adaos/services/workspaces/*`
- `src/adaos/services/yjs/*`
- `src/adaos/services/scenario/webspace_runtime.py`
- текущие browser-facing workspace shells, уже работающие с Yjs state

### Готово, когда

- оператор может понять, где именно болит подсеть, меньше чем за пять секунд
- детали объекта открываются в контексте, а не как несвязанные страницы

## Итерация 3: Topology и Impact Mode

### Цель

Сделать видимыми связи, пути деградации и последствия изменений.

### Deliverables

- layered topology map для режимов `physical`, `connectivity`, `runtime`, `resource` и `capability`
- neighborhood isolation и graph filters
- dependency graph для выбранного объекта
- impact preview перед `restart`, `reconnect`, `update` или `disable`
- карта version и sync drift

### Точки опоры

- `src/adaos/services/subnet/link_manager.py`
- `src/adaos/services/subnet/link_client.py`
- `src/adaos/services/subnet/runtime.py`
- `src/adaos/services/root/service.py`
- `src/adaos/services/reliability.py`

### Готово, когда

- оператор видит не только упавший узел, но и путь отказа
- потенциально disruptive действия показывают затронутые объекты до выполнения

## Итерация 4: Runtime, Incidents и Resources

### Цель

Превратить `Infrascope` из статичной инвентаризации в полноценный операционный cockpit.

### Deliverables

- runtime timeline для выполнений scenarios и skills
- поверхности для failed runs, retries и event backlog
- incident model с impact radius и root-cause candidates
- views по ресурсам и квотам, привязанным к owning objects
- trace от browser или user action до scenario, skill и device effect

### Точки опоры

- `src/adaos/services/scenario/workflow_runtime.py`
- `src/adaos/services/skill/runtime.py`
- `src/adaos/services/skill/service_supervisor_runtime.py`
- `src/adaos/services/observe.py`
- `src/adaos/services/resources/*`
- `src/adaos/services/chat_io/telemetry.py`

### Готово, когда

- runtime failure можно пройти по execution и event flow
- всплеск квоты можно привязать к конкретному scenario, skill или subnet object

## Итерация 5: Profile-Aware Overlays

### Цель

Поддержать нескольких человеческих и машинных потребителей поверх одних и тех же canonical objects.

### Deliverables

- overlays для identities, profiles, workspaces, roles и ownership
- profile-aware стартовые страницы и дефолтные фильтры
- представления объекта для `operator`, `developer`, `household` и `llm`
- ownership и review metadata на изменениях и инцидентах

### Точки опоры

- `src/adaos/services/user/profile.py`
- `src/adaos/services/workspaces/index.py`
- `src/adaos/services/policy/*`
- Yjs overlays, уже присутствующие в workspace manifest

### Готово, когда

- один и тот же hub рендерится по-разному для admin, household user и LLM, но сохраняет общий `id` и `relations`
- видимость и действия задаются тем же governance layer, который формирует UI

## Итерация 6: LLM-Assisted Operations и Change Loop

### Цель

Перевести LLM из роли пассивного наблюдателя в управляемого участника диагностики и контролируемых изменений.

### Deliverables

- task packets с `desired_state`, `actual_state`, `gap` и `constraints`
- change proposals, ссылающиеся на формальные actions, а не на произвольные текстовые инструкции
- dry-run и impact simulation перед отправкой предложения
- review и approval flow для изменений, предложенных LLM
- recommendation и anomaly layers поверх структурированных projections

### Точки опоры

- projection services из Итерации 1
- governance и ownership overlays из Итерации 5
- текущие CLI- и API-control paths в `src/adaos/apps/cli`, `src/adaos/apps/api` и `src/adaos/sdk/manage/*`

### Готово, когда

- LLM может объяснить проблему, предложить изменение и передать reviewable action plan без обхода policy
- операторы могут принять, отклонить или доработать предложение внутри того же control plane

## Группировка по релизам

Итерации можно собрать в более крупные пользовательские релизы:

- `v1 / operational MVP`: Итерации 0-2
- `v2 / topology and runtime control`: Итерации 3-4
- `v3 / profile-aware and LLM-native control plane`: Итерации 5-6

## Рекомендуемый порядок реализации

1. Нормализовать object envelopes в существующих API- и service-ответах.
2. Ввести projection services и builders для neighborhood и task packet.
3. Построить `overview`, `inventory` и unified inspector поверх этих projections.
4. Добавить layered topology и impact views.
5. Добавить runtime traces, incidents и resource attribution.
6. Добавить profile overlays, ownership и review/change workflows.

## Практический первый backlog

Если начинать реализацию прямо сейчас, первый backlog стоит держать узким:

- определить `CanonicalObject` и `ActionDescriptor`
- собрать projection adapters для `root`, `hub`, `member`, `skill` и `scenario`
- отдать один `overview` endpoint, собранный из текущих health и runtime signals
- отдать один `object inspector` endpoint с object, incidents, recent events и actions
- добавить один `task_packet` endpoint для выбранного объекта и цели задачи

Этого уже достаточно, чтобы начать поставлять `Infrascope`, не дожидаясь полного графа и полной многопользовательской полировки.
