# Root MCP Foundation

`Root MCP Foundation` is the target-state AdaOS architectural layer that should be established on the `root` server before the project attempts a broad MCP rollout.

It is not a private shell bridge for Codex and not a one-off endpoint for a single test environment. It is the first vertical slice of a shared machine-readable and agent-operable foundation that should later support both:

- `LLM-assisted development` of skills and scenarios through SDK-oriented surfaces
- `LLM-assisted operations` through managed targets and governed operational capabilities

This document defines the target-state architecture and key boundaries.
Detailed sequencing is tracked in [Root MCP Roadmap](root-mcp-roadmap.md).
The human-facing control-plane counterpart is described in [Infrascope](infrascope.md).

## Executive Summary

AdaOS already has the beginnings of a canonical system model, SDK metadata export, runtime/status APIs, reliability snapshots, and root-hub communication paths. What it does not yet have is a single root-hosted agent-facing layer that can expose those capabilities in a typed, governed, auditable way.

`Root MCP Foundation` should be introduced now because later LLM-assisted development and LLM-assisted operations both depend on the same missing pieces:

- stable self-description and contract exposure
- typed tool and action envelopes
- root-scoped policy decisions
- managed target descriptors
- audit and operational event capture

The layer belongs on `root` because `root` is already the natural place for:

- `trust anchor`
- `policy point`
- `routing point`
- `audit aggregation point`
- `agent-facing capability exposure point`

## Zonal Publication Boundary

`Root MCP Foundation` is a root-hosted surface, but clients do not talk to Python modules directly.
For each self-contained zone, the zonal backend published on the zone root hostname must expose the external `Root MCP` entry family:

- `/v1/root/mcp/foundation`
- `/v1/root/mcp/contracts`
- `/v1/root/mcp/descriptors`
- `/v1/root/mcp/sessions`
- `/v1/root/mcp/call`
- `/v1/root/mcp/audit`

This is a deployment obligation for the zonal backend, not an optional convenience route.

The backend may satisfy that obligation in either of these ways:

- directly host the `Root MCP` handlers
- publish a thin facade/proxy to the local AdaOS Python API where the canonical handlers live

The second form is preferred while the canonical `Root MCP` implementation remains in Python.

The key rule is:

- zonal backend publishes the stable external `Root MCP` URL family
- canonical `Root MCP` logic stays in one place
- root-local descriptive reads must not require a subnet roundtrip
- target-routed operational reads and writes may delegate further into the subnet when the contract requires it

The first operational target should be a `test hub`, but target operations should not be implemented as an always-open infra endpoint. They should be published through an installed and authorized `infra_access_skill`, which exposes a typed operational capability surface only while enabled. This aligns better with AdaOS's skill model, lifecycle, policy model, and future control plane.

## Proposed Architecture Extension

AdaOS target architecture should add a new root-hosted layer:

- `Root MCP Foundation`

That layer should be explicitly split into two sub-surfaces:

- `MCP Development Surface`
- `MCP Operational Surface`

These are not two separate products. They are two projections over the same foundation.

### Shared Foundation Components

Both sub-surfaces should reuse the same root-hosted building blocks:

- `self-description registry` for SDK, contracts, schemas, templates, and supported surface classes
- `tool contract registry` for typed tool descriptors, arguments, output envelopes, and stability metadata
- `managed target registry` for target identity, environment, health, installed operational skills, and published capabilities
- `policy decision point` for visibility, capability grants, execution constraints, and environment guards
- `routing bridge` that maps root-level tool calls to local services or managed-target operational skills
- `audit trail` and `operational event model`
- `normalized response model` for tool responses, errors, redactions, and linked event IDs

These building blocks should stay small and stable.
They are the `foundation`, not the whole product surface.

Domain-specific MCP surfaces should evolve as `planes` over that foundation.

## Placement in AdaOS Target Architecture

`Root MCP Foundation` should sit alongside the future web control plane, not below it and not as an implementation detail of it.

```text
human operator
  -> Web Control Plane / Infrascope
  -> canonical objects, projections, inspector, topology, incidents

LLM assistant / automation agent
  -> Root MCP Foundation
     -> MCP Development Surface
     -> MCP Operational Surface

Root MCP Foundation
  -> root policy point
  -> root audit aggregation
  -> root routing and target mediation
  -> canonical descriptors and typed tool contracts

managed targets
  -> hub / member / browser-related surfaces
  -> first target: test hub
  -> operational access published by installed skills such as infra_access_skill
```

### Relationship to Major AdaOS Elements

- `root`: hosts the foundation, applies policy, records audit, and routes requests
- `hub/member`: remain runtime and managed-node actors, not the primary MCP control point
- `test hub`: first `managed target` for operational pilot work
- `skills/scenarios`: remain the main AdaOS software units; MCP development should describe and scaffold them rather than bypass them
- `SDK`: becomes the first-class development contract surface that MCP exposes in machine-readable form
- `web control plane`: remains the primary human-facing operational surface
- `MCP`: becomes the primary agent-facing typed surface

## Foundation vs Planes

`Root MCP Foundation` should not hard-code every product-facing projection directly into the kernel layer.

The preferred target-state split is:

- `Root MCP Foundation`
  - auth and session resolution
  - policy evaluation
  - audit and event model
  - routing and target resolution
  - descriptor cache and plane registry contracts
- `MCP Planes`
  - typed tool catalogs
  - descriptive and operational projections
  - domain-specific capability profiles
  - client-facing surface evolution

For managed-target execution, planes should prefer skill-mediated publication over kernel-specific endpoint growth.
The node kernel should remain stable and transport-oriented, while domain-specific operational behavior should be expressed through installed skills that publish governed capability surfaces for that target.

This keeps the root-hosted governance boundary stable while allowing multiple MCP surfaces to evolve independently.

Candidate planes include:

- `AdaOSDevPlane`
- `InfraOpsPlane`
- `ProfileOpsPlane`

The key rule is:

- planes may evolve quickly
- foundation contracts should evolve slowly

## Root MCP Foundation Model

The foundation should be designed around four shared models.

### 1. Self-Description Model

Describes what AdaOS is willing to expose to agents as a stable development or operations contract:

- SDK modules and exported tools/events
- manifest and schema registries
- canonical object vocabulary
- supported capability classes
- template catalog metadata
- stability and version metadata

### 2. Tool Contract Model

Every MCP-exposed tool should publish:

- tool id
- purpose and summary
- input schema
- output schema
- side-effect class
- required capability
- allowed environments
- timeout and concurrency hints
- redaction rules

### 3. Managed Target Model

Every operationally reachable target should publish a typed descriptor, for example:

```json
{
  "target_id": "hub:test-alpha",
  "kind": "hub",
  "environment": "test",
  "status": "online",
  "transport": {
    "channel": "hub_root_protocol"
  },
  "operational_surface": {
    "published_by": "skill:infra_access_skill",
    "enabled": true,
    "capabilities": [
      "hub.get_status",
      "hub.get_runtime_summary",
      "hub.run_healthchecks"
    ]
  },
  "policy": {
    "write_scope": "test-only"
  }
}
```

The key point is that a target is operationally available because it is registered and publishes an enabled operational surface, not because it has a permanently exposed infrastructure endpoint.

### 4. Operational Event Model

Every significant MCP-handled operation should emit a normalized event record with fields such as:

- `event_id`
- `trace_id`
- `request_id`
- `surface`
- `actor`
- `target_id`
- `tool_id`
- `capability`
- `policy_decision`
- `execution_adapter`
- `dry_run`
- `status`
- `started_at`
- `finished_at`
- `result_summary`
- `error`
- `redactions`

That event model should be shared across:

- MCP responses
- audit trail
- web UI history
- diagnostics and incident timelines
- later analytics and workflow effectiveness reporting

For browser-facing workspace consumers, that shared operational vocabulary
should now align with the dedicated
[Operational Event Model](operational-event-model.md), which adds explicit
projection-demand, lifecycle, platform-emitter, and per-webspace
materialization semantics on top of the broader audit and request/result
envelope defined here.

## Root Descriptor Cache

The foundation should also include a root-hosted `descriptor cache` for stable and pseudo-static descriptive information.

This cache exists because many MCP queries are not live operational reads.
They are descriptive questions such as:

- how AdaOS is structured
- which SDK contracts are supported
- which manifests, templates, and schemas exist
- which public skills and scenarios are available
- which MCP planes and capability profiles are published

For these queries, `root` should not need to contact a `hub` on every request.

### Cached Descriptor Classes

The first descriptor-cache classes should include:

- architecture descriptors
- SDK export metadata
- schemas and manifest contracts
- template catalog metadata
- capability profile registry
- MCP plane registry
- public skill registry summaries
- public scenario registry summaries

### Descriptor Freshness Model

Each cached descriptor should publish freshness and provenance fields such as:

- `descriptor_id`
- `source_kind`
- `source_ref`
- `generated_at`
- `fresh_until`
- `cache_ttl_sec`
- `stability`
- `content_hash`

The key rule is:

- descriptive surfaces prefer root-curated cached descriptors
- operational surfaces prefer the live authority source

This is an architectural distinction, not only a performance optimization.

## MCP-to-SDK Foundation

The first development-facing responsibility of `Root MCP Foundation` is to expose a curated, typed, machine-readable view of AdaOS development surfaces.

This should not become a direct public bridge into the SDK runtime. The SDK remains an internal and skill-oriented contract source. Root MCP should publish a `root-curated descriptor registry` over stable services, schemas, manifests, and selected SDK-derived metadata.

### Minimal Root-Hosted Development Descriptors

The initial foundation should expose descriptors for:

- root-curated SDK metadata derived from `adaos.sdk.core.exporter`
- canonical control-plane object vocabulary from `adaos.services.system_model.*`
- skill manifest schema and runtime-related manifest metadata
- scenario manifest and projection metadata
- capability class registry and permission hints
- templates and scaffold metadata, including names, intended use, and required files
- supported projection classes such as object, neighborhood, task packet, inventory, and reliability views

### Self-Description Layer Requirements

The root-hosted self-description layer should answer questions such as:

- which SDK surfaces are stable, experimental, or internal
- what contract a skill or scenario is expected to satisfy
- which inputs and outputs are available for tools and events
- which object kinds and relation kinds exist in the canonical system model
- which capability classes and action classes are supported
- which templates exist for new skills and scenarios

### What the Development Surface Should Not Be

The development surface should not default to:

- arbitrary filesystem browsing of the whole repo
- arbitrary import and execution of project modules
- raw codebase dumping as the primary contract
- direct access to secrets or environment files

Instead, the surface should expose curated descriptors, schemas, manifests, registries, template metadata, and selected examples. Raw source access may still exist elsewhere for developers, but it should not be the primary MCP abstraction.

### Descriptor Build Pipeline

The descriptive interface should be integrated into CI/CD and publish lifecycle rather than being assembled only from ad hoc runtime scraping.

The current `build SDK` work is the right starting prototype for this direction.

Target-state flow:

1. CI/CD builds descriptive artifacts
2. publish flows for skills and scenarios emit normalized registry descriptors
3. root ingests and stores those artifacts in the descriptor cache
4. descriptive MCP planes serve those cached descriptors to clients

This allows descriptive freshness to become part of software lifecycle instead of depending on runtime reachability.

The same pipeline should later cover public skills and scenarios as first-class descriptive assets.

### Evolution Path

The development-facing path should be:

1. expose root-curated descriptor sets over stable SDK/service contracts
2. expose skill/scenario template metadata and supported capability classes
3. expose task-shaped development packets for authoring or refactoring tasks
4. add draft/proposal and review-oriented flows later, once the contracts are stable

## Managed Target and `infra_access_skill` Model

### Managed Target Model

A `managed target` is an environment that `root` can identify, evaluate, and govern for operational workflows. A target should include:

- identity and environment classification
- health and reachability state
- transport/routing metadata
- ownership and policy scope
- installed operational skills
- published capabilities
- recent incidents and audit history

The first managed target should be a `test hub`.

### Goal Slice: `ProfileOps`

For supervisor-owned profiling and leak diagnostics, AdaOS should define an explicit convergence slice:

- `ProfileOps`

`ProfileOps` is the goal-state where profiling becomes a first-class part of the `MCP Operational Surface`.
It should not remain only:

- a local supervisor HTTP API
- a root `memory_profile` report family
- a CLI-only or endpoint-specific retrieval path

The intended layering is:

```text
supervisor
  -> owns profiling policy, restart-into-profile, local session state, artifact materialization

root memory_profile report family
  -> remains the replication and retrieval substrate for published summaries and selected artifacts

Root MCP Foundation
  -> publishes typed profiler contracts
  -> applies policy and scope checks
  -> audits profiler reads and writes
  -> routes tool calls either to root-published profiling evidence or to target-side bounded profiling controls

Codex / automation / Infrascope
  -> consume the same typed profiler operational surface
```

The key rule is:

- supervisor remains the profiling authority
- Root MCP becomes the typed, governed, auditable projection of that authority

This closes the current gap where profiling can reach `root`, but is not yet exposed as a first-class MCP capability surface.

### Client Model

The first external client model should also be fixed early so Root MCP can later be attached to Codex in VS Code and similar tools without rethinking the contract.

For native AdaOS clients, the baseline client configuration should be:

- `root_url`
- `subnet_id`
- `access_token`
- `zone`

The intended flow is:

1. client connects to the `root` server for a chosen zone
2. client scopes itself to a `subnet_id`
3. client authenticates with an `access_token`
4. `root` resolves managed-target and policy context before exposing operational capabilities

For external MCP clients such as Codex, this explicit shape is still important internally, but the client may not be able to provide session parameters such as `subnet_id` during MCP setup.

That constraint should be handled through a root-issued session object rather than by weakening root routing semantics.

### MCP Session Lease

External MCP access should converge on a first-class root-side object:

- `MCP Session Lease`

An `MCP Session Lease` should bind:

- `session_id`
- `audience`
- `target_id`
- `subnet_id`
- `zone`
- `capability_profile`
- effective capabilities or allowed tools
- `created_at`
- `deadline_at`
- `last_used_at`
- `use_count`
- `status`

The intended flow is:

1. an operator uses a governed operational surface such as `infra_access_skill`
2. root creates an `MCP Session Lease` for a chosen target and capability profile
3. root returns a bearer bound to that stored lease
4. later requests use only `url + bearer`
5. root resolves target, subnet, zone, and capability context from the lease itself

This makes Codex-compatible access possible without treating `subnet_id` as an MCP session parameter.

The first intended UX should be:

- user chooses `target`
- user chooses `ttl`
- user chooses a capability profile such as `ProfileOpsRead` or `ProfileOpsControl`
- `infra_access_skill` lists open sessions with target, deadline, last-used time, and usage count using root as the source of truth

### Why the First Target Should Be a Test Hub

`test hub` is the right first target because it lets AdaOS validate:

- target registration and policy gating
- root-to-target routing semantics
- typed tool contracts
- bounded execution adapters
- operational observability
- approval and rollback patterns

without exposing production environments too early.

### `infra_access_skill`

`infra_access_skill` should be the first `skill-mediated infrastructure surface`.

Its job is not to expose an unrestricted admin shell. Its job is to publish a narrow, typed, policy-aware operational capability surface when:

- the skill is installed on the target
- the skill is enabled for that target
- root policy allows the requested capability

This gives AdaOS explicit lifecycle control through:

- install
- enable
- disable
- update
- audit
- per-target policy gating

### Execution Adapters

`infra_access_skill` should delegate real work to allowlisted `execution adapters`, for example:

- runtime summary adapter
- healthcheck adapter
- logs adapter
- deploy-ref adapter
- service restart adapter
- allowed-tests adapter
- test-results adapter
- rollback adapter

Adapters should be the boundary where bounded execution, timeout handling, redaction, dry-run behavior, and environment guards are enforced.

## WebUI and Observability Model

`infra_access_skill` should be treated as an observable operational skill from the start.

### Built-In WebUI

The skill should have a web-facing operational view that can later be embedded in `Infrascope`, with at least:

- `overview`
- `requests log`
- `failures and errors`
- `capability usage`
- `policy and profile state`
- `target summary`

In Phase 1, this web surface should be able to bind directly to typed Root MCP tools rather than inventing a second observability API. The initial typed data feeds should include:

- `hub.get_operational_surface`
- `hub.get_activity_log`
- `hub.get_capability_usage_summary`
- `hub.list_access_tokens`

As MCP session leases are introduced, web-facing operational views should also be able to bind to session-management data such as:

- list open MCP sessions
- show target, deadline, last use, and usage count
- revoke or rotate sessions

### What Should Be Logged

At minimum, every handled request should record:

- incoming request envelope
- selected tool and capability
- policy decision
- execution adapter choice
- dry-run flag
- execution start and finish
- normalized result
- error and retry information
- redaction summary

### Why the WebUI Matters Early

This web surface is not optional polish. It is needed as:

- `observability layer` for operational skill behavior
- `explainability layer` for policy and routing decisions
- `effectiveness evaluation layer` for MCP-driven workflows
- first convergence point between human-facing and agent-facing control surfaces

`Infrascope` should eventually treat `infra_access_skill` as a first-class operational object with an inspector, incidents, action history, and capability-usage panels.

## Capability Model

The capability model should be explicitly split by surface.

### Development-Facing Capabilities

The first capability classes should cover read-oriented development context:

- `sdk.read.metadata`
- `sdk.read.schemas`
- `sdk.read.skill_contracts`
- `sdk.read.scenario_contracts`
- `sdk.read.templates`
- `sdk.read.capability_classes`
- `sdk.read.system_model`

These capabilities should grant access to structured descriptors, not to arbitrary repository execution.

### Operational Capabilities

Operational capabilities should initially be published through `infra_access_skill` as typed operational tools such as:

- `hub.get_status`
- `hub.get_runtime_summary`
- `hub.get_operational_surface`
- `hub.get_activity_log`
- `hub.get_capability_usage_summary`
- `hub.get_logs`
- `hub.run_healthchecks`
- `hub.issue_access_token`
- `hub.list_access_tokens`
- `hub.revoke_access_token`
- `hub.deploy_ref`
- `hub.restart_service`
- `hub.run_allowed_tests`
- `hub.get_test_results`
- `hub.rollback_last_test_deploy`

`ProfileOps` should extend that same capability model with a dedicated profiler family:

- `hub.memory.get_status`
- `hub.memory.list_sessions`
- `hub.memory.get_session`
- `hub.memory.list_incidents`
- `hub.memory.list_artifacts`
- `hub.memory.get_artifact`
- `hub.memory.start_profile`
- `hub.memory.stop_profile`
- `hub.memory.retry_profile`
- `hub.memory.publish_profile`

These should split into:

- `read-oriented profiler tools` that project supervisor/root-held profiling state
- `bounded write-oriented profiler tools` that trigger supervisor-owned control actions through governed routes

The first implementation goal should be read-first parity.
Write tools should follow only after policy, audit, and execution-route boundaries are explicit.

Capability publication should also support named capability profiles, for example:

- `ProfileOpsRead`
- `ProfileOpsControl`

This is preferable to making external MCP sessions assemble arbitrary raw capability lists by default.

Early phases should start with read-only and low-risk diagnostics, then grow into controlled writes on test targets only.

### Explicit Non-Goals and Disallowed Operations

Early `Root MCP Foundation` should not allow:

- arbitrary shell execution
- arbitrary filesystem access
- reading secrets as a generic capability
- unrestricted `docker`, `systemctl`, `git`, or package-manager access
- broad production-target operations

## Operation Routing Model

The operational routing flow should be:

1. an agent calls a root MCP tool
2. `root` resolves the tool contract, validates capability and environment policy, and creates an audit/request envelope
3. `root` resolves the `managed target` descriptor and selects the published operational surface
4. the request is routed to the target over the existing control channel family instead of inventing a parallel transport first
5. on the target, the installed `infra_access_skill` receives the request and selects an allowlisted `execution adapter`
6. the adapter performs bounded work and returns a normalized result
7. `root` records the operational event and returns a normalized MCP response linked to the audit trace

The key distinction is:

- the client always enters through the zonal `root` MCP endpoint
- `root` resolves bearer-backed session context such as `subnet_id`, `zone`, `target_id`, and capability profile
- only after that resolution does `root` decide whether the request is answered directly on root or delegated to a managed target

This means external MCP clients should never need to know target routing parameters beyond the issued bearer.
Routing context belongs to the root-side `MCP Session Lease`, not to the client transport contract.

This path is intentionally evolutionary: the first implementation should reuse existing root-hub transports and node control paths where possible, while wrapping them in typed contracts and policy checks.

For `ProfileOps`, routing should be explicitly split by evidence source:

1. `summary and catalog reads`
   route through root-published `memory_profile` reports when the requested data has already been published to root
2. `artifact reads`
   route through root-inline payloads when available, otherwise return an explicit bounded delivery contract for local-control pull
3. `profiling control actions`
   route as typed MCP operations to the managed target only when that target publishes the corresponding profiling capabilities and execution constraints

Profiler reads and writes have different authority, latency, and artifact-delivery properties and should stay explicit in the contract model.

More generally, Root MCP routing should distinguish three classes:

- `descriptive cached reads`
- `operational live reads`
- `operational bounded writes`

For `hub`-side operational execution, the preferred target-state is:

- `root` remains the single external MCP entrypoint
- `hub` exposes task-specific operational behavior through installed skills and their published capability surfaces
- the kernel supplies stable transport, identity, runtime, and policy hooks rather than accumulating feature-specific MCP projections

This keeps node-core responsibilities stable while allowing new operational surfaces to evolve as skills and planes.

These classes should not be collapsed into one undifferentiated request path, because they have different freshness, authority, and caching semantics.

## Safety, Governance, and Audit

Safety is part of the architecture, not a later hardening task.

### Governing Principles

- `root` is the policy point
- early operational scope is `test-only`
- operational access is skill-mediated, not always exposed
- capabilities are allowlisted and environment-scoped
- execution is bounded by timeouts, concurrency limits, and adapter contracts
- secrets are redacted by default and not returned as general-purpose payloads
- all write operations should be attributable to a traceable actor and request

### Required Controls

- capability grant checks
- target-environment gating
- per-tool timeout and retry policy
- concurrency limits per target and per actor
- redaction of secrets and sensitive file paths
- clear failure containment boundaries
- rollback affordances for state-changing test operations
- persistent audit trail with event IDs and trace IDs

### Longer-Term Alignment

The eventual goal is to align this model with AdaOS's broader permission and capability architecture so that SDK, web UI, MCP, and automation use the same vocabulary and policy evaluation model.


## Gap Analysis Against Current AdaOS State

### Already Aligned

- `root/hub communication paths`: `src/adaos/services/bootstrap.py`, `src/adaos/services/root/service.py`, `src/adaos/services/root/client.py`, `src/adaos/services/subnet/*`
- `machine-readable control-plane foundations`: `src/adaos/services/system_model/*`, `src/adaos/sdk/control_plane.py`, `src/adaos/sdk/data/control_plane.py`
- `SDK self-description fragments`: `src/adaos/sdk/core/exporter.py`, `src/adaos/sdk/core/decorators.py`
- `skill contract fragments`: `src/adaos/services/skill/skill_schema.json`
- `observability building blocks`: `src/adaos/services/observe.py`, `src/adaos/services/eventbus.py`, `src/adaos/services/reliability.py`
- `workspace and browser-facing surfaces`: `src/adaos/services/workspaces/*`, `src/adaos/services/scenario/webspace_runtime.py`, `src/adaos/services/yjs/*`

### Partially Aligned

- `health/status/control APIs`: there are useful node and subnet APIs, but they are still mostly HTTP-centric and not normalized as root-routed typed operational tools
- `SDK metadata exposure`: exporter support exists and is now exposed through a root-curated descriptor registry, but the descriptor set is still narrow and not yet a full authoring surface
- `scenario/skill descriptors`: manifests, registries, and projection services now have a root-level descriptor path, but the catalog is still limited and incomplete for authoring workflows
- `capability model`: `src/adaos/services/policy/capabilities.py` and SDK capability errors now have an initial Root MCP capability registry and policy gate, but the vocabulary is not yet unified across all AdaOS permission layers
- `external client access`: Root MCP now has bounded access-token primitives and a scoped client model, but issuance/rotation/revocation are still minimal and root-local
- `web UI declarative assets`: Yjs and workspace surfaces exist, but no dedicated operational-skill web UI or audit-first operator view exists yet

### Missing

- `MCP Development Surface` and `MCP Operational Surface` as explicit products
- `infra_access_skill`
- executable managed-target routing beyond the current skeleton registry and descriptors
- production-grade token issuance, rotation, revocation, and trust integration for external MCP clients
- write-capable operational tool execution through target-side execution adapters
- fully unified operational event model spanning request, policy, execution, result, and error
- workflow effectiveness views over the operational audit trail
- target-level web UI for operational skills

### Should Be Refactored Before Extension

- broad node endpoints such as `infrastate/action` should eventually give way to typed tool contracts and execution adapters
- schema and descriptor publication should be centralized so MCP does not scrape ad hoc runtime outputs
- policy and governance metadata should move toward a shared root-evaluated vocabulary across SDK, API, and MCP
- operational writes should be routed through bounded adapters rather than directly exposing generic system primitives

## Recommended Edits to Existing Architecture Notes / Roadmap Documents

This proposal should be reflected in the current docs set as follows:

- [Infrascope](infrascope.md): add an explicit relationship section between the human-facing control plane and `Root MCP Foundation`
- [Infrascope Roadmap](infrascope-roadmap.md): add `Root MCP Foundation` as a companion track and note phase alignment points
- [Architecture Overview](index.md): clarify that target-state control-plane work is documented in both `Infrascope` and `Root MCP Foundation`
- `sdk_control_plane.md` and `cli/api.md`: keep aligned with the current root-curated descriptor model, managed-target registry, and external client shape as Phase 1 evolves

## Recommended Terminology

- `Root MCP Foundation`: the root-hosted machine-readable and agent-operable foundation for future MCP support
- `MCP Development Surface`: the development-facing MCP layer that exposes SDK, contracts, schemas, and supported authoring surfaces
- `MCP Operational Surface`: the operational MCP layer that exposes typed and governed target operations
- `managed target`: a target environment that root can identify, govern, and route operational requests to
- `test hub`: the first managed target for the operational pilot
- `infra_access_skill`: the installed skill that publishes a target's operational capability surface
- `operational capability surface`: the set of typed target operations a skill is currently allowed to publish
- `typed operational tools`: explicit tool contracts such as `hub.get_status` or `hub.run_healthchecks`
- `execution adapter`: a bounded target-side adapter that performs real work behind a typed tool
- `policy point`: the root-hosted evaluation point for whether a capability may be used
- `audit trail`: the persistent record of requests, policy decisions, execution, and outcomes
- `bounded execution`: execution constrained by environment scope, adapter type, timeouts, concurrency, and redaction rules
- `operational event model`: the shared event envelope used by MCP, audit, web UI history, diagnostics, and analytics
