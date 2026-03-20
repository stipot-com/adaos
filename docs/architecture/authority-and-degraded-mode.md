# Authority And Degraded Mode

## Purpose

AdaOS needs explicit authority boundaries before transport hardening.
Without them, degraded mode becomes ambiguous and reconnect logic becomes unsafe.

This document defines who owns what and how the system behaves when root, route, sync, or integration readiness is lost.

Related documents:

- [Channel Semantics](channel-semantics.md)
- [Hub-Root Protocol](hub-root-protocol.md)
- [Transport Ownership](transport-ownership.md)

## Authority model

### Root

Root is the authority for:

- hub registration and identity validation
- hub NATS session issuance
- owner-facing authentication and root-routed membership bootstrap
- cross-subnet coordination
- externally hosted integrations routed through root
- release and update coordination that spans hubs

Root is not the authority for:

- local hub execution state
- local skill runtime internals
- local Yjs in-memory session ownership

### Hub

Hub is the authority for:

- local execution of skills and scenarios
- local event bus
- local persisted webspace and Yjs state
- local member/browser session handling after admission
- local degraded-mode execution once root is unavailable

Hub is not the authority for:

- minting fresh root-backed trust
- claiming root integration delivery without root acknowledgement
- cross-subnet global truth

### Member / Browser

Member or browser is the authority for:

- local ephemeral session state
- local cached sync state
- local media device state

Member or browser is not the authority for:

- shared durable control state
- global routing authority
- root-issued trust state

### Sidecar

A sidecar may own:

- transport lifecycle
- socket diagnostics
- reconnect loops
- local relay IO

A sidecar must not become the authority for:

- command semantics
- idempotency rules
- durable cursor semantics
- degraded-mode business policy

## Readiness tree

AdaOS readiness must be evaluated as a tree, not as one boolean.

### Hub Local Core Ready

Means:

- local process is healthy
- local storage is writable
- local event bus is functioning
- local runtime can execute scenarios and skills

### Root Control Ready

Means:

- hub has an authenticated control session to root
- auth freshness is valid
- control channel subscriptions are established
- critical root RPC path is available

### Route Ready

Means:

- relay route path is installed and subscribed
- route queues are below threshold
- authority path for proxied traffic is known

### Sync Ready

Means:

- sync engine can persist and replay updates
- current authority path for sync is healthy
- snapshot + diff recovery is available

### Integration Ready

Must be tracked per integration:

- Telegram
- GitHub
- LLM

Each integration can be ready, degraded, buffered, or unavailable independently.

### Media Ready

Means:

- media path is available for the session
- signaling may be separate from actual media transport

Media readiness must not gate core hub readiness.

## Degraded mode states

AdaOS degraded behavior must be explicit.

### State: Root Control Lost

Allowed:

- continue local skill execution
- continue local scenario execution
- continue local Yjs sync on already admitted local sessions if local path exists
- buffer selected Class A outbound hub->root messages

Restricted:

- no minting of new root-backed trust
- no claiming successful external integration delivery
- no operations that require fresh root authority

### State: Route Lost But Root Control Up

Allowed:

- control-plane operations
- integration operations
- local hub operations

Restricted:

- new proxied browser/member route sessions may be rejected or deferred
- route traffic enters degraded policy based on backlog class

### State: Sync Path Lost

Allowed:

- local persistence of pending sync updates
- snapshot/diff recovery once path is restored

Restricted:

- no claim of real-time sync readiness
- awareness may be dropped

### State: Integration Degraded

Allowed:

- continue unrelated hub logic
- buffer or reject per integration policy

Restricted:

- do not mark integration actions as completed without the integration-specific acknowledgement rule

## Degraded behavior matrix

| Capability | Hub Local Core Ready | Root Control Ready | Route Ready | Sync Ready | Integration Ready |
|---|---|---|---|---|---|
| Execute local scenarios | required | no | no | no | no |
| Existing local member/browser sessions | required | optional | optional | optional | no |
| New root-backed member admission | required | required | optional | optional | no |
| Root-routed browser proxy | required | required | required | optional | no |
| Telegram/GitHub/LLM action completion | required | required | no | no | per integration |
| Core update coordination via root | required | required | no | no | GitHub optional |

## Stale authority rules

The hub must enter `stale authority` when:

- root-issued control credentials expire
- root control session is absent beyond the configured degraded window
- membership or route authority depends on root state that is older than the allowed freshness bound

When in stale authority:

- new root-dependent admissions are rejected
- buffered commands may remain queued but not executed as if authority were fresh
- diagnostics must surface the distinction between local readiness and stale authority

## Required implementation artifacts

Any code path that introduces reconnect, replay, failover, or degraded mode must name:

- authority owner
- stale authority threshold
- allowed operations during degradation
- prohibited operations during degradation
- recovery condition

This document is the policy source for those rules.
