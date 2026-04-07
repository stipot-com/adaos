# Root MCP Foundation

`Root MCP Foundation` — это целевой архитектурный слой AdaOS, который должен быть заложен на `root` server до того, как проект попытается развернуть широкий MCP-контур.

Это не приватный shell bridge для Codex и не разовый endpoint для одной тестовой среды. Это первый вертикальный срез общей machine-readable и agent-operable foundation, которая позже должна поддерживать одновременно:

- `LLM-assisted development` skills и scenarios через SDK-oriented surfaces
- `LLM-assisted operations` через managed targets и governed operational capabilities

Этот документ фиксирует целевое расширение архитектуры и ранний путь реализации. Human-facing counterpart описан в [Infrascope](infrascope.md).

## Executive Summary

В AdaOS уже есть зачатки canonical system model, экспорт SDK metadata, runtime/status API, reliability snapshots и root-hub communication paths. Чего пока нет, так это единого root-hosted agent-facing слоя, который мог бы публиковать эти возможности в типизированной, governed и auditable форме.

`Root MCP Foundation` нужно закладывать уже сейчас, потому что и будущий LLM-assisted development, и будущий LLM-assisted operations зависят от одних и тех же недостающих элементов:

- стабильный self-description и contract exposure
- typed tool и action envelopes
- root-scoped policy decisions
- managed target descriptors
- audit и operational event capture

Этот слой должен жить именно на `root`, потому что `root` уже естественно соответствует ролям:

- `trust anchor`
- `policy point`
- `routing point`
- `audit aggregation point`
- `agent-facing capability exposure point`

Первым operational target должен стать `test hub`, но target-operations не стоит реализовывать как всегда открытый infra endpoint. Их нужно публиковать через установленный и разрешенный `infra_access_skill`, который открывает типизированную operational capability surface только пока он включен. Это лучше согласуется со skill model, lifecycle, policy model и будущим control plane AdaOS.

## Proposed Architecture Extension

В целевую архитектуру AdaOS нужно добавить новый root-hosted слой:

- `Root MCP Foundation`

Этот слой должен быть явно разделен на два подслоя:

- `MCP Development Surface`
- `MCP Operational Surface`

Это не два отдельных продукта. Это две проекции поверх одной foundation.

### Shared Foundation Components

Оба подслоя должны использовать одни и те же root-hosted building blocks:

- `self-description registry` для SDK, contracts, schemas, templates и supported surface classes
- `tool contract registry` для typed tool descriptors, аргументов, output envelopes и stability metadata
- `managed target registry` для target identity, environment, health, installed operational skills и published capabilities
- `policy decision point` для visibility, capability grants, execution constraints и environment guards
- `routing bridge`, который сопоставляет root-level tool calls с локальными сервисами или managed-target operational skills
- `audit trail` и `operational event model`
- `normalized response model` для tool responses, errors, redactions и связанных event IDs

## Placement in AdaOS Target Architecture

`Root MCP Foundation` должен стоять рядом с будущим web control plane, а не под ним и не как его внутренний implementation detail.

```text
human operator
  -> Web Control Plane / Infrascope
  -> canonical objects, projections, inspector, topology, incidents

LLM assistant / automation agent
  -> Root MCP Foundation
     -> MCP Development Surface
     -> MCP Operational Surface

Root MCP Foundation
  -> root policy point
  -> root audit aggregation
  -> root routing and target mediation
  -> canonical descriptors and typed tool contracts

managed targets
  -> hub / member / browser-related surfaces
  -> first target: test hub
  -> operational access published by installed skills such as infra_access_skill
```

### Relationship to Major AdaOS Elements

- `root`: хостит foundation, применяет policy, записывает audit и маршрутизирует requests
- `hub/member`: остаются runtime и managed-node actors, но не становятся основным MCP control point
- `test hub`: первый `managed target` для operational pilot
- `skills/scenarios`: остаются главными программными сущностями AdaOS; MCP development должен описывать и scaffold'ить их, а не обходить
- `SDK`: становится first-class development contract surface, которую MCP публикует в machine-readable форме
- `web control plane`: остается основным human-facing operational surface
- `MCP`: становится основным agent-facing typed surface

## Root MCP Foundation Model

Foundation должна проектироваться вокруг четырех общих моделей.

### 1. Self-Description Model

Описывает, что AdaOS готов публиковать агентам как стабильный development или operations contract:

- SDK modules и exported tools/events
- manifest и schema registries
- canonical object vocabulary
- supported capability classes
- template catalog metadata
- stability и version metadata

### 2. Tool Contract Model

Каждый MCP-exposed tool должен публиковать:

- tool id
- purpose и summary
- input schema
- output schema
- side-effect class
- required capability
- allowed environments
- timeout и concurrency hints
- redaction rules

### 3. Managed Target Model

Каждый operationally reachable target должен публиковать типизированный descriptor, например:

```json
{
  "target_id": "hub:test-alpha",
  "kind": "hub",
  "environment": "test",
  "status": "online",
  "transport": {
    "channel": "hub_root_protocol"
  },
  "operational_surface": {
    "published_by": "skill:infra_access_skill",
    "enabled": true,
    "capabilities": [
      "hub.get_status",
      "hub.get_runtime_summary",
      "hub.run_healthchecks"
    ]
  },
  "policy": {
    "write_scope": "test-only"
  }
}
```

Ключевая мысль: target operationally available не потому, что у него навсегда открыт infrastructure endpoint, а потому, что он зарегистрирован и публикует enabled operational surface.

### 4. Operational Event Model

Каждая значимая MCP-handled operation должна порождать нормализованное event-представление с полями вроде:

- `event_id`
- `trace_id`
- `request_id`
- `surface`
- `actor`
- `target_id`
- `tool_id`
- `capability`
- `policy_decision`
- `execution_adapter`
- `dry_run`
- `status`
- `started_at`
- `finished_at`
- `result_summary`
- `error`
- `redactions`

Эта event-модель должна использоваться одновременно для:

- MCP responses
- audit trail
- web UI history
- diagnostics и incident timelines
- последующей analytics и оценки эффективности workflows

## MCP-to-SDK Foundation

Первая development-facing задача `Root MCP Foundation` — публиковать curated, typed, machine-readable view development surfaces AdaOS.

### Minimal Root-Hosted Development Descriptors

На первом этапе foundation должна публиковать descriptors для:

- SDK exported tools и events из `adaos.sdk.core.exporter`
- canonical control-plane vocabulary из `adaos.services.system_model.*`
- skill manifest schema и runtime-related manifest metadata
- scenario manifest и projection metadata
- capability class registry и permission hints
- templates и scaffold metadata, включая names, intended use и required files
- supported projection classes, например object, neighborhood, task packet, inventory и reliability views

### Self-Description Layer Requirements

Root-hosted self-description layer должен отвечать на вопросы вроде:

- какие SDK surfaces стабильны, experimental или internal
- какой contract должны удовлетворять skill или scenario
- какие inputs и outputs доступны для tools и events
- какие object kinds и relation kinds существуют в canonical system model
- какие capability classes и action classes поддерживаются
- какие templates существуют для новых skills и scenarios

### What the Development Surface Should Not Be

Development surface не должна по умолчанию превращаться в:

- произвольный filesystem browsing по всему repo
- произвольный import и execution project modules
- raw codebase dumping как главный контракт
- прямой доступ к secrets или environment files

Вместо этого surface должна публиковать curated descriptors, schemas, manifests, registries, template metadata и selected examples. Доступ к сырому коду может существовать для разработчиков отдельно, но не должен быть главным MCP abstraction.

### Evolution Path

Development-facing путь должен выглядеть так:

1. публикуем machine-readable SDK и contract descriptors
2. публикуем skill/scenario template metadata и supported capability classes
3. публикуем task-shaped development packets для authoring или refactoring tasks
4. позже добавляем draft/proposal и review-oriented flows, когда контракты стабилизируются

## Managed Target and `infra_access_skill` Model

### Managed Target Model

`managed target` — это среда, которую `root` может идентифицировать, оценивать и governance-ить для operational workflows. Такой target должен включать:

- identity и environment classification
- health и reachability state
- transport/routing metadata
- ownership и policy scope
- installed operational skills
- published capabilities
- recent incidents и audit history

Первым managed target должен стать `test hub`.

### Why the First Target Should Be a Test Hub

`test hub` подходит как первый target, потому что позволяет проверить:

- target registration и policy gating
- root-to-target routing semantics
- typed tool contracts
- bounded execution adapters
- operational observability
- approval и rollback patterns

не раскрывая production environments слишком рано.

### `infra_access_skill`

`infra_access_skill` должен стать первым `skill-mediated infrastructure surface`.

Его задача — не открыть unrestricted admin shell. Его задача — публиковать узкую, typed, policy-aware operational capability surface, когда одновременно выполняются условия:

- skill установлена на target
- skill включена для target
- root policy разрешает запрошенную capability

Это дает AdaOS явный lifecycle control через:

- install
- enable
- disable
- update
- audit
- per-target policy gating

### Execution Adapters

`infra_access_skill` должен делегировать реальную работу allowlisted `execution adapters`, например:

- runtime summary adapter
- healthcheck adapter
- logs adapter
- deploy-ref adapter
- service restart adapter
- allowed-tests adapter
- test-results adapter
- rollback adapter

Именно adapters должны быть границей, где обеспечиваются bounded execution, timeout handling, redaction, dry-run behavior и environment guards.

## WebUI and Observability Model

`infra_access_skill` нужно с самого начала рассматривать как observable operational skill.

### Built-In WebUI

Навык должен иметь web-facing operational view, который позже можно встроить в `Infrascope`, минимум с такими разделами:

- `overview`
- `requests log`
- `failures and errors`
- `capability usage`
- `policy and profile state`
- `target summary`

### What Should Be Logged

Минимально каждый обработанный request должен записывать:

- incoming request envelope
- выбранный tool и capability
- policy decision
- execution adapter choice
- dry-run flag
- execution start и finish
- normalized result
- error и retry information
- redaction summary

### Why the WebUI Matters Early

Этот web surface — не факультативная полировка. Он нужен как:

- `observability layer` для поведения operational skill
- `explainability layer` для policy и routing decisions
- `effectiveness evaluation layer` для MCP-driven workflows
- первая точка сходимости human-facing и agent-facing control surfaces

`Infrascope` со временем должен рассматривать `infra_access_skill` как first-class operational object с inspector, incidents, action history и capability-usage panels.

## Capability Model

Capability model должна быть явно разделена по surface.

### Development-Facing Capabilities

Первые capability classes должны покрывать read-oriented development context:

- `sdk.read.metadata`
- `sdk.read.schemas`
- `sdk.read.skill_contracts`
- `sdk.read.scenario_contracts`
- `sdk.read.templates`
- `sdk.read.capability_classes`
- `sdk.read.system_model`

Эти capabilities должны давать доступ к structured descriptors, а не к произвольному выполнению repository code.

### Operational Capabilities

Operational capabilities на первом этапе должны публиковаться через `infra_access_skill` в виде typed operational tools, например:

- `hub.get_status`
- `hub.get_runtime_summary`
- `hub.get_logs`
- `hub.run_healthchecks`
- `hub.deploy_ref`
- `hub.restart_service`
- `hub.run_allowed_tests`
- `hub.get_test_results`
- `hub.rollback_last_test_deploy`

Ранние этапы должны начинаться с read-only и low-risk diagnostics, а затем расширяться до controlled writes только на test targets.

### Explicit Non-Goals and Disallowed Operations

На раннем этапе `Root MCP Foundation` не должна разрешать:

- arbitrary shell execution
- arbitrary filesystem access
- reading secrets как generic capability
- unrestricted `docker`, `systemctl`, `git` или package-manager access
- broad production-target operations

## Operation Routing Model

Operational routing flow должен выглядеть так:

1. agent вызывает root MCP tool
2. `root` находит tool contract, валидирует capability и environment policy, создает audit/request envelope
3. `root` разрешает `managed target` descriptor и выбирает опубликованный operational surface
4. request маршрутизируется через уже существующее семейство control channels, а не через новый параллельный transport с первого дня
5. на target установленный `infra_access_skill` принимает request и выбирает allowlisted `execution adapter`
6. adapter выполняет bounded work и возвращает normalized result
7. `root` записывает operational event и возвращает normalized MCP response, связанный с audit trace

Этот путь должен быть эволюционным: первая реализация должна по максимуму переиспользовать текущие root-hub transports и node control paths, просто оборачивая их typed contracts и policy checks.

## Safety, Governance, and Audit

Безопасность здесь — часть архитектуры, а не более поздний hardening task.

### Governing Principles

- `root` — это policy point
- ранний operational scope — `test-only`
- operational access делается skill-mediated, а не always-exposed
- capabilities allowlisted и environment-scoped
- execution bounded через timeouts, concurrency limits и adapter contracts
- secrets редактируются по умолчанию и не возвращаются как general-purpose payloads
- все write operations должны быть привязаны к traceable actor и request

### Required Controls

- capability grant checks
- target-environment gating
- per-tool timeout и retry policy
- concurrency limits per target и per actor
- redaction secrets и sensitive file paths
- четкие failure containment boundaries
- rollback affordances для state-changing test operations
- persistent audit trail с event IDs и trace IDs

### Longer-Term Alignment

Долгосрочная цель — выровнять эту модель с более общей permission и capability architecture AdaOS, чтобы SDK, web UI, MCP и automation использовали один и тот же vocabulary и policy evaluation model.

## Roadmap Extension

`Root MCP Foundation` должна развиваться как companion roadmap track.

### Phase 0. Architectural Fixation

- фиксируем терминологию и boundaries
- фиксируем root-first MCP model
- фиксируем split между development и operational surfaces
- фиксируем managed target model
- фиксируем концепт `infra_access_skill`
- фиксируем operational event и observability model

### Phase 1. Root MCP Foundation Skeleton

- добавляем minimal root MCP entrypoint и request/response envelopes
- добавляем root-hosted tool contract registry
- добавляем initial audit primitives и event IDs
- публикуем только минимальные read-oriented descriptors и placeholder operational contracts

### Phase 2. MCP-to-SDK Base

- публикуем machine-readable SDK descriptors
- публикуем skill/scenario contracts и schemas
- публикуем canonical vocabulary и projection-class registries
- строим первый ограниченный LLM-facing development surface

### Phase 3. Test-Hub Operational Pilot

- регистрируем `test hub` как первый managed target
- реализуем `infra_access_skill`
- начинаем с read-only diagnostics и low-risk checks
- controlled write operations добавляем только после того, как доказаны bounded execution и audit
- добавляем web UI и operational logging для навыка

### Phase 4. Controlled Development + Operations Convergence

- добавляем task-shaped development packets для authoring skills/scenarios
- улучшаем structured diagnostics и target summaries
- сближаем web и MCP surfaces вокруг shared objects, actions и event history
- используем foundation в iterative Codex и LLM-assisted workflows

### Phase 5. Broader Operational Architecture

- поддерживаем несколько managed targets
- расширяем capability classes и policy overlays
- расширяем catalog operational skills beyond `infra_access_skill`
- выравниваем root MCP, `Infrascope` и future approval/change workflows

## Gap Analysis Against Current AdaOS State

### Already Aligned

- `root/hub communication paths`: `src/adaos/services/bootstrap.py`, `src/adaos/services/root/service.py`, `src/adaos/services/root/client.py`, `src/adaos/services/subnet/*`
- `machine-readable control-plane foundations`: `src/adaos/services/system_model/*`, `src/adaos/sdk/control_plane.py`, `src/adaos/sdk/data/control_plane.py`
- `SDK self-description fragments`: `src/adaos/sdk/core/exporter.py`, `src/adaos/sdk/core/decorators.py`
- `skill contract fragments`: `src/adaos/services/skill/skill_schema.json`
- `observability building blocks`: `src/adaos/services/observe.py`, `src/adaos/services/eventbus.py`, `src/adaos/services/reliability.py`
- `workspace and browser-facing surfaces`: `src/adaos/services/workspaces/*`, `src/adaos/services/scenario/webspace_runtime.py`, `src/adaos/services/yjs/*`

### Partially Aligned

- `health/status/control APIs`: полезные node и subnet APIs уже есть, но они по-прежнему в основном HTTP-centric и не нормализованы как root-routed typed operational tools
- `SDK metadata exposure`: exporter уже есть, но он еще не curated и не опубликован как root MCP development surface
- `scenario/skill descriptors`: manifests, registries и projection services существуют, но пока не собраны в единый root-level self-description catalog
- `capability model`: `src/adaos/services/policy/capabilities.py` и SDK capability errors уже есть, но нет unified capability-class registry и MCP policy layer
- `web UI declarative assets`: Yjs и workspace surfaces уже есть, но dedicated operational-skill web UI и audit-first operator view пока нет

### Missing

- фактический runtime `Root MCP Foundation` на `root`
- `MCP Development Surface` и `MCP Operational Surface` как явные продукты
- managed target registry с publication state operational surfaces
- `infra_access_skill`
- typed operational tool catalog на root
- unified operational event model, охватывающая request, policy, execution, result и error
- operational audit trail и workflow effectiveness views
- target-level web UI для operational skills

### Should Be Refactored Before Extension

- широкие node endpoints вроде `infrastate/action` со временем должны уступить место typed tool contracts и execution adapters
- schema и descriptor publication нужно централизовать, чтобы MCP не скрейпил ad hoc runtime outputs
- policy и governance metadata должны двигаться к общему root-evaluated vocabulary для SDK, API и MCP
- operational writes нужно маршрутизировать через bounded adapters, а не через generic system primitives

## Recommended Edits to Existing Architecture Notes / Roadmap Documents

Это предложение должно отражаться в текущем наборе docs так:

- [Infrascope](infrascope.md): добавить явный раздел о связи human-facing control plane и `Root MCP Foundation`
- [Infrascope Roadmap](infrascope-roadmap.md): добавить `Root MCP Foundation` как companion track и отметить точки фазового выравнивания
- [Architecture Overview](index.md): уточнить, что target-state control-plane evolution описана и в `Infrascope`, и в `Root MCP Foundation`
- `sdk_control_plane.md` и `cli/api.md`: обновлять позже, когда появятся первый реальный root MCP skeleton и опубликованные contracts

## Recommended Terminology

- `Root MCP Foundation`: root-hosted machine-readable и agent-operable foundation для будущего MCP support
- `MCP Development Surface`: development-facing MCP layer, публикующий SDK, contracts, schemas и supported authoring surfaces
- `MCP Operational Surface`: operational MCP layer, публикующий typed и governed target operations
- `managed target`: target environment, которую root может идентифицировать, governance-ить и маршрутизировать к ней operational requests
- `test hub`: первый managed target для operational pilot
- `infra_access_skill`: установленный skill, который публикует operational capability surface target
- `operational capability surface`: набор typed target operations, которые skill сейчас имеет право публиковать
- `typed operational tools`: явные tool contracts вроде `hub.get_status` или `hub.run_healthchecks`
- `execution adapter`: bounded target-side adapter, выполняющий реальную работу за typed tool
- `policy point`: root-hosted evaluation point, где решается, можно ли использовать capability
- `audit trail`: постоянная история requests, policy decisions, execution и outcomes
- `bounded execution`: execution, ограниченный environment scope, adapter type, timeouts, concurrency и redaction rules
- `operational event model`: общее event envelope для MCP, audit, web UI history, diagnostics и analytics
