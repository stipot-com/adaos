# AdaOS Realtime Sidecar

## Goal

Move fragile realtime transport ownership out of the main hub process.
This document is intentionally narrower than the overall reliability model.
It describes an ownership boundary, not the full semantics of delivery, replay, idempotency, or degraded mode.

Read this together with:

- [Channel Semantics](channel-semantics.md)
- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Hub-Root Protocol](hub-root-protocol.md)
- [Transport Ownership](transport-ownership.md)
- [AdaOS Supervisor](adaos-supervisor.md)

For the first rollout, `adaos-realtime` owns the remote hub<->root WebSocket and exposes a local `nats://127.0.0.1:<port>` endpoint to the hub. The existing hub NATS bridge stays in place and connects to the local sidecar instead of the remote `wss://.../nats` endpoint.

This is intentionally minimal:

- no route/Yjs/WebRTC rewrite yet
- no protocol change between hub bridge and root
- no hub business-logic move
- only transport ownership moves out of the main process

## Current Contract

`adaos-realtime`:

- accepts one local NATS TCP client
- opens one remote NATS-over-WebSocket session to root
- relays raw NATS bytes in both directions
- writes periodic diagnostics to `.adaos/diagnostics/realtime_sidecar.jsonl`
- exposes a runtime status surface in protocol terms:
  - transport readiness
  - control readiness
  - reconnect, quarantine, and supersede counters
  - transport provenance and ownership boundary
  - current scope, lifecycle manager, and next planned boundaries
- can be inspected and restarted independently through the local control API / CLI without restarting the hub process

Supervisor / runtime boundary:

- in managed topology, `adaos-supervisor` starts, restarts, and observes the sidecar
- standalone runtime keeps a temporary fallback and may still start the sidecar itself when supervisor is absent
- hub runtime connects its existing NATS client to local `nats://127.0.0.1:7422`
- hub runtime does not install the internal WebSocket NATS transport patch while sidecar mode is enabled
- browser `/ws` and `/yws` transport still terminate in the runtime and explicitly report that ownership as transitional until Phase 2 is implemented

## Why this split

The WS failures observed on Windows are transport-loop failures, not hub domain-logic failures. Keeping WS ownership in a dedicated process gives:

- isolated event loop and socket lifecycle
- smaller failure surface
- simpler diagnostics
- a direct path to moving WebRTC and Yjs data-plane later

What this split does not solve by itself:

- durable outbox/inbox
- replay cursor semantics
- idempotent command handling
- authority boundaries
- degraded-mode policy
- local update supervision and restart-state authority

Those remain protocol and system responsibilities.

In the target local architecture, those process/update responsibilities belong to `adaos-supervisor`, not to `adaos-realtime`.

## Live Media Continuity Target

There is one especially important target scenario for the later phases.

- a member is currently producing live media over WebRTC
- the member update must be deferred while that member remains the live media producer
- the hub may still need to restart or switch runtime slots
- hub-side continuity should therefore depend on an independent sidecar path, not on the main hub runtime staying up

The intended future behavior is:

- `member` update policy: defer while member-owned live media is active
- `hub` update policy: allow runtime restart only if the hub-side realtime sidecar stays alive and can continue serving the browser/hub proxy or signaling continuity path
- sidecar continuity must be visible in diagnostics before the orchestration logic is allowed to rely on it

Current status:

- this is a target contract, not a completed capability
- reliability/runtime diagnostics now expose this as planned continuity behavior rather than silently assuming restart safety
- `adaos-supervisor` now also consumes that continuity guard and conservatively defers unsafe update/start paths instead of pretending hub restart continuity already exists
- the current sidecar still owns only `hub_root` transport and does not yet preserve live WebRTC continuity during hub runtime restart

## Rollout

### Phase 1 - NATS transport sidecar

Implemented now.

- add `adaos realtime serve`
- add local TCP NATS relay
- route hub NATS bridge through sidecar
- disable direct hub WS transport when sidecar mode is on
- expose sidecar runtime state in `GET /api/node/reliability`, CLI, and Infra State

Success criteria:

- hub startup shows `nats ws transport: sidecar`
- root sees one stable hub WS-NATS session
- no `nats keepalive pong missing` caused by hub-local WS stalls
- operators can see that sidecar owns transport only, and can inspect `transport_ready`, `control_ready`, reconnect counters, and selected remote provenance
- operators can restart sidecar transport runtime independently from hub business runtime

### Phase 2 - Route tunnel ownership

Next step.

- move `/ws` and `/yws` tunnel transport into sidecar
- keep local IPC between hub core and sidecar narrow and explicit
- leave HTTP/API orchestration in hub main process
- until the move is complete, diagnostics must expose `current_owner=runtime`, `planned_owner=sidecar`, and the current blocker for each websocket transport

Success criteria:

- browser realtime traffic no longer depends on hub main-process socket loop
- route-proxy failures do not tear down control-plane logic

### Phase 3 - Full realtime runtime

Later.

- move WebRTC signaling/media control into sidecar
- move Yjs session ownership into sidecar
- keep hub core focused on orchestration, skills, API, state transitions
- make live media continuity explicit during updates:
  - defer member updates while member-owned live media is active
  - preserve hub-side sidecar continuity while hub runtime restarts
  - keep that continuity observable through reliability, CLI, and supervisor surfaces

Success criteria:

- all long-lived realtime sockets are owned by one dedicated runtime
- hub restart and realtime restart can be reasoned about independently
- a hub runtime restart does not implicitly terminate the live media continuity path that has already been delegated to sidecar ownership

## Deferred Design Block: Sidecar-Owned Yjs Session Runtime

This is a separate future block.
It is intentionally not part of the current `Phase 2 - Route tunnel ownership`
closeout and should not be mixed into Event Model `Phase 0` completion criteria.

Why this needs its own block:

- moving only browser `"/yws"` socket ingress into sidecar is not enough to make Yjs survive slot switch
- current live Yjs room lifecycle, in-process `YRoom` ownership, direct live-room mutation paths, and room reset/reload orchestration still live in the runtime process
- as long as those room/session responsibilities stay runtime-owned, a runtime slot switch can still tear down the live Yjs continuity path even if the public `"/yws"` transport ingress has already moved

Target for the later block:

- sidecar owns Yjs websocket termination and live room/session lifecycle
- sidecar owns room reset/reload/idle-eviction orchestration for the browser-facing Yjs runtime
- runtime/core interacts with Yjs through a narrow explicit gateway instead of reaching into in-process `y_server.rooms`
- diagnostics distinguish transport ownership, session ownership, and persistence ownership instead of collapsing them into one `yws` bit

Preparatory work that is allowed before that block starts:

- reduce direct runtime dependencies on in-process live-room globals
- introduce a shared Yjs runtime gateway abstraction that can later point to runtime-local or sidecar-owned session authority
- keep `YStore`/persistence semantics explicit so session ownership can move without smuggling hub business logic into sidecar
- continue the current roadmap focus on public `"/yws"` transport cutover and communication prerequisites without claiming full Yjs session continuity yet

What this means for the current roadmap:

- Event Model `Phase 0` still depends on the current `"/yws"` transport ownership cutover track
- full sidecar-owned Yjs session/runtime continuity is a later reliability/runtime block, not a hidden extra acceptance criterion for the current phase

## Operational Notes

- Hub runtimes now default to `sidecar on` for the current `hub_root` transport scope.
- `ADAOS_REALTIME_ENABLE=0` or `HUB_REALTIME_ENABLE=0` explicitly opts out and keeps direct runtime-owned hub-root transport.
- Non-hub roles still stay `sidecar off` by default unless enabled explicitly.
- Local endpoint defaults to `nats://127.0.0.1:7422`.
- Remote candidate selection still uses existing node/root NATS configuration.
- Managed process topology prefers `systemd -> adaos-supervisor -> {adaos-runtime, adaos-realtime}`.
- Standalone runtime-owned sidecar lifecycle remains transitional compatibility only and is not the target long-term architecture.
