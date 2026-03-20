# Channel Semantics

## Why this exists

AdaOS must not start from transport choices such as WebSocket, WebRTC, HTTP/2, or WebTransport.
It must start from the semantics that each logical flow requires.

This document fixes the target model for channel semantics before transport refactors.

Related documents:

- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Hub-Root Protocol](hub-root-protocol.md)
- [Transport Ownership](transport-ownership.md)
- [Realtime Reliability Roadmap](realtime-reliability-roadmap.md)

## Design rules

1. A transport is an implementation detail.
2. A logical channel must declare its delivery semantics explicitly.
3. Reliability is applied only where the message class requires it.
4. Ordering, replay, buffering, and backpressure policy are channel properties, not side effects of a socket library.
5. At any moment in time, each logical stream has one authority path unless multipath is explicitly designed.

## Message taxonomy

AdaOS traffic is split into message classes, not only subject prefixes.

### Command

Use for imperative actions that change state.

Examples:

- lifecycle transitions
- update orchestration
- route decisions
- integration actions with side effects

Required fields:

- `message_id`
- `channel_id`
- `authority`
- `issued_at`
- `ttl_ms`

Semantics:

- usually acknowledged
- may require idempotency
- replay depends on command subtype

### Request

Use for point-to-point ask/answer interactions.

Examples:

- fetch hub release state
- resolve route
- request snapshot

Semantics:

- correlated with `request_id`
- timeout-bound
- retries allowed only if request is idempotent

### Response

Use for replies to a request.

Semantics:

- correlated to a single request
- usually non-replayable after request timeout
- may be dropped if requester no longer owns the session

### StateReport

Use for durable reports of local state.

Examples:

- hub lifecycle status
- core update progress
- integration backlog status

Semantics:

- last-write-wins or versioned
- replayable within a bounded window
- duplicate-safe by design

### Event

Use for observable domain facts that may fan out.

Examples:

- `subnet.draining`
- `scenarios.synced`
- integration notifications

Semantics:

- not always acknowledged
- may be durable or ephemeral depending on subtype
- fanout policy must be declared

### SyncUpdate

Use for replicated state deltas such as Yjs document updates.

Semantics:

- replayable within a bounded window
- persistence policy defined by the sync engine
- transport must not reinterpret the payload

### Presence

Use for liveness and ephemeral membership hints.

Examples:

- online/offline hints
- awareness state
- cursor hints

Semantics:

- explicitly ephemeral
- drop allowed
- no durable replay

### RouteFrame

Use for proxied request or stream frames crossing a relay boundary.

Examples:

- browser -> hub HTTP proxy frame
- root route tunnel messages

Semantics:

- may be ordered per logical stream
- backpressure-sensitive
- replayability depends on the wrapped logical flow, not on the frame itself

### MediaFrame

Use for audio/video or other latency-sensitive stream chunks.

Semantics:

- usually non-durable
- loss-tolerant
- congestion-aware
- never shares critical reliability assumptions with control traffic

## Semantic channel types

AdaOS transport abstraction should be built around these channel types.

### CommandChannel

Properties:

- ordered per command stream
- acknowledged
- idempotency required for retryable commands
- optional bounded replay

### EventChannel

Properties:

- fanout-capable
- may be durable or ephemeral
- ordering only where consumers require it

### SyncChannel

Properties:

- versioned or cursor-based
- replayable within bounded window
- snapshot + diff capable

### PresenceChannel

Properties:

- best-effort
- non-durable
- drop allowed under pressure

### RouteChannel

Properties:

- stream-oriented
- relay-aware
- explicit buffering and backpressure policy

### MediaChannel

Properties:

- latency-first
- usually unordered or partially ordered
- transport-specific congestion handling

## Delivery classes

Each message type must also declare a delivery class.

### Class A: Must Not Lose

Examples:

- lifecycle transitions
- update orchestration
- critical task state transitions
- durable integration actions

Requirements:

- durable outbox
- ack
- bounded replay
- dedupe
- idempotency or exactly-once effect guard

### Class B: Nice To Replay

Examples:

- state reports
- non-critical event history
- bounded route recovery metadata

Requirements:

- bounded retention
- replay window
- duplicate-safe consumers preferred

### Class C: Drop Allowed

Examples:

- presence
- awareness
- transient UI hints
- media frames

Requirements:

- no durable queue
- no replay guarantee
- backpressure may drop

## Channel contract checklist

Every new logical channel in AdaOS must answer these questions:

1. Which message taxonomy class does it carry?
2. Who is the authority for that channel?
3. Is the channel durable or ephemeral?
4. Is replay required, optional, or forbidden?
5. Is ordering required?
6. What is the ack model?
7. What is the max backlog?
8. What happens in degraded mode?
9. What happens on reconnect?
10. What path owns the stream after failover?

## What this means for current AdaOS

- `hub <-> root` control traffic must be modeled as `CommandChannel`, `StateReport`, and selected `EventChannel` flows.
- `route.to_hub.*` and `route.to_browser.*` are `RouteChannel` traffic, not generic control traffic.
- Yjs document replication is `SyncChannel`.
- browser awareness is `PresenceChannel`.
- audio/video is `MediaChannel`.

This classification is the prerequisite for transport hardening and sidecar adoption.
