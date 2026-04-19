# Root MCP Roadmap

This roadmap tracks sequencing for [Root MCP Foundation](root-mcp-foundation.md).

`Root MCP Foundation` defines the stable root-hosted governance and routing layer.
This roadmap tracks how that foundation should evolve into descriptive and operational product surfaces.

## Sequencing Principles

1. Foundation before product surfaces.
   Auth, policy, audit, routing, and session resolution should stabilize before rapid plane growth.
2. Descriptive and operational work should diverge early.
   Cached descriptors and live target operations have different freshness and authority requirements.
3. Plane evolution should not force kernel churn.
   New MCP surfaces should land primarily as planes over the foundation.
4. External-client constraints should be solved with root-issued session context.
   Do not weaken routing semantics just because a client cannot provide session parameters.
5. CI/CD should become the freshness engine for descriptive context.
   Pseudo-static MCP knowledge should be built and published, not scraped ad hoc at runtime.

## Companion Slices

The roadmap has two explicit companion slices:

- `AdaOSDevPlane`
  - descriptive and authoring-oriented MCP surface for SDK, manifests, templates, and architecture
- `ProfileOps`
  - operational MCP surface for supervisor-owned profiling and profiling evidence

## Phase 0. Architectural Fixation

- [x] fix root-first MCP terminology and boundaries
- [x] split foundation from plane evolution
- [x] define `Root Descriptor Cache`
- [x] define `MCP Session Lease`
- [x] define first named plane concepts such as `AdaOSDevPlane` and `ProfileOpsPlane`

Phase is complete when:

- architecture docs separate foundation from planes
- descriptive caching is documented as a first-class root concern
- external MCP access is documented through root-issued session leases rather than ad hoc client parameters

## Phase 1. Root MCP Foundation Skeleton

- [x] add minimal root MCP entrypoint and request/response envelopes
- [x] add root-hosted tool contract registry
- [x] add initial audit primitives and event IDs
- [x] expose only minimal read-oriented descriptors and placeholder operational contracts
- [x] keep planes implicit at first, but avoid baking every projection directly into the foundation contract

### Current Implementation Slice

The current code checkpoint establishes the first root-hosted skeleton under:

- `src/adaos/services/root_mcp/model.py`
- `src/adaos/services/root_mcp/audit.py`
- `src/adaos/services/root_mcp/registry.py`
- `src/adaos/services/root_mcp/service.py`
- `src/adaos/services/root_mcp/targets.py`
- `src/adaos/services/root_mcp/client.py`
- `src/adaos/services/root_mcp/codex_bridge.py`
- `src/adaos/apps/api/root_endpoints.py`
- `src/adaos/apps/cli/commands/dev.py`

What is implemented in this slice:

- root MCP response envelope and error model
- root-hosted tool-contract registry over root-curated descriptor sets plus placeholder operational contracts
- root-side descriptor registry instead of a direct public `export_sdk` MCP path
- state-backed managed-target registry skeleton with `test hub` as the first published target descriptor
- report-backed managed-target refresh through hub control-lifecycle reports
- `RootMcpClient` skeleton with `root_url + subnet_id + access_token + zone` configuration model
- initial root-side capability registry and policy gate for direct endpoints and MCP tool execution
- bounded root-issued MCP access-token primitives for external clients
- initial audit persistence, filtering, and scoped MCP auth
- local `stdio` Codex bridge for VS Code and Codex CLI using a workspace-local profile plus token file

This is still an early skeleton.
It proves the first `hub -> root -> Root MCP` operational loop and the first end-to-end external-client workflow through a local bridge.

## Phase 2. Root Descriptor Cache and Descriptor Build Pipeline

- [x] define descriptor bundle formats and provenance fields
- [x] promote current `build SDK` work into a descriptor-build pipeline prototype
- [x] publish architecture, SDK, schema, manifest, and template descriptors through root cache
- [x] add CI/CD and publish hooks so public skills and scenarios refresh root descriptors as part of lifecycle
- [x] expose freshness metadata and TTL semantics in descriptor responses

Phase is complete when:

- descriptive MCP reads can be served from root cache without contacting a hub
- pseudo-static descriptors have build provenance and freshness metadata
- public skill and scenario publication updates the descriptive registry path

## Phase 3. AdaOSDevPlane

- [x] define the first explicit descriptive plane over the foundation
- [x] expose architecture, SDK, manifest, schema, template, and public registry descriptors through typed plane contracts
- [x] make `AdaOSDevPlane` the preferred LLM-programmer descriptive surface
- [x] keep descriptive responses root-curated and cache-backed by default

Phase is complete when:

- LLM-oriented descriptive context no longer depends on live target reachability
- descriptive MCP surface is plane-scoped rather than hard-coded as one monolithic root API

## Phase 4. Test-Hub Operational Pilot

- [x] register `test hub` as the first managed target
- [x] implement `infra_access_skill`
- [x] start with read-only diagnostics and low-risk checks
- [x] add controlled write operations only after bounded execution and audit are proven
- [x] add web UI and operational logging for the skill

Phase is complete when:

- root-routed operational reads and bounded writes are governed by published target capabilities
- local-pilot execution remains bounded and auditable

## Phase 5. MCP Session Lease

- [x] introduce root-side `MCP Session Lease` issuance
- [x] allow operator selection of `target`, `ttl`, and named capability profile
- [x] bind `subnet_id`, `zone`, target scope, and effective capabilities to the stored lease
- [x] make root the source of truth for lease deadline, last use, and usage count
- [x] expose list/revoke flows for open sessions through skill-facing operational surfaces

Phase is complete when:

- external MCP clients can operate with `url + bearer` without passing `subnet_id` as a session parameter
- `infra_access_skill` can show open sessions with target, deadline, last-used time, and usage count

## Phase 6. Plane Registration and Multiple Operational Planes

- [ ] formalize plane registration contracts
- [ ] let multiple operational planes coexist over the same foundation
- [ ] keep capability profiles and tool catalogs plane-scoped where appropriate
- [ ] avoid coupling feature-specific product surfaces directly to the foundation implementation

## Functional Maturity Milestones

### Milestone A. VS Code Codex Local Bridge Trial

- [x] root-hosted MCP foundation can be reached through local `stdio` bridge
- [x] Codex can inspect managed target status and operational surface through the bridge
- [ ] Codex can use named session/capability bootstrap instead of only workspace-local profile/token preparation

This milestone is the earliest point for repeated live trials with `Codex in VS Code` through the current local bridge model.

### Milestone B. VS Code Codex Session-Lease Trial

- [x] root can issue `MCP Session Lease` objects for a chosen target and capability profile
- [x] bearer-only access resolves subnet/zone/target context from the lease itself
- [x] session list/revoke UX is available for operators through `infra_access_skill`
- [x] Codex bootstrap no longer depends on explicit `subnet_id` transport parameters

This milestone is the first clean live-aprroval point for `Codex in VS Code` with target-scoped bearer access that matches MCP client limitations.

### Milestone C. Descriptive Codex Trial

- [x] root descriptor cache serves architecture and SDK descriptors without contacting a hub
- [x] `AdaOSDevPlane` exposes stable descriptive contracts for LLM-programmer workflows
- [x] public skill/scenario publication refreshes the descriptive registry path

This milestone is the first point where live Codex trials should cover AdaOS architecture/programming assistance rather than only operational inspection.

### Milestone D. ProfileOps Live Trial

- [x] `ProfileOpsRead` is available through MCP
- [x] `ProfileOpsControl` is available through bounded typed writes
- [x] profiler reads/writes appear in shared Root MCP audit history
- [ ] Infrascope and Codex consume the same profiler contracts

This milestone is the first live approval point for real supervisor-profiler MCP workflows.

Phase is complete when:

- the system can support multiple independently evolving MCP planes without duplicating auth, audit, or routing logic

## `ProfileOps` Roadmap

`ProfileOps` should run as an explicit operational plane over the foundation.
Its purpose is to turn supervisor-owned profiling into a first-class typed operational surface.

### `ProfileOps-0`. Architecture Fixation

- [x] fix the target-state statement that profiler authority remains with supervisor
- [x] define `ProfileOps` as the MCP projection layer over supervisor profiling
- [x] define the first profiler tool ids and capability vocabulary
- [x] define the split between root-published profiling evidence and target-routed profiling controls

Phase is complete when:

- architecture docs no longer imply that root report endpoints alone are equivalent to MCP integration
- profiler reads and writes have named MCP capability families

### `ProfileOps-1`. Read-Only MCP Projection

- [x] add profiler tool contracts to Root MCP for status, sessions, incidents, artifact catalogs, and artifact retrieval
- [x] project already-published root profiling summaries through typed MCP responses
- [x] expose the read surface through `RootMcpClient`
- [x] expose the same read surface through the local Codex `stdio` bridge
- [x] package read capabilities as a named profile such as `ProfileOpsRead`

Phase is complete when:

- an MCP client can inspect profiling status and published sessions without bespoke knowledge of `/v1/hubs/memory_profile/*`
- profiler reads participate in standard Root MCP policy and audit paths

### `ProfileOps-2`. Target-Routed Profiling Controls

- [x] add typed MCP write tools for `start`, `stop`, `retry`, and `publish`
- [x] require explicit target capability publication before any write tool is allowed
- [x] keep execution bounded and environment-scoped in the same style as existing `hub.*` write tools
- [x] preserve supervisor as the only authority that decides profile mode convergence and session lifecycle
- [x] package control capabilities as a named profile such as `ProfileOpsControl`

Phase is complete when:

- MCP-triggered profiling controls are equivalent in governance to existing bounded operational tools
- no profiler write path bypasses Root MCP policy and audit

### `ProfileOps-3`. Unified Audit and Event History

- [x] align profiler tool execution with the Root MCP operational event model
- [x] link profiling control actions, root publication, and artifact retrieval through common trace ids where practical
- [x] let activity feeds and capability-usage views include profiler operations without a second audit vocabulary

Phase is complete when:

- profiler actions and reads appear as first-class operational history rather than as isolated report events

### `ProfileOps-4`. Human/Agent Surface Convergence

- [ ] let Infrascope consume the same typed profiler tools for remote inspection
- [ ] use the same contracts for Codex, operator UI, and later approval-aware workflows
- [ ] keep direct report endpoints as substrate and compatibility paths, not as the primary product surface

Phase is complete when:

- profiling has one typed operational surface with multiple consumers
- web and agent clients no longer need separate profiler integration logic
