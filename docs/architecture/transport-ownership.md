# Transport Ownership

## Purpose

This document defines ownership boundaries between:

- hub main process
- optional realtime sidecar
- root
- member/browser transports

It exists to prevent transport refactors from accidentally absorbing protocol semantics.

Related documents:

- [Channel Semantics](channel-semantics.md)
- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Hub-Root Protocol](hub-root-protocol.md)
- [AdaOS Realtime Sidecar](adaos-realtime-sidecar.md)

## Ownership rule

Transport ownership is about lifecycle of connections, sockets, and relay loops.
It is not ownership of message semantics.

## Hub main process responsibilities

The hub main process owns:

- local runtime state
- skills and scenarios
- local event bus
- local storage
- business semantics of control, sync, and integration actions
- degraded-mode policy decisions

The hub main process should not directly own:

- every long-lived remote socket if that causes operational fragility

## Sidecar responsibilities

If a sidecar is used, it owns:

- remote socket lifecycle
- low-level reconnect loops
- heartbeat wiring
- wire diagnostics
- local relay endpoint for the hub main process

The sidecar must not own:

- durable outbox semantics
- replay cursor authority
- idempotency policy
- command classification
- degraded-mode authority

## Root responsibilities

Root owns:

- session admission for root-backed channels
- trust issuance
- root-side replay and dedupe for eligible streams
- route rendezvous where applicable

Root does not own:

- local hub execution semantics
- direct member/browser local persistence

## Member/Browser transport ownership

Member or browser transport implementations may vary, but the logical channel owner is determined by semantics.

Examples:

- `CommandChannel` may be owned by hub or root depending on authority
- `SyncChannel` state may be hub-owned even if a relay path carries it
- `MediaChannel` transport may be peer-to-peer while signaling remains hub- or root-mediated

This distinction is especially important for direct media:

- a browser may consume media from the hub over a direct peer
- a browser may later consume media from a member over a direct peer
- in both cases, transport endpoint ownership may be `browser <-> hub` or `browser <-> member`
- but admission, path authority, degradation policy, and signaling rendezvous may still remain hub- or root-mediated

AdaOS should therefore treat "who terminates the media peer" and
"who decides whether that peer is allowed/preferred/degraded" as separate questions.

## Routing authority versus transport ownership

Transport ownership does not answer:

- which target should answer a skill or scenario response
- whether direct, relayed, or deferred delivery is currently allowed
- whether a path should be retried, degraded, quarantined, or declared failed
- whether a response should switch from one target/path to another

Those are routing semantics, not socket semantics.

AdaOS should keep an explicit routing authority layer that can evaluate:

- need: what response or media flow is being requested
- capability: which node or runtime claims it can serve it
- ability: whether it is currently reachable and allowed to serve it
- attempt: which route/path is currently being tried
- degradation: which fallback policy applies when the preferred route is weak
- observed failure: what concrete incident happened on the active attempt
- monitoring: whether the route has become healthy, stale, pressured, or failed

That routing authority may be implemented by a `router` service or semantic
layer, but it must not be hidden inside transport adapters.

## Path selection policy

AdaOS must not allow uncontrolled path flapping.

For any logical stream, policy must define:

- preferred path
- fallback path
- handover condition
- freeze period after failover
- duplicate suppression strategy

At any time a logical stream has one active authority path unless multipath is explicitly designed.

## Why sidecar is not first

A sidecar is useful as an ownership boundary only after protocol semantics are fixed.

Without that, a sidecar is just a cleaner wrapper around a fragile channel.

Therefore:

1. define semantics first
2. harden hub-root protocol second
3. move transport ownership to sidecar where it reduces blast radius

## Current AdaOS interpretation

For the near term:

- sidecar is a transport isolation tool for `hub <-> root`
- hub main process remains the owner of protocol semantics
- browser/member transport abstraction must be built from logical channel semantics, not from a list of transport libraries
