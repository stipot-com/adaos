# Routing: IO and Skills

This document describes how AdaOS routes outgoing UI notifications (IO routing) and how a hub proxies tool calls to members (Skills routing).

## IO Routing (stdout)

- Source: Any skill can emit `ui.notify` events. The local RouterService subscribes to `ui.notify` on the process-local `LocalEventBus`.
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

## Subnet Directory (hub and members)

- The hub persists the directory of nodes in SQLite: `subnet_nodes`, `subnet_capacity_io`, `subnet_capacity_skills`, `subnet_capacity_scenarios`.
- Liveness is in-memory with a TTL; on hub a background task marks nodes offline if no heartbeat is seen.
- On register/heartbeat from a member, the hub updates the node and replaces its capacity (IO + skills).
- Members periodically fetch the snapshot from hub (`GET /api/subnet/nodes`) and ingest it locally to keep their directory view up-to-date (IO + skills + scenarios).

## Skills Routing (hub → member)

If the hub receives `/api/tools/call` for a skill that is not installed locally, it looks up an online member that has this skill and proxies the call to `<member.base_url>/api/tools/call`. The hub returns the member’s JSON response unchanged.

Selection prefers active runtimes and most recent `last_seen`.

## Capacity Reporting

- Each node reports its capacity via `register` and `heartbeat`:
  - `base_url`: derived from `ADAOS_SELF_BASE_URL` set by `adaos api serve`.
  - `capacity.io`: from `.adaos/node.yaml` (with a default `stdout` entry if missing).
  - `capacity.skills`: skills present on the node `{ name, version, active, dev }`.
  - `capacity.scenarios`: scenarios present on the node `{ name, version, active, dev }`.

- When a skill or scenario is installed/activated/removed via the service layer, the node updates `.adaos/node.yaml` and the next heartbeat includes the updated capacity for the hub to persist. On the hub, capacity is also written to SQLite immediately for the hub node.

## Typical Flows

1. Member starts → sends `register` with `base_url` and capacity → hub persists the node and marks it online.
2. Skills install/activate on member → node updates `.adaos/node.yaml` → heartbeat propagates updated capacity → hub persists it.
3. Hub restarts → restores nodes from SQLite as offline → first heartbeats mark them online again.
4. Hub receives `/api/tools/call` for a skill not present locally → proxies to an online member with that skill.
- TTS (“say”): Router also listens to `ui.say` events with payload `{ text, voice? }` and routes them to the target node’s `/api/say`.
  - Advertise capability via `capacity.io: [{ io_type: "say", capabilities: ["text",...], priority: 40 }]`.
  - Selection uses the same rule structure with `target.io_type: "say"`.
