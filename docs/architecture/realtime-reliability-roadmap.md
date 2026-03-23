# Realtime Reliability Roadmap

## Goal

Fix AdaOS reliability from the top down:

1. message semantics
2. authority and degraded behavior
3. hub-root protocol hardening
4. transport ownership boundaries
5. hub-member transport abstraction and semantic channels
6. sync and media specialization
7. skills and scenarios lifecycle hardening

This ordering is deliberate.
The project must not start with sidecar or transport adapters as if they alone solved reliability.

## Current status: 2026-03-21

### Done

- architecture documents for channel semantics, authority, hub-root protocol, and transport ownership are in place
- runtime reliability model is represented in code and exposed through `GET /api/node/reliability`
- `adaos node reliability` surfaces readiness, degraded matrix, and channel diagnostics
- Infra State shows realtime summary and transport diagnostics through Yjs-backed UI
- runtime now exposes canonical channel overview entries for `hub_root`, `hub_root_browser`, and `browser_hub_sync`
- runtime now exposes `hub_root_transport_strategy` with current transport, candidate list, recent attempts, reconnect/failure history, and active hypothesis parameters
- CLI and Infra State now surface the current hub-root transport strategy instead of only the last readiness bit
- detailed channel trace is no longer a default console behavior; summary/incident output remains visible while deep console trace is explicit opt-in
- channel stability is now assessed from incidents and transport churn, not only from the last connected snapshot
- repo workspace fallback exists for built-in skills, scenarios, and `webui.json`
- built-in fallback for `web_desktop` restores the return path from scenario views when scenario assets are missing on a hub
- canonical runtime store for skill-local env and memory moved to `.runtime/<skill>/data/db/skill_env.json`

### In progress

- hub-root readiness is observable, but delivery guarantees are not yet enforced by an explicit Class A protocol layer
- route and root-control incident classes still need clearer separation
- transport strategy is now visible, but automatic policy-driven transport switching is not yet the default runtime behavior
- sidecar ownership boundary is documented, but not yet the default hardened runtime path
- Yjs ownership boundaries for desktop and scenario state are still implicit

### Confirmed gaps

- transport/resource isolation is still weaker than subject naming suggests
- hub-root message inventory is not yet fully classified by delivery class and idempotency policy
- route/session incidents are still underrepresented compared to root-control incidents
- system skills and scenarios still rely on a transitional mix of `workspace`, `repo workspace`, `runtime slot`, and `built-in seed`

## Phase 0: Architecture freeze

### Status

Completed.

### Deliverables

- [Channel Semantics](channel-semantics.md)
- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Hub-Root Protocol](hub-root-protocol.md)
- [Transport Ownership](transport-ownership.md)

### Exit criteria

- message taxonomy approved
- delivery classes approved
- readiness tree approved
- degraded matrix approved
- authority boundaries approved

## Phase 1: Observability and incident-driven readiness

### Status

Completed for observability scope.
The model is visible in diagnostics, route/session incidents are now classified separately from root-control incidents, and transport/sidecar provenance is exposed in the runtime snapshot.
The next step is protocol hardening, not more ad-hoc diagnostics.

### Focus

Make readiness and degradation visible before changing protocol ownership.

### Work items

- keep readiness tree and degraded matrix visible in node API, CLI, and Infra State
- keep channel stability derived from incidents, reconnect churn, and watchdog failures
- separate `root_control` transport assessment from route/session incidents
- expose provenance of current transport and current artifact source in diagnostics
- make remote and direct hub diagnostics consistent when browser is connected to `:8777`

### Candidate code areas

- `src/adaos/services/reliability.py`
- `src/adaos/services/bootstrap.py`
- `.adaos/workspace/skills/infrastate_skill`
- `tools/diag_nats_ws.py`
- `tools/diag_route_probe.py`

### Exit criteria

- `ready/stable` is never reported when fresh incidents prove the channel is unstable
- route-session failures and root-control failures are visible as different incident classes
- operator can tell whether a problem is transport, route, sync, or artifact-source related

## Phase 2: Hub-root protocol hardening

### Focus

Strengthen the most critical control plane first.

### Status

Checkpoint reached.
Runtime now exposes explicit hub-root traffic classes with per-class pending budgets, live subscription/backpressure metrics, route runtime pressure, and integration outbox state.
The critical control-plane state report `hub_root.control.lifecycle` is now also explicit: hub reports carry stable `stream_id/message_id/cursor`, hub persists pending ack state locally, and root rejects stale or duplicate lifecycle reports by cursor/message id.
The first concrete Class A stream is now explicit for `hub_root.integration.github_core_update`: hub reports carry stable `stream_id/message_id/cursor`, the hub persists pending ack state locally, and root rejects stale or duplicate state reports by cursor/message id.
The selected retryable integration flow `hub_root.integration.telegram` now carries an explicit `operation_key`, and root suppresses duplicate Telegram sends inside a bounded Redis TTL window instead of relying on text-only heuristics.
The selected retryable integration flow `hub_root.integration.llm` now carries an explicit `request_id`, and root suppresses duplicate LLM retries by serving a bounded cached response when the same logical request is replayed with the same request fingerprint.
Root-side report provenance is now queryable for the explicit control/core-update streams, including root receive time and ack result, so operators can verify protocol state without direct Redis inspection.
Generalized Class A durability, inbox dedupe, and cursor-based replay are still pending for the remaining integration and control flows beyond lifecycle/core-update.

### Work items

- classify current hub-root messages by taxonomy and delivery class
- isolate control, integration, route, and sync-metadata traffic by real budgets
- split queues, workers, limits, and backpressure policy, not only subject prefixes
- add explicit per-stream cursors where replay is required
- add durable outbox only for Class A and selected integration flows
- add inbox dedupe where retry or replay exists
- define command-specific idempotency rules
- define stale-authority thresholds per hub-root flow

### Candidate code areas

- `src/adaos/services/bootstrap.py`
- `src/adaos/integrations/inimatic/backend/app.ts`
- `src/adaos/services/reliability.py`
- `tools/diag_nats_ws.py`
- `tools/diag_route_probe.py`

### Exit criteria

- route pressure cannot starve control readiness
- reconnect restores control readiness through explicit protocol state
- critical hub-root actions are duplicate-safe
- degraded mode is driven by explicit authority and delivery rules

## Phase 3: Sidecar as transport ownership boundary

### Focus

Move transport ownership where it reduces blast radius, without moving protocol semantics.

### Work items

- define sidecar status API in protocol terms
- expose control readiness, route readiness, reconnect diagnostics, and transport provenance
- ensure hub main process remains owner of durability and degraded policy
- use sidecar first for hub-root transport lifecycle after protocol guarantees are explicit

### Candidate code areas

- realtime sidecar runtime
- hub startup and shutdown wiring
- diagnostics aggregation

### Exit criteria

- transport failures are isolated from hub business logic
- sidecar does not become a hidden protocol authority

## Phase 4: Hub-member semantic channels

### Focus

Build abstraction from logical channel semantics, not from transport names.

### Work items

- define `CommandChannel`, `EventChannel`, `SyncChannel`, `PresenceChannel`, `RouteChannel`, `MediaChannel`
- map existing `/ws`, `/yws`, WebRTC data channels, and root relay traffic to those channel types
- define path selection, failover, freeze period, and duplicate suppression rules
- keep one active authority path per logical stream unless multipath is explicitly designed

### Candidate code areas

- `src/adaos/services/webrtc/peer.py`
- browser/hub websocket gateways
- route proxy logic

### Exit criteria

- one logical stream has one active authority path
- failover rules are explicit
- adapters no longer leak transport semantics into application code

## Phase 5: Yjs as SyncChannel

### Focus

Make Yjs transport-independent without building a second distributed system around it.

### Work items

- append-only bounded update log
- snapshot + diff recovery
- client local persistence
- awareness explicitly ephemeral
- explicit resync path after route or transport churn

### Candidate code areas

- sync engine
- Yjs gateway and recovery paths
- local persistence integration

### Exit criteria

- document updates survive reconnect within replay window
- Yjs reliability is not duplicated blindly across transport, log, and UI layers
- awareness may drop without compromising document state

## Phase 6: Media plane

### Focus

Keep media architecture separate, but do not let it block core messaging stabilization.

### Work items

- define media signaling authority
- define direct vs relay policy
- keep media readiness outside core control readiness

### Exit criteria

- media path is architecturally isolated from control and sync hardening

## Phase 7: Skills and scenarios lifecycle hardening

### Focus

Handle artifact provenance, scenario UX, and runtime lifecycle after the communication model is hardened.

### Work items

- define a first-class artifact model for system skills and scenarios
- separate three lifecycle operations clearly:
  - `source sync`
  - `runtime refresh`
  - `A/B rollout`
- add change classification for skill updates so the system can decide whether `runtime_update` is enough
- make `desktop.scenario.set` transactional and observable with `requested`, `effective`, and `error` state
- define explicit ownership for Yjs subtrees such as `ui`, `data`, `registry`, and desktop-installed artifacts
- surface artifact provenance in diagnostics: `workspace`, `repo_workspace`, `runtime_slot`, `built_in_seed`, `dev`
- preserve scenario install/open state through reload and rebuild of current Yjs projection

### Candidate code areas

- `src/adaos/services/skill/manager.py`
- `src/adaos/services/skill/update.py`
- `src/adaos/services/scenario/manager.py`
- `src/adaos/services/scenarios/loader.py`
- `src/adaos/services/scenario/webspace_runtime.py`
- `src/adaos/services/skills_loader_importlib.py`
- `src/adaos/services/yjs/bootstrap.py`
- `src/adaos/services/yjs/gateway_ws.py`
- `src/adaos/integrations/inimatic/src/app/runtime/desktop-schema.service.ts`

### Exit criteria

- operator can explain how a skill or scenario update propagates without reading the code
- scenario switch result is observable and not inferred from UI side effects
- remote hubs behave consistently even when local workspace assets are absent
- installed desktop items and current scenario recover correctly after Yjs rebuild or reconnect

## Immediate implementation order

The next coding steps should follow this order:

1. inventory existing hub-root subjects and messages by taxonomy and delivery class
2. isolate route backlog from control backlog in real runtime resources
3. define Class A hub-root flows and add outbox, inbox, and idempotency where required
4. separate root-control and route/session incidents in readiness and diagnostics
5. only then tighten sidecar adoption
6. then move to hub-member, Yjs, and media semantics
7. after the communication phases, harden skills and scenarios lifecycle and scenario UX

## Non-goals for the first iteration

- universal transport abstraction for every edge case
- production-grade media relay
- infinite replay of all traffic
- exactly-once delivery for every message
- replacing all fallback paths before provenance and lifecycle are formalized

The first iteration is about explicit guarantees, provenance, and operational clarity, not maximum theoretical reliability.
