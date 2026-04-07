# Root MCP Foundation

`Root MCP Foundation` is the target-state AdaOS architectural layer that should be established on the `root` server before the project attempts a broad MCP rollout.

It is not a private shell bridge for Codex and not a one-off endpoint for a single test environment. It is the first vertical slice of a shared machine-readable and agent-operable foundation that should later support both:

- `LLM-assisted development` of skills and scenarios through SDK-oriented surfaces
- `LLM-assisted operations` through managed targets and governed operational capabilities

This document defines the target-state extension and the early implementation path. The human-facing control-plane counterpart is described in [Infrascope](infrascope.md).

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

### Client Model

The first external client model should also be fixed early so Root MCP can later be attached to Codex in VS Code and similar tools without rethinking the contract.

The baseline client configuration should be:

- `root_url`
- `subnet_id`
- `access_token`
- `zone`

The intended flow is:

1. client connects to the `root` server for a chosen zone
2. client scopes itself to a `subnet_id`
3. client authenticates with an `access_token`
4. `root` resolves managed-target and policy context before exposing operational capabilities

On later phases, `infra_access_skill` may become the issuer or bootstrap mediator for target-scoped access tokens. What matters at this stage is fixing the client-facing shape early, even before the token-issuance lifecycle is fully implemented.

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
- `hub.get_logs`
- `hub.run_healthchecks`
- `hub.deploy_ref`
- `hub.restart_service`
- `hub.run_allowed_tests`
- `hub.get_test_results`
- `hub.rollback_last_test_deploy`

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

This path is intentionally evolutionary: the first implementation should reuse existing root-hub transports and node control paths where possible, while wrapping them in typed contracts and policy checks.

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

## Roadmap Extension

`Root MCP Foundation` should evolve as a companion roadmap track.

### Phase 0. Architectural Fixation

- fix terminology and boundaries
- define root-first MCP model
- define split between development and operational surfaces
- define managed target model
- define `infra_access_skill` concept
- define operational event and observability model

Phase 0 is complete when:

- the terminology and placement are fixed in architecture docs
- `Infrascope`, SDK-first control-plane docs, and roadmap notes reference the same MCP foundation model
- `infra_access_skill` is established as the preferred target-side operational surface
- `test hub` is established as the first managed target in the architecture notes

### Phase 1. Root MCP Foundation Skeleton

- add minimal root MCP entrypoint and request/response envelopes
- add root-hosted tool contract registry
- add initial audit primitives and event IDs
- expose only minimal read-oriented descriptors and placeholder operational contracts

### Current Phase 1 Implementation Slice

The current code checkpoint establishes the first root-hosted skeleton under:

- `src/adaos/services/root_mcp/model.py`
- `src/adaos/services/root_mcp/audit.py`
- `src/adaos/services/root_mcp/registry.py`
- `src/adaos/services/root_mcp/service.py`
- `src/adaos/services/root_mcp/targets.py`
- `src/adaos/services/root_mcp/client.py`
- `src/adaos/apps/api/root_endpoints.py`

What is implemented in this slice:

- root MCP response envelope and error model
- root-hosted tool-contract registry over root-curated descriptor sets plus placeholder operational contracts
- root-side descriptor registry instead of a direct public `export_sdk` MCP path
- managed-target registry skeleton with `test hub` as the first published target descriptor
- `RootMcpClient` skeleton with `root_url + subnet_id + access_token + zone` configuration model
- minimal root MCP API entrypoints:
  - `GET /v1/root/mcp/foundation`
  - `GET /v1/root/mcp/contracts`
  - `GET /v1/root/mcp/targets`
  - `POST /v1/root/mcp/call`
  - `GET /v1/root/mcp/audit`
- read-only development tools for:
  - foundation summary
  - contract listing
  - descriptor-set catalog
  - descriptor-set retrieval
  - canonical system-model vocabulary
  - skill/scenario manifest schemas
- placeholder operational contract catalog for future `infra_access_skill`
- initial audit persistence to local root MCP audit storage
- audit filtering by tool, trace, target, and subnet scope

This is intentionally still a skeleton: it establishes the entrypoint, contracts, and audit shape before adding real managed-target execution.

### Phase 2. MCP-to-SDK Base

- expose machine-readable SDK descriptors
- expose skill/scenario contracts and schemas
- expose canonical vocabulary and projection-class registries
- build the first limited LLM-facing development surface

### Phase 3. Test-Hub Operational Pilot

- register `test hub` as the first managed target
- implement `infra_access_skill`
- start with read-only diagnostics and low-risk checks
- add controlled write operations only after bounded execution and audit are proven
- add web UI and operational logging for the skill

### Phase 4. Controlled Development + Operations Convergence

- add task-shaped development packets for skill/scenario authoring
- improve structured diagnostics and target summaries
- converge web and MCP surfaces around shared objects, actions, and event history
- use the foundation in iterative Codex and LLM-assisted workflows

### Phase 5. Broader Operational Architecture

- support multiple managed targets
- enrich capability classes and policy overlays
- expand operational-skill catalog beyond `infra_access_skill`
- align root MCP, `Infrascope`, and future approval/change workflows

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
- `SDK metadata exposure`: exporter support exists, but it is not yet curated and hosted as a root MCP development surface
- `scenario/skill descriptors`: manifests, registries, and projection services exist, but not yet as a unified root-level self-description catalog
- `capability model`: `src/adaos/services/policy/capabilities.py` and SDK capability errors exist, but there is no unified capability-class registry or MCP policy layer
- `web UI declarative assets`: Yjs and workspace surfaces exist, but no dedicated operational-skill web UI or audit-first operator view exists yet

### Missing

- actual `Root MCP Foundation` runtime on `root`
- `MCP Development Surface` and `MCP Operational Surface` as explicit products
- managed target registry with operational-surface publication state
- `infra_access_skill`
- typed operational tool catalog on root
- unified operational event model spanning request, policy, execution, result, and error
- operational audit trail and workflow effectiveness views
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
- `sdk_control_plane.md` and `cli/api.md`: update later when the first real root MCP skeleton and published contracts land

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
