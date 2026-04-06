# Infrascope

`Infrascope` — это сценарий control plane в AdaOS для оценки, понимания и управления подсетью как живой распределенной системой.

Его нельзя проектировать как один экран или как статичную админ-панель. Инфраструктура AdaOS одновременно включает топологию узлов, размещение runtime, доступность устройств, выполнение сценариев, квоты, доверие и пользовательские рабочие пространства. Одна и та же система должна оставаться понятной человеку-оператору, машиночитаемой для LLM и управляемой в будущей многопользовательской среде.

Этот документ фиксирует целевую архитектуру. Поэтапная реализация описана в [Infrascope Roadmap](infrascope-roadmap.md).

## Цели

`Infrascope` должен позволять AdaOS быстро и последовательно отвечать на вопросы:

- что ломается прямо сейчас
- где именно находится проблема: root, hub, member, browser, device, runtime или policy
- что зависит от выбранного объекта
- где возникла нагрузка по ресурсам или квотам
- что изменилось незадолго до деградации
- что текущий пользователь, LLM или automation agent вообще могут увидеть и сделать дальше

## Принципы проектирования

1. Сначала `canonical system model`.
   UI, API, аудит и LLM context packets строятся как проекции одной и той же модели.
2. Одна система, несколько линз.
   `overview`, `topology`, `inventory`, `runtime`, `resources` и `incidents` — это согласованные режимы поверх общих объектов.
3. В центре объект, а не страница.
   Пользователь должен исследовать и изменять выбранный объект через единый инспектор, а не через набор разрозненных экранов.
4. `profile-aware` с самого начала.
   Видимость, терминология, дефолтные фильтры и доступные действия зависят от профиля, роли, workspace и policy.
5. Целевое и фактическое состояние живут вместе.
   `Infrascope` нужен не только для наблюдения, но и для безопасного планирования изменений.
6. LLM читает структурированные проекции, а не DOM и не сырые логи по умолчанию.
   Логи остаются доказательной базой, но не главным контрактом control plane.

## Участники

Одна и та же модель инфраструктуры должна обслуживать разных участников:

- `owner / super-admin`: полный инфраструктурный и governance-взгляд
- `infra operator`: здоровье, инциденты, связи, ресурсы и безопасные действия
- `developer`: runtime, деплой, версии, event trace и зависимости skills/scenarios
- `household user`: комнаты, устройства, автоматизации и бытовые алерты
- `LLM assistant`: task-shaped машинный контекст, формальные действия, ограничения и impact preview
- `automation agent`: узкий machine-to-machine control с явными разрешениями

Для них не нужно заводить разные объектные модели. Нужен один объект и разные overlays.

## Canonical System Model

Целевая модель — это `double digital twin`.

### Operational Twin

Описывает, что существует и что происходит:

- `root`
- `hub`
- `member`
- `browser_session`
- `device`
- `smart_home_endpoint`
- `skill`
- `scenario`
- `runtime`
- `connection`
- `route`
- `resource_quota`
- `event_stream`
- `incident`

### Governance Twin

Описывает владение, интерпретацию и допустимое управление:

- `identity`
- `profile`
- `workspace`
- `role`
- `policy`
- `team`
- `ownership`
- `review`
- `change_request`

Именно вместе они задают и инфраструктурную правду, и то, как она показывается, объясняется и управляется.

## Контракт объекта

Каждый объект, участвующий в `Infrascope`, должен поддерживать единый минимальный контракт.

```json
{
  "id": "hub:alpha",
  "kind": "hub",
  "title": "Hub Alpha",
  "summary": "Primary home subnet hub",
  "status": "degraded",
  "health": {
    "availability": 0.91,
    "connectivity": "unstable",
    "resource_pressure": "medium"
  },
  "relations": {
    "parent": "root:main",
    "members": ["member:m1", "member:m2"],
    "connected_browsers": ["browser:b7"],
    "devices": ["device:lamp-kitchen"]
  },
  "resources": {
    "cpu": 0.74,
    "ram": 0.81,
    "disk": 0.43
  },
  "runtime": {
    "skills_active": 12,
    "scenarios_active": 3,
    "failed_runs_24h": 5
  },
  "versioning": {
    "desired": "2026.03.23",
    "actual": "2026.03.20",
    "drift": true
  },
  "desired_state": {},
  "actual_state": {},
  "incidents": [],
  "actions": [],
  "governance": {
    "tenant_id": "tenant:main",
    "owner_id": "profile:ops-team",
    "visibility": ["role:admin"],
    "roles_allowed": ["role:infra-admin"],
    "shared_with": []
  },
  "representations": {
    "system": {},
    "operator": {},
    "user": {},
    "llm": {}
  },
  "audit": {
    "created_by": "system:bootstrap",
    "updated_by": "profile:ops-team",
    "last_seen": "2026-04-05T10:00:00Z",
    "last_changed": "2026-04-05T09:57:00Z"
  }
}
```

### Обязательный словарь статусов

Минимально контракт должен нормализовать:

- `online | offline | degraded | warning | unknown`
- `reachable | unreachable`
- `authenticated | untrusted | expired`
- `normal | overloaded | throttled`
- `synced | outdated | drifted`
- `installed | active | broken | pending_update`

## Projection Layer

Канонический объект не должен в сыром виде уходить каждому потребителю. Для `Infrascope` нужны стабильные проекции.

### Object Projection

Стандартизованное типизированное представление одного объекта.

Используется для:

- строк inventory
- карточек инспектора
- API-ответов
- local-object context для LLM

### Topology Projection

Срез графа по выбранным узлам и связям.

Используется для:

- карты подсети
- dependency и impact режимов
- анализа путей и мест отказа

### Narrative Projection

Короткое заранее подготовленное смысловое описание для оператора и LLM.

Типовые поля:

- `summary`
- `current_issue`
- `operator_focus`
- `risk_summary`

### Action Projection

Формальные, policy-aware описания действий.

Типовые поля:

- `id`
- `title`
- `requires_role`
- `risk`
- `affects`
- `preconditions`

### Task Packet

Task-shaped пакет контекста для LLM или automation:

- `local_object`
- `neighborhood`
- `task_goal`
- `policy_context`
- `allowed_actions`
- `relevant_incidents`
- `recent_changes`

Именно этот пакет должен быть основной LLM-точкой входа. Он позволяет не выгружать всю инфраструктуру целиком и не раскрывать лишние данные.

## Representation Layers

Один и тот же объект должен иметь несколько overlays поверх общего источника истины:

- `system representation`: точная runtime- и инфраструктурная форма
- `operational representation`: рабочее представление для operator и developer
- `user representation`: бытовая или упрощенная workspace-форма
- `llm representation`: сжатая, типизированная и удобная для reasoning форма

Пример:

- администратор видит `mTLS`, `version drift`, `routes` и `event pressure`
- household user видит "основной домашний узел", "12 устройств подключено" и "1 автоматизация работает с задержкой"
- LLM видит `relations`, `health vector`, `incidents`, `constraints` и `formal actions`

## Композиция UI

`Infrascope` должен вести себя как workspace оператора, а не как набор несвязанных страниц.

### Основные режимы

- `overview`: что происходит прямо сейчас
- `topology`: как связана подсеть
- `inventory`: какие объекты вообще есть и как их фильтровать
- `runtime`: какие сценарии и навыки работают прямо сейчас
- `resources`: где возникла нагрузка по квотам и ресурсам
- `events`: потоки, trace и подписки
- `incidents`: что сломано и что затронуто
- `policies / ownership / changes`: кто чем владеет и как управляются изменения

### Общий shell

- левая панель: навигация по workspace и saved views
- центральная область: карта, таблица, таймлайн или рабочее пространство детализации
- правая панель: единый object inspector

### Единый инспектор объекта

Инспектор должен быть консистентным для разных типов объектов и открывать вкладки вроде:

- `summary`
- `topology`
- `runtimes`
- `resources`
- `events / logs`
- `governance`
- `actions`

Задача здесь — исследовать объект без потери контекста и без постоянного ухода на новую страницу.

## Ключевые представления

### Overview

Стартовый экран должен сжимать операционный контекст, а не быть просто дашбордом ради графиков.

Рекомендуемые блоки:

- `health strip`: root, hubs, members, browsers, devices, event bus, LLM, Telegram
- `active incidents`
- `topology snapshot`
- `runtime pressure`
- `external services and quotas`
- `recently changed`

### Topology

Режим topology должен быть управляемым графом, а не одной огромной схемой.

Обязательные возможности:

- слои `physical`, `connectivity`, `runtime`, `resource`, `capability`
- локальное раскрытие графа и изоляция neighborhood
- подсветка degraded и failed путей
- impact preview перед потенциально разрушительными действиями
- overlay по version и sync drift

### Inventory and Control

Inventory нужно держать отдельно от live runtime, но строить на одном и том же контракте объекта.

Базовые вкладки:

- hubs
- members
- browsers
- devices
- skills
- scenarios
- runtimes
- quotas
- policies

### Runtime

Runtime нужен как отдельный режим, потому что AdaOS сильно завязан на orchestration.

Он должен объяснять:

- какие scenarios активны
- где они инстанцированы
- какие skills вызываются
- какие события находятся в полете
- где накапливаются retries, backlog и ошибки
- какие user, browser и device вовлечены

## Вопросы за пять секунд

`Infrascope` считается удачным, если он быстро отвечает на такие вопросы:

- Почему сейчас не работает сценарий?
- Это проблема root, hub, member, browser, device или policy?
- Устройство физически недоступно или логически заблокировано?
- Какой объект вызвал всплеск квоты или ресурсов?
- Какие навыки сейчас активны на этом hub?
- Что изменилось перед началом деградации?
- Что еще сломается, если я перезапущу hub или обновлю skill?
- Что текущему пользователю или LLM вообще разрешено сделать дальше?

## Точки опоры в текущем AdaOS

Новая архитектура должна наращиваться поверх существующих runtime-поверхностей, а не переписывать их с нуля.

- состояние узлов и подсети: `src/adaos/apps/api/node_api.py`, `src/adaos/apps/api/subnet_api.py`, `src/adaos/services/reliability.py`, `src/adaos/services/subnet/*`, `src/adaos/services/root/service.py`
- инвентарь runtime: `src/adaos/apps/api/skills.py`, `src/adaos/apps/api/scenarios.py`, `src/adaos/services/skill/*`, `src/adaos/services/scenario/*`
- наблюдаемость и события: `src/adaos/apps/api/observe_api.py`, `src/adaos/services/observe.py`, `src/adaos/services/eventbus.py`
- workspaces, Yjs и browser-facing overlays: `src/adaos/services/workspaces/*`, `src/adaos/services/yjs/*`, `src/adaos/services/scenario/webspace_runtime.py`
- профили и governance-основа: `src/adaos/services/user/profile.py`, `src/adaos/services/policy/*`

Именно из этих текущих producers должен собираться canonical model и набор projections.

## Антипаттерны

Стоит избегать следующего:

- один гигантский граф без фильтров и без локального neighborhood
- смешивание inventory-строк и live runtime-инстансов как будто это одно и то же
- одинаковое обращение с browser session, member и device
- скрытие зависимостей и impact radius
- ставка только на цвет без текстового статуса
- полный переход на отдельную страницу по каждому клику
- выдача LLM сырого инфраструктурного дампа вместо ограниченных task packets

## Целевой MVP

Первая действительно полезная версия `Infrascope` должна включать:

- `overview` со здоровьем системы, инцидентами, runtime pressure, квотами и recent changes
- `topology` для root, hubs, members, browsers и devices
- `inventory` tabs для основных классов объектов
- единый `object inspector`
- базовую `runtime` панель для активных scenarios и failed runs

Этот MVP намеренно операционный. Recommendation layer, anomaly detection и change proposals идут позже по дорожной карте.
