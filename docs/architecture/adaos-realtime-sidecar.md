# AdaOS Realtime Sidecar

## Goal

Move fragile realtime transport ownership out of the main hub process.

For the first rollout, `adaos-realtime` owns the remote hub↔root WebSocket and exposes a local `nats://127.0.0.1:<port>` endpoint to the hub. The existing hub NATS bridge stays in place and connects to the local sidecar instead of the remote `wss://.../nats` endpoint.

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

Hub main process:

- starts the sidecar on hub startup
- connects its existing NATS client to local `nats://127.0.0.1:7422`
- does not install the internal WebSocket NATS transport patch while sidecar mode is enabled

## Why this split

The WS failures observed on Windows are transport-loop failures, not hub domain-logic failures. Keeping WS ownership in a dedicated process gives:

- isolated event loop and socket lifecycle
- smaller failure surface
- simpler diagnostics
- a direct path to moving WebRTC and Yjs data-plane later

## Rollout

### Phase 1 — NATS transport sidecar

Implemented now.

- add `adaos realtime serve`
- add local TCP NATS relay
- route hub NATS bridge through sidecar
- disable direct hub WS transport when sidecar mode is on

Success criteria:

- hub startup shows `nats ws transport: sidecar`
- root sees one stable hub WS-NATS session
- no `nats keepalive pong missing` caused by hub-local WS stalls

### Phase 2 — Route tunnel ownership

Next step.

- move `/ws` and `/yws` tunnel transport into sidecar
- keep local IPC between hub core and sidecar narrow and explicit
- leave HTTP/API orchestration in hub main process

Success criteria:

- browser realtime traffic no longer depends on hub main-process socket loop
- route-proxy failures do not tear down control-plane logic

### Phase 3 — Full realtime runtime

Later.

- move WebRTC signaling/media control into sidecar
- move Yjs session ownership into sidecar
- keep hub core focused on orchestration, skills, API, state transitions

Success criteria:

- all long-lived realtime sockets are owned by one dedicated runtime
- hub restart and realtime restart can be reasoned about independently

## Operational Notes

- Sidecar mode is enabled for Windows hub role by default and can be forced with `ADAOS_REALTIME_ENABLE=1`.
- Local endpoint defaults to `nats://127.0.0.1:7422`.
- Remote candidate selection still uses existing node/root NATS configuration.
