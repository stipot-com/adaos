# Hub-Root Protocol

## Purpose

The hub-root link is AdaOS's primary control plane.
This document defines the target protocol semantics for that link.

It does not prescribe a specific transport.
WebSocket, sidecar relay, HTTP/2, or another transport may carry the protocol, but the protocol obligations stay the same.

Related documents:

- [Channel Semantics](channel-semantics.md)
- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Transport Ownership](transport-ownership.md)

## Scope

This protocol governs traffic between hub and root for:

- control commands
- root-facing lifecycle and state reports
- root-routed integration actions
- route-control metadata

This protocol does not define:

- media transport
- raw Yjs payload shape
- browser-local ephemeral presence semantics

## Traffic classes

Hub-root traffic must be split into real execution classes, not only names.

### Control class

Examples:

- lifecycle transitions
- auth refresh
- update orchestration
- route-install readiness

Requirements:

- highest priority
- bounded queue
- acked
- durable when Class A

### Integration class

Examples:

- Telegram send job
- GitHub update check/report
- LLM task execution state

Requirements:

- per-integration buffering policy
- separate backlog accounting
- independent readiness reporting

### Route class

Examples:

- proxy route frames
- hub route request/reply envelopes

Requirements:

- isolated resource budgets
- slow consumer in route class must not starve control class

### Sync metadata class

Examples:

- sync snapshot requests
- sync cursor negotiation

Requirements:

- replayable within bounded window
- lower priority than control

## Required protocol fields

Every replayable or durable hub-root message must carry:

- `stream_id`
- `message_id`
- `message_type`
- `delivery_class`
- `issued_at`
- `ttl_ms`
- `authority_epoch`

Additionally:

- acked messages carry `ack_required=true`
- replayable messages carry `cursor`
- responses carry `request_id`

## Cursor and replay model

### Streams

Cursoring is defined per logical stream, not globally.

Examples:

- `hub-control:<hub_id>`
- `hub-integration:telegram:<hub_id>`
- `hub-integration:github:<hub_id>`

### Replay window

Replay is bounded.
AdaOS does not attempt infinite replay of all traffic.

Recommended target:

- Class A: durable bounded replay
- Class B: optional bounded replay
- Class C: no replay

### Resume

On reconnect:

1. hub reports last durable cursor per stream
2. root replays eligible messages after that cursor
3. hub dedupes by `message_id`
4. stream returns to steady state only after control readiness is restored

## Outbox and inbox model

### Hub outbox

Required only for Class A outbound messages and selected integration messages.

Properties:

- durable local storage
- retry state
- per-stream ordering where required
- dedupe-safe resend

### Root inbox dedupe

Required for replayable and retryable commands.

Properties:

- bounded dedupe window
- keyed by `message_id` and `stream_id`
- stores completion result where needed

## Idempotency rules

Not every operation can be replayed the same way.
AdaOS needs explicit command taxonomy.

### Retry-safe by idempotent handler

Examples:

- set lifecycle state to value `X`
- publish latest state report version `N`
- set route readiness status

### Retry-safe only with operation key

Examples:

- Telegram send request
- GitHub release acknowledgement
- integration job transition

These require stable operation keys and dedupe-aware completion handling.

### Not replayable

Examples:

- transient route frames
- presence changes
- media signaling hints that are session-local and expired

## Backpressure and resource isolation

Hub-root classes must not share one undifferentiated pressure domain.

The implementation must isolate:

- queue budgets
- worker budgets
- retry budgets
- drop policies

Minimum rule:

- route backlog must not block control acks or heartbeats

## Readiness conditions for hub-root

The hub-root protocol is ready only when all of these are true:

1. authenticated session established
2. authority freshness valid
3. control subscriptions ready
4. control ack path healthy
5. replay reconciliation finished

Route or integration readiness may still be degraded after control becomes ready.

## Observability requirements

The protocol layer must expose:

- current authority epoch
- per-stream last sent cursor
- per-stream last acked cursor
- outbox size by class
- dedupe hit counts
- replay counts
- reconnect count
- degraded reason

These metrics are protocol metrics, not transport-only metrics.

## Mapping to current code

Current code already contains the transport-oriented pieces:

- hub NATS session issuance on root
- hub bridge and reconnect handling
- route subjects and route proxy

Those pieces must be evolved toward the protocol defined here rather than only patched at the socket layer.
