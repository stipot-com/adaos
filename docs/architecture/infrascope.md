# Infrascope

`Infrascope` is the AdaOS control-plane scenario for evaluating, understanding, and operating a subnet as a living distributed system.

It should not be designed as a single page or a static admin panel. AdaOS infrastructure combines node topology, runtime placement, device reachability, scenario execution, quotas, trust, and user-facing workspaces. The same system must stay understandable for a human operator, machine-readable for an LLM, and governable in a future multi-user environment.

This document defines the target-state architecture. The phased delivery plan lives in [Infrascope Roadmap](infrascope-roadmap.md).

## Goals

`Infrascope` should let AdaOS answer, quickly and consistently:

- what is failing right now
- where the failure sits: root, hub, member, browser, device, runtime, or policy
- what depends on the selected object
- what resources or quotas are under pressure
- what changed recently
- what a human, LLM, or automation agent is allowed to see and do next

## Design Principles

1. `canonical system model` first.
   UI, API, audits, and LLM context packets are projections of the same model.
2. One system, multiple lenses.
   `overview`, `topology`, `inventory`, `runtime`, `resources`, and `incidents` are coordinated views over shared objects.
3. Object-centric interaction.
   Users should inspect and act on a selected object through a unified inspector instead of navigating into isolated pages.
4. `profile-aware` from the start.
   Visibility, terminology, defaults, and available actions depend on profile, role, workspace, and policy.
5. Desired state and actual state must coexist.
   `Infrascope` is not only for observation; it is also the basis for safe change planning and review.
6. LLM reads structured projections, not the DOM or raw logs by default.
   Logs remain supporting evidence, not the primary control-plane contract.

## Participants

The same infrastructure model is consumed by different actors:

- `owner / super-admin`: full-system operational and governance view
- `infra operator`: health, incidents, connectivity, resources, and safe actions
- `developer`: runtimes, deployments, versions, event traces, and scenario/skill dependencies
- `household user`: rooms, devices, automations, and household-facing alerts
- `LLM assistant`: task-shaped machine context, formal actions, constraints, and impact previews
- `automation agent`: narrow machine-to-machine control with explicit permissions

These actors should not receive separate object models. They receive the same object with different overlays.

## Canonical System Model

The target-state model is a `double digital twin`.

### Operational Twin

Describes what exists and what is happening:

- `root`
- `hub`
- `member`
- `browser_session`
- `device`
- `smart_home_endpoint`
- `skill`
- `scenario`
- `runtime`
- `connection`
- `route`
- `resource_quota`
- `event_stream`
- `incident`

### Governance Twin

Describes ownership, interpretation, and allowed control:

- `identity`
- `profile`
- `workspace`
- `role`
- `policy`
- `team`
- `ownership`
- `review`
- `change_request`

Together they define both infrastructure truth and how that truth is shown, explained, and operated.

## Object Contract

Every object that participates in `Infrascope` should expose the same minimum contract.

```json
{
  "id": "hub:alpha",
  "kind": "hub",
  "title": "Hub Alpha",
  "summary": "Primary home subnet hub",
  "status": "degraded",
  "health": {
    "availability": 0.91,
    "connectivity": "unstable",
    "resource_pressure": "medium"
  },
  "relations": {
    "parent": "root:main",
    "members": ["member:m1", "member:m2"],
    "connected_browsers": ["browser:b7"],
    "devices": ["device:lamp-kitchen"]
  },
  "resources": {
    "cpu": 0.74,
    "ram": 0.81,
    "disk": 0.43
  },
  "runtime": {
    "skills_active": 12,
    "scenarios_active": 3,
    "failed_runs_24h": 5
  },
  "versioning": {
    "desired": "2026.03.23",
    "actual": "2026.03.20",
    "drift": true
  },
  "desired_state": {},
  "actual_state": {},
  "incidents": [],
  "actions": [],
  "governance": {
    "tenant_id": "tenant:main",
    "owner_id": "profile:ops-team",
    "visibility": ["role:admin"],
    "roles_allowed": ["role:infra-admin"],
    "shared_with": []
  },
  "representations": {
    "system": {},
    "operator": {},
    "user": {},
    "llm": {}
  },
  "audit": {
    "created_by": "system:bootstrap",
    "updated_by": "profile:ops-team",
    "last_seen": "2026-04-05T10:00:00Z",
    "last_changed": "2026-04-05T09:57:00Z"
  }
}
```

### Required Status Vocabulary

At minimum the contract should normalize:

- `online | offline | degraded | warning | unknown`
- `reachable | unreachable`
- `authenticated | untrusted | expired`
- `normal | overloaded | throttled`
- `synced | outdated | drifted`
- `installed | active | broken | pending_update`

## Projection Layer

The canonical object is not sent raw to every consumer. `Infrascope` needs stable projections.

### Object Projection

Standard typed envelope for a single object.

Used by:

- inventory rows
- inspector cards
- API responses
- LLM local-object context

### Topology Projection

Graph slice over selected nodes and edges.

Used by:

- subnet map
- dependency and impact views
- path and failure analysis

### Narrative Projection

Short pre-computed explanation for operator and LLM reasoning.

Example fields:

- `summary`
- `current_issue`
- `operator_focus`
- `risk_summary`

### Action Projection

Formal, permission-aware action descriptors.

Example fields:

- `id`
- `title`
- `requires_role`
- `risk`
- `affects`
- `preconditions`

### Task Packet

Task-shaped context bundle for LLM or automation:

- `local_object`
- `neighborhood`
- `task_goal`
- `policy_context`
- `allowed_actions`
- `relevant_incidents`
- `recent_changes`

This packet is the preferred LLM interface. It avoids dumping the entire infrastructure or leaking unauthorized data.

## Representation Layers

Each object should support multiple overlays over the same source of truth:

- `system representation`: exact runtime and infrastructure detail
- `operational representation`: operator and developer control surface
- `user representation`: household or simplified workspace framing
- `llm representation`: concise, typed, reasoning-friendly shape

Example:

- an admin sees `mTLS`, `version drift`, `routes`, and `event pressure`
- a household user sees "main home node", "12 devices connected", and "1 automation delayed"
- an LLM sees `relations`, `health vector`, `incidents`, `constraints`, and `formal actions`

## UI Composition

`Infrascope` should behave like an operator workspace, not a collection of disconnected pages.

### Primary Modes

- `overview`: what is happening right now
- `topology`: how the subnet is connected
- `inventory`: what objects exist and how to filter them
- `runtime`: what scenarios and skills are running now
- `resources`: where quota or capacity pressure sits
- `events`: recent streams, traces, and subscriptions
- `incidents`: what is broken and what is affected
- `policies / ownership / changes`: who owns what and how changes are governed

### Shared Shell

- left panel: workspace navigation and saved views
- center canvas: map, table, timeline, or detail workspace
- right panel: unified object inspector

### Unified Object Inspector

The inspector should stay consistent across object kinds and expose tabs such as:

- `summary`
- `topology`
- `runtimes`
- `resources`
- `events / logs`
- `governance`
- `actions`

The goal is to inspect without losing context or opening a new page for every click.

## Core Views

### Overview

The starting point should provide operational compression, not a generic dashboard.

Recommended sections:

- `health strip`: root, hubs, members, browsers, devices, event bus, LLM, Telegram
- `active incidents`
- `topology snapshot`
- `runtime pressure`
- `external services and quotas`
- `recently changed`

### Topology

The topology view should be a managed graph, not a giant untamed diagram.

Required capabilities:

- layered modes: `physical`, `connectivity`, `runtime`, `resource`, `capability`
- local graph expansion and neighborhood isolation
- highlighting of degraded and failed paths
- impact preview before disruptive actions
- version and sync drift overlays

### Inventory and Control

Inventory should stay separate from live runtime, while sharing the same object contract.

Suggested tabs:

- hubs
- members
- browsers
- devices
- skills
- scenarios
- runtimes
- quotas
- policies

### Runtime

Runtime deserves its own mode because AdaOS is orchestration-heavy.

It should explain:

- which scenarios are active
- where they are instantiated
- which skills are being called
- which events are in flight
- where retries, backlogs, or failures accumulate
- which user, browser, and device are involved

## Five-Second Questions

`Infrascope` is successful when it can answer these questions in seconds:

- Why is a scenario failing right now?
- Is this a root, hub, member, browser, device, or policy problem?
- Is a device physically unreachable or logically blocked?
- Which object is causing a quota or resource spike?
- Which skills are active on this hub?
- What changed before the degradation started?
- What else breaks if I restart this hub or update this skill?
- What can the current user or LLM safely do next?

## Current AdaOS Anchors

The design should extend current runtime surfaces rather than replace them wholesale.

- node and subnet state: `src/adaos/apps/api/node_api.py`, `src/adaos/apps/api/subnet_api.py`, `src/adaos/services/reliability.py`, `src/adaos/services/subnet/*`, `src/adaos/services/root/service.py`
- runtime inventories: `src/adaos/apps/api/skills.py`, `src/adaos/apps/api/scenarios.py`, `src/adaos/services/skill/*`, `src/adaos/services/scenario/*`
- observations and events: `src/adaos/apps/api/observe_api.py`, `src/adaos/services/observe.py`, `src/adaos/services/eventbus.py`
- workspaces, Yjs, and browser-facing overlays: `src/adaos/services/workspaces/*`, `src/adaos/services/yjs/*`, `src/adaos/services/scenario/webspace_runtime.py`
- profiles and governance roots: `src/adaos/services/user/profile.py`, `src/adaos/services/policy/*`

These are the existing producers from which the canonical model and projections should be composed.

## Anti-Patterns

Avoid the following:

- one giant graph without filters or neighborhood focus
- mixing inventory rows with live runtime instances as if they were the same thing
- treating browser sessions, members, and devices as interchangeable nodes
- hiding dependencies and impact radius
- relying on color without explicit text state
- forcing a full page transition on every object click
- giving LLM raw infrastructure dumps instead of constrained task packets

## MVP Target

The first usable `Infrascope` should include:

- `overview` with health, incidents, runtime pressure, quotas, and recent changes
- `topology` for root, hubs, members, browsers, and devices
- `inventory` tabs for core object classes
- unified `object inspector`
- basic `runtime` panel for active scenarios and failed runs

This MVP is intentionally operational. Recommendation layers, anomaly detection, and change proposals come later in the roadmap.
