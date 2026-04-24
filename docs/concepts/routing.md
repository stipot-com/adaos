# Routing: IO and Skills

This document describes how AdaOS routes outgoing UI notifications (IO routing) and how a hub proxies tool calls to members (Skills routing).

## Router role

`RouterService` should not remain only a forwarding helper.
Its target role is a semantic route administrator for outputs, responses, and
browser-visible delivery paths.

The router should own decisions such as:

- what response or output is needed
- which node/runtime/browser session advertises the capability
- which target is currently able to serve it
- which path should be attempted first
- which fallback/degradation class is allowed
- which observed failures belong to the current attempt
- which health signals should keep the route active, trigger failover, or expose degradation

This means route execution and route administration are related but separate.
HTTP/WebSocket/WebRTC clients execute a route.
The router should decide whether that route is preferred, degraded, unavailable,
or superseded.

For browser-visible media routing this contract should stay explicit in
`data.media.route`, including the active `attempt`, its `sequence`,
`switch_total`, `previous_route`, `previous_member_id`, `last_switch_at`,
current `observed_failure`, and the `monitoring` cause that triggered the last
re-evaluation.

## Route administration model

For any response path chosen on behalf of a skill or scenario, the router
should track a small semantic state machine:

- need:
  - what the caller is trying to deliver or obtain
- capability:
  - which targets claim they can satisfy the need
- ability:
  - whether those targets are currently reachable, authorized, and healthy enough
- attempt:
  - which target/path is currently in flight
- degradation:
  - which fallback class is currently allowed
- observed failure:
  - the concrete failure attached to the active attempt
- monitoring:
  - the health/freshness signals that determine whether the route remains valid

This is the right place to model:

- direct vs relayed browser delivery
- local hub vs remote member execution
- response routing for skills and scenarios
- later media delivery choices such as browser-hub vs browser-member direct media

## IO Routing (stdout)

- Source: Any skill can emit `ui.notify` events. The local RouterService subscribes to `ui.notify` on the process-local `LocalEventBus`.
- Source (TTS): RouterService also listens to `ui.say` events with payload `{ text, voice? }` and routes them to the target node’s `/api/say`.
- Rules: RouterService reads `.adaos/route_rules.yaml` and picks the first rule by `priority` (descending). Minimal schema is supported:

```
node_id: "this"            # or a specific node_id
kind: "io_type"
io_type: "stdout"
priority: 50
```

Or the verbose form:

```
version: 1
rules:
  - match: {}
    target: { node_id: "<node_id>", kind: "io_type", io_type: "stdout" }
    priority: 50
```

- Delivery:
  - `this`: prints locally using `io_console.print_text` → `[IO/console@{node}] ...`.
  - Other node: Router resolves `base_url` for the target node and POSTs to `<base_url>/api/io/console/print`.

## Web IO Routing (Yjs webspaces and browser streams)

For browser-driven webspaces we use dedicated “web IO” topics that the RouterService
either projects into the appropriate Yjs doc or fans out as browser stream events.

- Source:
  - `io.out.chat.append` (chat message append)
  - `io.out.say` (enqueue TTS)
  - `io.out.media.route` (media route intent / normalized route contract)
  - `io.out.stream.publish` (transport-independent browser stream receiver payload)
- Target selection:
  - default: RouterService resolves the destination from `_meta.webspace_id` (fallback: `payload.webspace_id`, else `default`)
  - broadcast: set `_meta.webspace_ids = [...]` to fan-out into multiple webspaces
  - route table: set `_meta.route_id = '<id>'` and configure `data.routing.routes[<id>]` in the source webspace
- Projection:
  - `io.out.chat.append` -> `data.voice_chat.messages`
  - `io.out.say` -> `data.tts.queue`
  - `io.out.media.route` -> `data.media.route`
  - `io.out.stream.publish` -> semantic browser event `webio.stream.<webspace>.<receiver>`

For stream receivers the router does not write the payload back into Yjs.
The browser runtime keeps the receiver's local state in `WebIoStreamService`,
using the receiver config declared in `webui.json`.

### Routeless skills

When a tool is invoked with a payload that contains `_meta`, AdaOS sets an execution
context so `io.out.*` helpers inherit it automatically. This allows skills to emit
outputs without carrying routing logic (no explicit `_meta` plumbing in skill code).

### Member-originated browser streams

Member-originated `io.out.stream.publish` follows the same semantic path as
other member bus events:

* the member skill emits `io.out.stream.publish`
* `MemberLinkClient` forwards allowed local bus events to the hub
* the hub ingests the member event on its local bus
* `RouterService` resolves webspace targets and emits `webio.stream.*`

This means the member skill does not need to know about the transport path,
but it must still scope the output correctly with `_meta.webspace_id`,
`_meta.webspace_ids`, or `_meta.route_id`.

## Subnet Directory (hub and members)

- The hub persists the directory of nodes in SQLite: `subnet_nodes`, `subnet_capacity_io`, `subnet_capacity_skills`, `subnet_capacity_scenarios`.
- Liveness is in-memory with a TTL; on hub a background task marks nodes offline if no heartbeat is seen.
- On register/heartbeat from a member, the hub updates the node and replaces its capacity (IO + skills).
- Members periodically fetch the snapshot from hub (`GET /api/subnet/nodes`) and ingest it locally to keep their directory view up-to-date (IO + skills + scenarios).

## Skills Routing (hub → member)

If the hub receives `/api/tools/call` for a skill that is not installed locally, it looks up an online member that has this skill and proxies the call to `<member.base_url>/api/tools/call`. The hub returns the member’s JSON response unchanged.

Selection prefers active runtimes and most recent `last_seen`.

Target-state note:

- this should evolve from a single ad-hoc proxy rule into router-owned semantic route selection
- the same route-administration model should cover:
  - skill responses
  - scenario responses
  - browser-directed outputs
  - future direct media delivery
- transport adapters should execute the chosen route, not own the decision of which runtime/browser/member answers the response

## Capacity Reporting

- Each node reports its capacity via `register` and `heartbeat`:
  - `base_url`: derived from `ADAOS_SELF_BASE_URL` set by `adaos api serve`.
  - `capacity.io`: from the local capacity projection (with a default `stdout` entry if missing).
  - `capacity.skills`: skills present on the node `{ name, version, active, dev }`.
  - `capacity.scenarios`: scenarios present on the node `{ name, version, active, dev }`.

- When a skill or scenario is installed/activated/removed via the service layer, the node updates its local capacity projection and the next heartbeat includes the updated capacity for the hub to persist. On the hub, capacity is also written to SQLite immediately for the hub node.

## Media routing target

The same router semantics should eventually be extended to media.

Target model:

- a media-producing skill or scenario may run on hub or member
- the router chooses whether the browser consumes that media through:
  - local direct hub path
  - browser-hub direct WebRTC
  - browser-member direct WebRTC
  - root-routed bounded relay
- direct media signaling may still be hub- or root-mediated even when the
  media peer is browser-member
- media route degradation should be visible as a routing fact, not hidden as a
  generic transport reconnect

Current foundation in code:

- the normalized browser-visible carrier is `data.media.route`
- router-owned state already uses one vocabulary for:
  - `route_intent`
  - `preferred_route` / `active_route`
  - `producer_authority` / `producer_target`
  - `selection_reason` / `degradation_reason`
  - `member_browser_direct`
  - `monitoring.observed_failure`
- `browser <-> member` direct media is now backed by explicit per-member capability inventory in `capacity.io`
  using `io_type: webrtc_media` and flat capability tokens such as
  `webrtc:av`, `producer:member`, and `topology:member_browser_direct`
- router/reliability/media runtime now select candidate members from persisted subnet capacity and preserve
  `preferred_member_id` in the normalized route contract, even when the active route degrades to hub loopback or relay
- router now re-evaluates existing `data.media.route` contracts when browser session state or member media inventory changes,
  so route administration is a live control function rather than a one-shot projection
- router now advances an explicit `attempt` contract across those re-evaluations, so route switches and target-member changes are observable state rather than hidden transport behavior
- direct-media admission policy and dedicated signaling still need to be completed before this path becomes production-default

## Typical Flows

1. Member starts → sends `register` with `base_url` and capacity → hub persists the node and marks it online.
2. Skills install/activate on member → node updates the local capacity projection → heartbeat propagates updated capacity → hub persists it.
3. Hub restarts → restores nodes from SQLite as offline → first heartbeats mark them online again.
4. Hub receives `/api/tools/call` for a skill not present locally → proxies to an online member with that skill.
- TTS (“say”): Router also listens to `ui.say` events with payload `{ text, voice? }` and routes them to the target node’s `/api/say`.
  - Advertise capability via `capacity.io: [{ io_type: "say", capabilities: ["text",...], priority: 40 }]`.
  - Selection uses the same rule structure with `target.io_type: "say"`.
