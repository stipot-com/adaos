# Registry, Marketplace, and Operations Roadmap

This note fixes the target architecture for three related tracks:

- registry synchronization for published skills and scenarios
- marketplace UX in `InfrastateSkill`
- reusable long-running operation projection for hub-client interaction

It is intentionally evolutionary and tied to the AdaOS codebase as it exists today.

The governing rule is:

> Yjs is a live projection layer for clients, not the execution transport and not the source of truth for orchestration.

## Why This Note Exists

The current codebase already has most of the raw building blocks, but they are split across different layers:

- local workspace catalog snapshot helpers in [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py)
- draft and publish flows in [service.py](/d:/git/adaos/src/adaos/services/root/service.py)
- synchronous install endpoints in [skills.py](/d:/git/adaos/src/adaos/apps/api/skills.py) and [scenarios.py](/d:/git/adaos/src/adaos/apps/api/scenarios.py)
- hub-only infrastructure snapshot and action endpoints in [node_api.py](/d:/git/adaos/src/adaos/apps/api/node_api.py)
- `InfrastateSkill` snapshot/action composition in [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)
- client modal, action, and notification plumbing in [page-modal.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts), [page-action.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-action.service.ts), and [notification-log.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/notification-log.service.ts)

What is missing is one coherent contract that connects them.

## Current Implementation Slice

## What Already Exists

### Registry and publish path

- AdaOS already has a deterministic `registry.json` helper for a workspace in [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py).
- The current shape is machine-readable and normalized as:
  - top-level `version`, `updated_at`, `skills`, `scenarios`
  - per-entry fields such as `kind`, `name`, `version`, `updated_at`, `path`, `manifest`, plus kind-specific metadata
- `upsert_workspace_registry_entry(...)` already gives us the right local abstraction for create-or-update semantics.
- Root publish flow already reuses that helper after publish-to-workspace in [service.py](/d:/git/adaos/src/adaos/services/root/service.py#L2370).

### Marketplace-adjacent UI

- `InfrastateSkill` already builds infrastructure-facing tables and actions from a snapshot model in [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py).
- It already exposes `Update skills & scenarios` as an action row via `_update_actions(...)`.
- The client already supports:
  - static modal routing in [page-modal.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts)
  - schema-backed transient modals
  - host-action dispatch with HTTP fallback in [page-action.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-action.service.ts)
  - local notification history plus Yjs-fed toasts in [notification-log.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/notification-log.service.ts)

### Runtime and Yjs projection

- AdaOS already uses Yjs as the browser-visible runtime state and persists it through YStore snapshots.
- `node_api` already exposes a hub-only `infrastate/snapshot` and `infrastate/action` surface.
- `InfrastateSkill` already uses `skill_memory_*` for local UI state and supports background refresh, which is a useful precedent for non-blocking UI updates.
- Notifications already have a separation between transient UI display and persisted history, and Yjs already carries `data/desktop/toasts`.

## What Does Not Exist Yet

- `skill push` and `scenario push` do not update the shared `adaos-registry` catalog snapshot directly.
- install/update API calls are still mostly synchronous request/response flows
- there is no reusable `OperationManager`-like runtime abstraction for accepted/queued/running/completed work
- there is no canonical Yjs subtree for live operations and recent notifications
- marketplace content is not yet modeled as a client-facing catalog adapter separate from raw `registry.json`

## Target Architecture

## 1. Registry Sync

`adaos-registry/registry.json` should be treated as a published catalog snapshot.

It should not become:

- a runtime state store
- an execution queue
- a per-client session artifact

The target contract is a single shared upsert path for both skills and scenarios:

1. read artifact manifest
2. normalize into one catalog entry model
3. upsert by stable identity
4. write sorted deterministic `registry.json`

### Target entry shape

The target entry should preserve current useful fields and converge on a richer stable model:

```json
{
  "kind": "skill",
  "id": "infrastate_skill",
  "name": "Infra State",
  "version": "1.4.0",
  "description": "Infrastructure and runtime control surface",
  "tags": ["infra", "ops"],
  "source": {
    "registry": "adaos-registry",
    "path": "skills/infrastate_skill",
    "manifest": "skills/infrastate_skill/skill.yaml"
  },
  "publisher": {
    "owner_id": "owner-123",
    "node_id": "hub-1"
  },
  "install": {
    "kind": "skill",
    "name": "infrastate_skill"
  },
  "updated_at": "2026-04-07T12:00:00+00:00"
}
```

Rules:

- reuse canonical manifest fields where they already exist
- keep deterministic ordering by `kind` then stable id/name
- preserve backward compatibility with current top-level `skills` and `scenarios` arrays
- introduce shared normalization code instead of separate skill/scenario serializers

### Current anchors

- local registry helper: [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py)
- push entrypoints: [dev.py](/d:/git/adaos/src/adaos/apps/cli/commands/dev.py)
- root push orchestration: [service.py](/d:/git/adaos/src/adaos/services/root/service.py)

### Recommended implementation shape

- extract a typed `registry catalog entry` normalizer from [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py)
- reuse it both for local workspace registry and remote `adaos-registry` updates
- make `push_skill` and `push_scenario` call one shared `upsert_registry_catalog_entry(kind, source_dir, target_repo)` path after artifact upload succeeds

## 2. Marketplace in InfrastateSkill

The marketplace should be modeled as a thin adapter over the registry catalog, not as a direct UI binding to raw `registry.json`.

### Target flow

1. hub loads registry catalog from `adaos-registry`
2. adapter maps raw entries into UI-facing rows
3. installed skills/scenarios are filtered out using local registries
4. `InfrastateSkill` exposes a `Marketplace` action next to `Update skills & scenarios`
5. client opens a modal with two tabs or two sections: skills and scenarios
6. row action dispatches install command

### UI model

The UI-facing model should be small and explicit:

```json
{
  "kind": "skill",
  "id": "infrastate_skill",
  "title": "Infra State",
  "version": "1.4.0",
  "description": "Infrastructure and runtime control surface",
  "tags": ["infra", "ops"],
  "publisher": "owner-123",
  "installed": false,
  "install_action": {
    "target": "infrastate.action",
    "id": "marketplace_install",
    "value": {
      "target_kind": "skill",
      "target_id": "infrastate_skill"
    }
  }
}
```

### Current anchors

- installed skills list: `_skills_items()` in [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)
- installed scenarios list: `_scenario_items()` in [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)
- modal infrastructure: [page-modal.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts)

### Recommended implementation shape

- add a small marketplace catalog service on the hub side
- have `InfrastateSkill` consume a UI-ready marketplace snapshot instead of parsing registry payload inline
- keep filtering logic as a pure function over:
  - catalog entries
  - installed skill ids
  - installed scenario ids

## 3. Long-Running Operations

The install/update architecture should follow this lifecycle:

1. client sends command
2. hub validates and accepts quickly
3. hub creates operation record
4. runtime executes work asynchronously
5. runtime updates canonical operation state
6. projection service mirrors that state into Yjs
7. UI reacts to Yjs only for visibility and affordances

The canonical execution state must stay in runtime memory and/or durable runtime storage, not in Yjs.

### Operation model

Core naming should stay reusable:

- `OperationManager`
- `OperationState`
- `OperationProjectionService`
- `OperationNotification`

Minimum operation fields:

```json
{
  "operation_id": "op_20260407_001",
  "kind": "skill.install",
  "target_kind": "skill",
  "target_id": "infrastate_skill",
  "status": "running",
  "progress": 40,
  "message": "Preparing runtime",
  "current_step": "runtime.prepare",
  "started_at": "2026-04-07T12:00:00+00:00",
  "updated_at": "2026-04-07T12:00:12+00:00",
  "finished_at": null,
  "result": null,
  "error": null,
  "initiator": {
    "kind": "user",
    "id": "local"
  },
  "scope": [
    "global",
    "skill.install",
    "skill:infrastate_skill"
  ],
  "can_cancel": false
}
```

### Current anchors

- synchronous install endpoints: [skills.py](/d:/git/adaos/src/adaos/apps/api/skills.py) and [scenarios.py](/d:/git/adaos/src/adaos/apps/api/scenarios.py)
- existing `infrastate.action` acceptance path: [node_api.py](/d:/git/adaos/src/adaos/apps/api/node_api.py)
- current non-blocking precedent: background refresh logic in [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)

### Recommended implementation shape

- add `src/adaos/services/operations/` with:
  - `model.py`
  - `manager.py`
  - `projection.py`
  - `notifications.py`
- keep API request handling thin:
  - create operation
  - schedule worker task
  - return `202 accepted` style payload with `operation_id`

## 4. Yjs Projection Shape

Yjs should expose a client-facing projection subtree with enough information for:

- button disabling
- progress display
- active operations list
- reconnect recovery
- recent completion notifications

### Target shape

```json
{
  "runtime": {
    "operations": {
      "by_id": {
        "op_20260407_001": {
          "operation_id": "op_20260407_001",
          "kind": "skill.install",
          "target_kind": "skill",
          "target_id": "infrastate_skill",
          "status": "running",
          "progress": 40,
          "message": "Preparing runtime",
          "current_step": "runtime.prepare",
          "scope": ["global", "skill.install", "skill:infrastate_skill"],
          "started_at": "...",
          "updated_at": "...",
          "finished_at": null
        }
      },
      "order": ["op_20260407_001"],
      "active": ["op_20260407_001"]
    },
    "notifications": [
      {
        "id": "notif_1",
        "level": "success",
        "message": "Skill infrastate_skill installed",
        "operation_id": "op_20260407_001",
        "target_kind": "skill",
        "target_id": "infrastate_skill",
        "ts": "..."
      }
    ]
  }
}
```

### Retention policy

- active operations stay projected while active
- finished operations remain in a bounded recent-history window
- notifications remain separate from operation state
- YStore snapshot persistence is enough for reconnect/reload recovery

### Current anchors

- Yjs persistence and room lifecycle: `src/adaos/services/yjs/*`
- current client-side Yjs toast ingestion: [notification-log.service.ts](/d:/git/adaos/src/adaos/integrations/adaos-client/src/app/runtime/notification-log.service.ts)

## 5. Notifications

Operation projection and notifications should stay separate.

Operation state answers:

- what is running
- what step is active
- whether a button must be disabled

Notification state answers:

- what should be shown to the user after completion or failure

For the current client, the simplest compatible path is:

- project operation notifications into `runtime.notifications`
- mirror completion/failure into existing `data/desktop/toasts` until the client is fully switched to the new subtree

That gives backward compatibility with current toast behavior while establishing the new contract.

## Phased Roadmap

## Phase 0: Fix Contracts

### Goal

Define stable contracts before wiring UI and background workers.

### Deliverables

- shared catalog entry model for registry sync
- shared operation state model
- Yjs projection schema for `runtime.operations` and `runtime.notifications`
- explicit rule that Yjs is projection-only

### Current anchors

- [workspace_registry.py](/d:/git/adaos/src/adaos/services/workspace_registry.py)
- [node_api.py](/d:/git/adaos/src/adaos/apps/api/node_api.py)
- [main.py](/d:/git/adaos/.adaos/workspace/skills/infrastate_skill/handlers/main.py)

## Phase 1: Registry Sync

### Goal

Make `skill push` and `scenario push` update `adaos-registry/registry.json`.

### Deliverables

- shared upsert helper reused by both artifact kinds
- tests for create/update behavior
- deterministic output ordering

### Current anchors

- [service.py](/d:/git/adaos/src/adaos/services/root/service.py)
- [dev.py](/d:/git/adaos/src/adaos/apps/cli/commands/dev.py)

## Phase 2: Marketplace Read Path

### Goal

Expose a marketplace catalog in `InfrastateSkill`.

### Deliverables

- `Marketplace` action next to `Update skills & scenarios`
- catalog adapter service
- modal view with skills/scenarios sections
- filtering against installed artifacts

## Phase 3: Async Install Operations

### Goal

Convert install/update flows from blocking request/response into accepted async operations.

### Deliverables

- `OperationManager`
- async install command handlers
- `operation_id` response contract
- operation projection into Yjs

## Phase 4: UI Binding and Notifications

### Goal

Make the client react to projected operations instead of waiting on request completion.

### Deliverables

- disable install button for same target while active
- show progress and current step
- show active operations list in infra UI
- show success/error notifications on completion

## Suggested File and Module Changes

Smallest coherent change set:

- `src/adaos/services/workspace_registry.py`
  - extract shared catalog-entry normalization so local and remote registry sync do not drift
- `src/adaos/services/root/service.py`
  - hook shared registry upsert into push flow for remote `adaos-registry`
- `src/adaos/apps/api/skills.py`
  - add accepted async install endpoint or extend current install endpoint to return operation metadata
- `src/adaos/apps/api/scenarios.py`
  - same for scenarios
- `src/adaos/apps/api/node_api.py`
  - expose marketplace snapshot and operation-aware infrastate actions
- `src/adaos/services/operations/*`
  - new reusable operation runtime layer
- `.adaos/workspace/skills/infrastate_skill/handlers/main.py`
  - add marketplace action, marketplace snapshot section, and operation projection consumption
- `src/adaos/integrations/adaos-client/src/app/runtime/page-modal.service.ts`
  - register marketplace modal
- `src/adaos/integrations/adaos-client/src/app/runtime/page-action.service.ts`
  - dispatch install commands and stop assuming immediate completion
- `src/adaos/integrations/adaos-client/src/app/runtime/notification-log.service.ts`
  - optionally read new `runtime.notifications` projection in addition to legacy toasts

## Backward Compatibility and Migration Risks

- Keep `registry.json` backward compatible by preserving `skills` and `scenarios` arrays.
- Keep existing sync install APIs working during migration; async operation mode can be introduced behind new response fields first.
- Keep existing `data/desktop/toasts` projection until the client fully consumes `runtime.notifications`.
- Keep `infrastate.action` as the UX entry point if desired, but route it internally to reusable operation services instead of embedding install-specific orchestration in the skill handler.

Main risk areas:

- letting registry schema drift between local workspace helper and shared registry publisher
- letting operation status become duplicated between runtime memory and Yjs
- making `InfrastateSkill` parse raw catalog payloads directly instead of using an adapter
- introducing install-specific naming that prevents later reuse for node update, diagnostics, model download, or migrations

## Architectural Decision Summary

AdaOS should evolve this area by reusing what already exists:

- local deterministic registry helpers
- hub-side `InfrastateSkill` snapshot/action composition
- client modal and notification plumbing
- Yjs persistence and reconnect behavior

But it should add one explicit layer that does not exist yet:

- a reusable operation runtime model whose state is projected into Yjs for visibility, never executed through it

That gives a coherent path from the current implementation to:

- registry-backed marketplace discovery
- UI-triggered install flows
- non-blocking hub operations
- reconnect-safe live progress and notifications

