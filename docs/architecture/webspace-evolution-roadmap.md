# Webspace Evolution Roadmap

This document fixes the current architectural understanding of AdaOS webspaces,
scenario projection, skill `webui.json`, Yjs live state, and the projection
track. It is intentionally evolutionary.

The governing rule is:

> Formalize and extract, not redesign and replace.

This roadmap does not propose:

- a full rewrite of `WebspaceScenarioRuntime`
- a replacement of the Yjs-first runtime model
- a forced redesign of existing `scenario.json` or `webui.json` contracts
- a full overlay engine on day one
- a heavy normalized database model before API semantics stabilize

Instead, it defines the target boundaries that current code should grow into
through adapters, typed metadata, and compatibility projections.

## Scope

This note covers:

- webspace identity and metadata
- `scenario.json` as base scenario-side declarative source
- skill `webui.json` as shipped skill-side contribution source
- Yjs as live collaborative state
- `ProjectionRegistry` / `ProjectionService` as a separate data-routing track
- the phased path from the current runtime to a more explicit architecture

For current implementation details see also:

- [Scenarios and Target State](../concepts/scenarios-target-state.md)
- the repository note `docs/io/webio.md`

## Four Source Model

AdaOS already behaves as if four different model sources exist. The roadmap
makes them explicit.

### 1. Webspace metadata / manifest

Responsibility:

- persistent identity of the space
- kind and composition context
- base scenario selection
- future owner / profile / device / policy hooks

Examples of fields the model should eventually support:

- `id`
- `kind`
- `home_scenario`
- `owner_scope`
- `mounts`
- `sharing`
- `policies`
- `profile_scope`
- `device_binding`

This source must not be inferred from Yjs state.

### 2. `scenario.json`

Responsibility:

- base declarative UI for a scenario
- workflow definition
- scenario-local data seeds
- base shell composition for desktop-like spaces

This is the canonical scenario-side shipped spec.

### 3. Skill `webui.json`

Responsibility:

- shipped declarative contribution spec for skill-driven UI
- skill-side UI extension into a webspace

In the current code, skill `webui.json` already expresses:

- catalog entries via `apps` and `widgets`
- registry entries via `registry.modals` and `registry.widgets`
- extension contributions via `contributions`
- bootstrap/default bindings for Yjs/data via `ydoc_defaults`

Its architectural boundaries should remain explicit:

- it does not define webspace identity
- it does not define `home_scenario`
- it does not own the shell composition of the space
- it does not replace `scenario.json` as the base source of a desktop shell

### 4. Yjs

Responsibility:

- live collaborative state
- ephemeral and operator-recoverable runtime state
- compatibility host for materialized effective UI during the current phase

Yjs remains the live shared medium. The roadmap does not replace it.

## Updated Current-to-Target Mapping

### Current mapping: webspace metadata

Current code already has a proto-manifest:

- `y_workspaces` stores `workspace_id`, `path`, `created_at`, `display_name`
- `WebspaceService` handles create, rename, delete, refresh, and seeding
- `desktop.webspace.create` already accepts `scenario_id` and `dev`

This covers:

- identity
- persistence
- lifecycle
- partial creation-time composition context

What is still missing is an explicit metadata model for:

- `kind`
- `home_scenario`
- owner / scope
- sharing / policies
- future profile awareness

### Current mapping: `scenario.json`

`scenario.json` already acts as the base declarative source for a scenario:

- `ScenarioManager` loads and normalizes `ui`, `registry`, `catalog`, and `data`
- it projects them into `ui.scenarios`, `registry.scenarios`, and `data.scenarios`
- `ScenarioWorkflowRuntime` separately materializes workflow state

This is already close to the target role of `ui_spec` plus scenario workflow.

### Current mapping: skill `webui.json`

Skill `webui.json` is already a distinct architectural layer in practice:

- `WebspaceScenarioRuntime` loads it per active skill
- `apps` and `widgets` feed the effective catalog
- `registry` contributes modals and widgets
- `contributions` drive extension-point style behavior such as auto-install
- `ydoc_defaults` seed missing Yjs paths for widgets and modals

In other words, it already behaves as `ui_contributions`, even though the
system does not yet call it that consistently.

### Current mapping: Yjs live state

Yjs currently hosts several different things:

- live collaborative state
- current scenario selection
- runtime workflow state
- materialized effective UI
- compatibility projections such as `data.webspaces`

YStore snapshots and room management already separate persisted snapshotting
from live in-memory docs. The missing step is not transport replacement, but
clear ownership of what inside Yjs is live, canonical, or derived.

### Current mapping: projection model

The projection model already exists as a separate abstraction:

- `ProjectionRegistry` maps logical `(scope, slot)` pairs to physical targets
- `ProjectionService` applies writes to `yjs`, `kv`, and later `sql`
- `scenario.yaml` and `skill.yaml` already define `data_projections`

This is target-aligned, but the current runtime still lets projection loading
leak into the UI rebuild path.

## Previously Underexplored Aspects

The first architecture pass left several important points too implicit.

### `webui.json` is not just a skill-local file

It must be treated as a first-class shipped contribution layer.

That changes the architecture in two ways:

- UI resolve must model skill-side contributions as a separate input source
- future extraction must preserve the boundary between base shell ownership
  (`scenario.json`) and skill-driven extension (`webui.json`)

### The system has four model sources, not two

The roadmap must distinguish:

- persistent webspace metadata
- scenario-side shipped spec
- skill-side shipped contribution spec
- Yjs live runtime state

Without that split, the current Yjs materializations are too easy to mistake
for canonical storage.

### `home_scenario` needs an explicit semantic contract

`home_scenario` should mean:

- the target for "go home"
- the base composition source for reseed/reset

It should not be conflated with `current_scenario`.

`current_scenario` should remain:

- a live runtime selection
- collaborative state in Yjs

For desktop spaces:

- `home_scenario` identifies the base shell to return to
- `current_scenario` may temporarily switch to another desktop scenario

For non-desktop spaces:

- `home_scenario` can initially be used only as the reseed/reset source
- shell semantics can remain deferred

For dev webspaces:

- `home_scenario` may point to the scenario currently under development
- dev mode affects source resolution and mounted skill variants

Operational policy for the current incremental implementation:

- regular workspaces keep `home_scenario` stable unless an explicit
  `set_home` / `set-home` action is requested
- dev webspaces may update `home_scenario` automatically when scenario
  switching happens without an explicit override
- `ensure_dev` is the preferred orchestration primitive for
  scenario-driven dev UX because it reuses or creates a dev webspace with a
  consistent `home_scenario`

### Dev webspace is a first-class use case

The current runtime already has separate dev semantics:

- webspace creation accepts `dev=True`
- scenario and skill loaders support `space="dev"`
- Prompt IDE workflows already assume a dev-oriented space

This means dev should become explicit metadata, not just a title convention.

Compatibility rule:

- existing `DEV:` display-name behavior can remain as a mirror during migration
- metadata becomes the canonical source of `kind` and dev-mode behavior

## Current Rebuild Status

The current runtime already contains several recovery and rebuild paths, but
they are not yet the same thing and should not be treated as interchangeable.

Implemented today:

- bootstrap seed:
  `ensure_webspace_seeded_from_scenario()` seeds an empty room from
  `scenario.json` through `ScenarioManager.sync_to_yjs*`
- operator reload/reset:
  `reload_webspace_from_scenario()` clears the live room and store, reseeds
  from the selected scenario source, refreshes compatibility listing, and then
  calls `WebspaceScenarioRuntime.rebuild_webspace_async()`
- scenario switch:
  `switch_webspace_scenario()` projects a new scenario payload into the room
  and then rebuilds effective UI plus workflow state
- snapshot restore:
  `restore_webspace_from_snapshot()` restores persisted Yjs state, but does not
  currently re-run the semantic rebuild pipeline
- client resync:
  browser-side Yjs resync reconnects transport and re-subscribes to the room,
  but it is not a semantic rebuild of the webspace model

The practical implication is important:

- `YJS Resync` can recover transport
- `desktop.webspace.reload` / `reset` can recover semantic state
- snapshot restore can recover persisted state
- none of these are yet formalized as a single authoritative rebuild contract

## Required Semantic Rebuild Contract

The target architecture needs one explicit backend-owned operation for
"rebuild this webspace from authoritative sources".

Its inputs should be:

- `WebspaceManifest`
- `manifest.home_scenario` or explicit scenario override
- scenario base spec from `scenario.json`
- skill UI contributions from `webui.json`
- projection configuration from `scenario.yaml` / `skill.yaml`
- optional live overlay/customization state

Its steps should be:

1. Resolve the authoritative rebuild source for the target webspace.
2. Invalidate scenario/skill caches as needed.
3. Reset live room and persisted Yjs store when the chosen action requires a
   clean rebuild.
4. Project base scenario data into Yjs.
5. Refresh projection registry/loading for the active scenario and skills.
6. Materialize effective UI through `WebspaceScenarioRuntime`:
   `ui.application`, `data.catalog`, `registry.merged`, compatibility mirrors.
7. Re-sync workflow/runtime state that depends on the active scenario.
8. Refresh derived compatibility data such as `data.webspaces`.
9. Emit a rebuild-complete event only after the semantic rebuild is actually
   complete.

The browser-facing rule should be explicit:

- client Yjs resync is transport recovery only
- semantic rebuild is a backend operation
- frontend resync may follow rebuild, but must not substitute for it

## Current Gaps Relative To That Contract

The current codebase is close, but still fragmented:

- bootstrap seeding relies on `scenarios.synced -> rebuild_webspace_async()`
  as an event side effect rather than an explicit rebuild pipeline
- reload/reset is the closest thing to the target contract, but it still
  mixes room reset, scenario reseed, compatibility listing refresh, and UI
  rebuild in one ad-hoc flow
- scenario switch uses a different projection path than reload/reset instead of
  reusing the same semantic rebuild primitive
- snapshot restore restores persisted Yjs state without a follow-up semantic
  reconcile step
- projection loading is still partly hidden inside rebuild logic, which makes
  recovery order hard to reason about
- frontend `YJS Resync` and `YJS Reload` can currently be confused as two
  variants of the same operation, while they solve different failure modes

### Future profile-aware personalization must be planned now

The roadmap should not implement profile-aware overlays yet, but it must avoid
blocking them.

That means:

- metadata schema should reserve fields for future owner/profile/device scope
- overlay extraction should avoid assuming that customization is forever only
  per-webspace

What is deferred:

- precedence rules between webspace/profile/device overlays
- migration of all existing customizations into a full overlay engine

### Projection is a separate architecture track

`webui.json` contributes UI.

`data_projections` route logical data.

Those responsibilities should remain separate:

- UI resolve should not own data-routing lifecycle
- projection refresh should become an explicit activation / reseed concern

## Canonical / Derived / Live / Deferred

| Artifact | Status | Storage | Notes |
| --- | --- | --- | --- |
| Webspace identity and composition context | Canonical | Persistent metadata storage | `id`, `kind`, `home_scenario`, and future scope fields belong here |
| `scenario.json` UI/workflow/data seeds | Canonical | Git / workspace / dev workspace | Base scenario-side shipped spec |
| Skill `webui.json` | Canonical | Git / workspace / dev workspace | Skill-side shipped UI contribution spec |
| `scenario.yaml` / `skill.yaml` `data_projections` | Canonical | Git / workspace / dev workspace | Separate projection-routing spec |
| Overlay record for install/pin customizations | Canonical later, deferred now | Persistent storage | Start as a thin adapter later; do not force a full engine yet |
| `ui.scenarios`, `registry.scenarios`, `data.scenarios` | Derived | Yjs | Scenario projection cache for runtime compatibility |
| `ui.application`, `data.catalog`, `registry.merged` | Derived | Yjs | Materialized effective UI for current renderer |
| `data.webspaces` | Derived | Yjs | Compatibility listing, not canonical metadata |
| `ui.current_scenario` | Live state | Yjs | Runtime selection, not base composition identity |
| Workflow runtime state | Live state | Yjs | Collaborative / runtime state |
| Device presence / awareness | Live state | Yjs | Ephemeral collaboration state |
| Full profile-aware overlay precedence | Deferred | Future persistent storage model | Do not block it, do not implement it now |
| Full SQL projection backend | Deferred | SQL | Keep contract, delay implementation |

## Storage Responsibilities

The storage split should become explicit.

### What remains in git / workspace

- `scenario.json`
- `scenario.yaml`
- skill `webui.json`
- skill `skill.yaml`
- dev variants of those same artifacts

These remain the canonical shipped specs.

### What belongs to persistent metadata storage

- webspace manifest / metadata
- later, thin persistent overlay records
- future owner/profile/device/policy hooks

This storage should hold identity and composition context, not live UI state.

### What remains in Yjs

- live collaborative state
- current scenario selection
- workflow state
- ephemeral runtime branches
- compatibility materializations consumed by the current frontend

### What should be treated as derived only

- `ui.application`
- `data.catalog`
- `registry.merged`
- `data.webspaces`
- scenario projection caches under `ui.scenarios`, `registry.scenarios`,
  `data.scenarios`

These are runtime projections, not primary sources of truth.

## Revised Phased Plan

### Phase 1. Formalize Webspace Metadata and Source Taxonomy

Goal:

- introduce an explicit `WebspaceManifest`
- make the four-source model part of the documented architecture

Touched components:

- workspace index
- `WebspaceService`
- webspace listing flows
- docs and terminology around webspace/runtime ownership

Additions:

- typed metadata model with `kind`, `home_scenario`, and a mode field for
  dev/workspace source selection
- explicit taxonomy:
  `webspace metadata` / `scenario.json` / `skill webui.json` / `Yjs`

Intentionally untouched scope:

- current `scenario.json` format
- current `webui.json` format
- current room naming
- current `WebspaceScenarioRuntime` merge logic

Key risks:

- drift between new metadata and old display-name conventions
- partial migration of existing spaces

Expected result:

- webspace identity and composition context stop depending on runtime guesses

### Phase 2. Make `home_scenario` and Dev Semantics Operational

Goal:

- define stable semantics for `home_scenario`
- make dev webspace behavior metadata-driven

Touched components:

- webspace creation
- reseed / reload / reset flows
- webspace switching semantics
- dev/workspace source resolution

Additions:

- `home_scenario` used as:
  - "go home" target
  - base reseed/reset source
- `current_scenario` remains live Yjs state
- explicit dev compatibility rule:
  metadata is canonical, `DEV:` title prefix remains a mirror
- control surfaces should expose explicit operations for:
  - `go_home`
  - `set_home`
  - `ensure_dev`
  - create/update of webspace metadata, including explicit scenario choice
- control surfaces should also expose operational introspection for:
  - `home_scenario`
  - `current_scenario`
  - `kind`
  - `source_mode`
- workspace management UI should stay reachable independently from the
  currently active scenario or home shell
- Prompt IDE and similar dev flows should be able to open a scenario-backed
  dev preview webspace in a separate browser window, instead of always
  hijacking the current shell
- scenario- and dependency-driven dev preview webspaces should refresh
  from dev sources when Prompt IDE mutates the underlying project
- CLI control surfaces should stay roughly symmetric with node HTTP
  control surfaces for create/update/home/dev operations
- default policy remains asymmetric:
  regular workspaces do not auto-persist switched scenarios as home, while
  dev webspaces may do so unless an explicit override is supplied

Intentionally untouched scope:

- shell redesign
- frontend page model
- non-desktop shell specialization

Key risks:

- legacy spaces may depend on current behavior where reset follows
  `ui.current_scenario`

Expected result:

- regular and dev webspaces gain predictable reset and return-home semantics

### Phase 3. Extract a Light Resolver Contract Around Current Runtime

Goal:

- formalize the resolver inputs and outputs without replacing
  `WebspaceScenarioRuntime`

Touched components:

- `ScenarioManager`
- `WebspaceScenarioRuntime`
- nearby DTO / adapter layer

Additions:

- explicit resolver inputs:
  - webspace metadata
  - scenario base spec
  - skill `webui.json` contributions
  - overlay snapshot
  - live Yjs state
- explicit semantic rebuild contract for:
  - bootstrap seed
  - reload/reset
  - scenario switch
  - snapshot restore reconcile
- explicit resolver outputs:
  - effective application projection
  - effective catalog projection
  - effective registry projection
- extracted runtime DTOs:
  - `WebspaceResolverInputs`
  - `WebspaceResolverOutputs`
- current `WebspaceScenarioRuntime` now follows an explicit three-step shape:
  - collect resolver inputs from Yjs + metadata
  - resolve effective materialized UI state
  - apply resolved outputs back into compatibility Yjs paths
- semantic rebuild helper now exists and is already reused by:
  - `desktop.webspace.reload`
  - `desktop.webspace.reset`
  - snapshot restore reconcile
  - scenario switch reconcile after scenario payload mutation

Intentionally untouched scope:

- current Yjs output paths
- current frontend renderer contract
- existing merge behavior unless needed for correctness

Key risks:

- abstraction drift if extraction is done without tests

Expected result:

- materialized effective UI becomes an explicit architectural layer

### Phase 4. Split Projection Lifecycle From UI Resolve

Goal:

- keep `ProjectionRegistry` / `ProjectionService` as a separate architecture
  track

Touched components:

- scenario activation
- skill activation
- scenario switch / reseed hooks
- projection registry loading

Additions:

- explicit projection refresh step on:
  - scenario activation
  - skill activation
  - scenario switch
  - webspace reseed/reset
- explicit rebuild ordering so projection refresh is a declared rebuild step,
  not a hidden side effect
- documented boundary:
  UI resolve consumes UI sources, projection lifecycle consumes
  `data_projections`

Intentionally untouched scope:

- SQL backend implementation
- replacement of Yjs or KV backends
- redesign of `ctx.*`

Key risks:

- ordering bugs between projection refresh and runtime writes

Expected result:

- UI merge and data-routing no longer depend on the same side effects

### Phase 5. Add Thin Overlay Groundwork With Future Scopes

Goal:

- begin separating persistent customizations from live state
- keep future profile-aware personalization possible

Touched components:

- desktop install/pin helpers
- reload/restore flows
- compatibility mirrors into Yjs

Additions:

- thin overlay adapter for install/pin state
- schema reserved for future user/profile/device scopes
- Yjs mirrors remain for renderer compatibility

Intentionally untouched scope:

- full overlay migration engine
- full precedence rules between webspace/profile/device
- broad personalization feature work

Key risks:

- temporary dual-write drift between overlay storage and Yjs mirrors

Expected result:

- customization state starts leaving the "everything lives in Yjs" trap
  without breaking the current frontend

## Immediate Rebuild Follow-Up

The next runtime hardening slice after the current Phase 2 work should be a
single reusable backend primitive, for example:

- `rebuild_webspace_from_sources(webspace_id, action, scenario_override=None)`

The important point is not the exact function name, but that the same backend
pipeline is reused by:

- first-room bootstrap
- `desktop.webspace.reload`
- `desktop.webspace.reset`
- scenario switch when a full semantic rebuild is required
- snapshot restore reconcile

This slice should also make the frontend contract explicit:

- `YJS Resync` means reconnect transport only
- `YJS Reload` means invoke semantic rebuild and then optionally resync

That will remove the current ambiguity where several recovery buttons appear to
do similar things while actually touching different layers of the system.

Current status:

- a first reusable primitive is now in place under the current runtime
  (`rebuild_webspace_from_sources(...)`)
- bootstrap seed is still the main remaining caller that does not yet flow
  through the same semantic rebuild entry point

## First Implementation Slice

The smallest first slice with the highest architectural payoff is:

1. Add a typed `WebspaceManifest` on top of the current workspace registry.
2. Persist `kind`, `home_scenario`, and explicit dev/workspace mode there.
3. Keep `display_name` and `DEV:` title behavior only as compatibility mirrors.
4. Change reload/reset defaults to prefer `manifest.home_scenario`.
5. Continue publishing `data.webspaces`, but document it as a derived
   compatibility projection.

Why this slice first:

- it creates the missing canonical boundary
- it clarifies how dev webspaces should behave
- it makes future resolver extraction much simpler
- it does not require replacing `WebspaceScenarioRuntime`

## Explicitly Postponed Work

The following items are intentionally postponed after the first slice:

- full overlay engine
- profile-aware precedence rules
- broad migration of all customizations
- removal of current Yjs materializations
- SQL projection implementation
- redesign of `scenario.json` or `webui.json`
- replacement of page reload on webspace switch

## Practical Guidance For Upcoming Changes

When touching this subsystem, prefer:

- typed metadata over inferred conventions
- adapters over rewrites
- compatibility mirrors over hard cutovers
- explicit source boundaries over merged responsibilities

Avoid:

- moving identity into Yjs
- letting skill `webui.json` own shell composition or `home_scenario`
- coupling projection refresh to UI rebuild as a hidden side effect
- introducing heavy persistence models before semantics are stable

This roadmap should be treated as the fixed trajectory for the next
incremental changes in webspace/runtime evolution.
