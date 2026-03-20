# Realtime Reliability Roadmap

## Goal

Fix AdaOS reliability from the top down:

1. message semantics
2. authority and degraded behavior
3. hub-root protocol hardening
4. transport ownership boundaries
5. hub-member transport abstraction
6. sync and media specialization

This ordering is deliberate.
The project must not start with sidecar or transport adapters as if they alone solved reliability.

## Phase 0: Architecture freeze

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

## Phase 1: Hub-root protocol hardening

### Focus

Strengthen the most critical control plane first.

### Work items

- classify current hub-root messages by semantics and delivery class
- isolate control, integration, route, and sync-metadata traffic by real budgets
- add explicit per-stream cursors where replay is required
- add durable outbox only for Class A and selected integration flows
- add inbox dedupe where retry/replay exists
- define command-specific idempotency rules
- expose readiness tree in status and diagnostics

### Candidate code areas

- `src/adaos/services/bootstrap.py`
- `src/adaos/integrations/inimatic/backend/app.ts`
- `tools/diag_nats_ws.py`
- `tools/diag_route_probe.py`

### Exit criteria

- route pressure cannot starve control readiness
- reconnect restores control readiness through explicit protocol state
- critical hub-root actions are duplicate-safe

## Phase 2: Sidecar as transport ownership boundary

### Focus

Move transport ownership where it reduces blast radius, without moving protocol semantics.

### Work items

- define sidecar status API in protocol terms
- expose control readiness, route readiness, and reconnect diagnostics
- ensure hub main process remains owner of durability and degraded policy
- use sidecar first for hub-root transport lifecycle

### Candidate code areas

- realtime sidecar runtime
- hub startup and shutdown wiring
- diagnostics aggregation

### Exit criteria

- transport failures are isolated from hub business logic
- sidecar does not become a hidden protocol authority

## Phase 3: Hub-member semantic channels

### Focus

Build abstraction from logical channel semantics, not from transport names.

### Work items

- define `CommandChannel`, `EventChannel`, `SyncChannel`, `PresenceChannel`, `RouteChannel`, `MediaChannel`
- map existing `/ws`, `/yws`, WebRTC data channels, and root relay traffic to those channel types
- define path selection, failover, freeze period, and duplicate suppression rules

### Candidate code areas

- `src/adaos/services/webrtc/peer.py`
- browser/hub websocket gateways
- route proxy logic

### Exit criteria

- one logical stream has one active authority path
- failover rules are explicit
- adapters no longer leak transport semantics into application code

## Phase 4: Yjs as SyncChannel

### Focus

Make Yjs transport-independent without building a second distributed system around it.

### Work items

- append-only bounded update log
- snapshot + diff recovery
- client local persistence
- awareness explicitly ephemeral

### Exit criteria

- document updates survive reconnect within replay window
- awareness may drop without compromising document state

## Phase 5: Media plane

### Focus

Keep media architecture separate, but do not let it block core messaging stabilization.

### Work items

- define media signaling authority
- define direct vs relay policy
- keep media readiness outside core control readiness

### Exit criteria

- media path is architecturally isolated from control and sync hardening

## Immediate implementation order

The next coding steps should follow this order:

1. classify existing hub-root messages and subjects by taxonomy
2. add readiness tree representation to diagnostics and status
3. identify Class A hub-root flows and design outbox/inbox around them
4. isolate route backlog from control backlog in runtime resources
5. only then tighten sidecar adoption

## Non-goals for the first iteration

- universal transport abstraction for every edge case
- production-grade media relay
- infinite replay of all traffic
- exactly-once delivery for every message

The first iteration is about explicit guarantees, not maximum theoretical reliability.
