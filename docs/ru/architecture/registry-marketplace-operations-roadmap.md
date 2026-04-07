# Registry, Marketplace и Operations Roadmap

Эта заметка фиксирует эволюционную архитектуру для трех связанных направлений:

- синхронизация `registry.json` при `skill push` и `scenario push`
- marketplace UI в `InfrastateSkill`
- единая модель long-running operations с проекцией в Yjs

Ключевое правило:

> Yjs используется как live projection layer для клиента, но не как execution transport и не как source of truth для orchestration.

## Текущий срез

В коде уже есть важные опорные элементы:

- локальный deterministic helper для `registry.json` в [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py)
- publish/update flow в [service.py](/d:/git/adaos/src/adaos/services/root/service.py)
- hub-only snapshot/action surface для Infra State в [node_api.py](/d:/git/adaos/src/adaos/apps/api/node_api.py)
- текущая композиция `InfrastateSkill` в [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)
- client modal/action/notification plumbing в [page-modal.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts), [page-action.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-action.service.ts) и [notification-log.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/notification-log.service.ts)

При этом сейчас:

- `workspace_registry` уже умеет `upsert`, но это локальный helper, а не shared registry sync в `adaos-registry`
- install endpoints в [skills.py](/d:/git/adaos/src/adaos/apps/api/skills.py) и [scenarios.py](/d:/git/adaos/src/adaos/apps/api/scenarios.py) по сути синхронные
- reusable operation layer пока нет
- canonical Yjs subtree для operations и notifications пока нет
- marketplace пока не оформлен как отдельный adapter/service поверх registry catalog

## Целевая архитектура

## 1. Registry sync

`adaos-registry/registry.json` должен оставаться published catalog snapshot.

Он не должен становиться:

- runtime state store
- очередью операций
- клиентским session store

Целевая модель:

1. `push` читает manifest
2. нормализует его в shared catalog entry
3. делает upsert по стабильной identity
4. записывает deterministic `registry.json`

Минимальная shape entry:

- `kind`
- `id`
- `name`
- `version`
- `description`
- `tags`
- `source`
- `publisher`
- `updated_at`
- optional install metadata

Опора на текущий код:

- [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py)
- [service.py](/d:/git/adaos/src/adaos/services/root/service.py)
- [dev.py](/d:/git/adaos/src/adaos/apps/cli/commands/dev.py)

## 2. Marketplace в InfrastateSkill

Marketplace должен читаться не напрямую из raw `registry.json`, а через маленький adapter в UI-facing model.

Целевой flow:

1. hub читает registry catalog
2. adapter превращает raw entries в rows для UI
3. installed skills/scenarios фильтруются через локальные registries
4. `InfrastateSkill` добавляет кнопку `Marketplace` рядом с `Update skills & scenarios`
5. клиент открывает modal с секциями skills/scenarios
6. row action запускает install command

Опора на текущий код:

- `_skills_items()` и `_scenario_items()` в [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)
- modal plumbing в [page-modal.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts)

## 3. Long-running operations

Install/update flow должен быть таким:

1. client отправляет command
2. hub быстро принимает request
3. hub создает `operation_id`
4. execution идет асинхронно
5. runtime обновляет canonical operation state
6. projection service зеркалит состояние в Yjs
7. UI реагирует на Yjs-проекцию

Минимальная модель `OperationState`:

- `operation_id`
- `kind`
- `target_kind`
- `target_id`
- `status`
- `progress`
- `message`
- `current_step`
- `started_at`
- `updated_at`
- `finished_at`
- `result`
- `error`
- `initiator`
- `scope`
- `can_cancel`

Рекомендуемые имена:

- `OperationManager`
- `OperationState`
- `OperationProjectionService`
- `OperationNotification`

## 4. Yjs projection

Yjs должен не оркестрировать install, а только проецировать его состояние.

Целевой subtree:

- `runtime.operations.by_id`
- `runtime.operations.order`
- `runtime.operations.active`
- `runtime.notifications`

Это даст клиенту возможность:

- disable install button для того же target
- показать progress и current step
- отобразить active operations
- показать success/error notification
- восстановиться после reconnect через YStore snapshot

## 5. Notifications

Нужно разделить:

- `operation`: live/historical process state
- `notification`: user-facing result event

Для плавной миграции разумно:

- ввести `runtime.notifications`
- временно продолжать mirror completion/failure в существующий `data/desktop/toasts`

## Пошаговый roadmap

## Phase 0: Contracts

- зафиксировать shared catalog entry model
- зафиксировать `OperationState`
- зафиксировать Yjs projection shape
- явно зафиксировать rule: Yjs = projection only

## Phase 1: Registry sync

- общий upsert path для skills/scenarios
- привязка `push_skill` и `push_scenario` к обновлению `adaos-registry/registry.json`
- тесты на create/update и deterministic ordering

## Phase 2: Marketplace read path

- `Marketplace` action в `InfrastateSkill`
- catalog adapter/service
- modal UI
- filtering installed artifacts

## Phase 3: Async install operations

- новый `src/adaos/services/operations/*`
- async install handlers
- быстрый accepted response с `operation_id`
- projection в Yjs

## Phase 4: UI binding and notifications

- disabled buttons по scope/target
- progress/current step binding
- active operations section
- visible success/error notifications

## На какие файлы логично опираться при реализации

- `src/adaos/services/workspace_registry.py`
- `src/adaos/services/root/service.py`
- `src/adaos/apps/api/skills.py`
- `src/adaos/apps/api/scenarios.py`
- `src/adaos/apps/api/node_api.py`
- `.adaos/workspace/skills/infrastate_skill/handlers/main.py`
- `src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts`
- `src/adaos/integrations/adaos-client/src/app/runtime/page-action.service.ts`
- `src/adaos/integrations/adaos-client/src/app/runtime/notification-log.service.ts`
- новый пакет `src/adaos/services/operations/*`

## Совместимость и риски

- сохранить backward compatibility для top-level `skills` / `scenarios` в `registry.json`
- не дублировать source of truth между runtime storage и Yjs
- не завязывать marketplace UI напрямую на raw registry schema
- не вводить слишком узкие имена вроде `InstallStack`, чтобы тот же operation layer потом подошел для update, diagnostics, migration и model download

Итоговое решение должно дорастить уже существующие строительные блоки, а не обходить их:

- deterministic registry helper
- hub-side infrastate snapshot/action surface
- client modal and notification plumbing
- Yjs persistence and reconnect behavior

Главное добавление поверх текущего состояния одно:

- reusable operation runtime layer с проекцией в Yjs только для client visibility

