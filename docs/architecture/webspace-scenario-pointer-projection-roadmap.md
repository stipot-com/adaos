# Webspace Scenario Pointer/Projection Roadmap

This document fixes the target architecture and migration plan for scenario
switching in Yjs-backed webspaces.

The goal is to move from the current `materialize-and-copy` approach toward a
`pointer + resolved projection` model without breaking the current renderer,
runtime recovery flows, or operational control surfaces.

The target is not "remove rebuild". The target is to keep semantic rebuild as
the backend-owned reconcile primitive while taking it off the latency-critical
path for full scenario payload copy and evolving it toward fragmented,
phase-aware materialization.

## Status

- Current implementation: legacy `materialize-and-copy` compatibility caches
  by default, with an opt-in feature-flagged `pointer-first` switch path
  available for migration testing; effective runtime branches are now left to
  semantic rebuild in both modes
- Target implementation: `pointer + resolved projection`
- Migration mode: phased, compatibility-first

## Problem Statement

Today `switch_webspace_scenario()` still spans both:

- a runtime selection operation
- a migration-era compatibility cache materialization operation

In practice that means a single legacy scenario switch currently:

- validates the target scenario
- loads the target `scenario.json`
- writes scenario payload into Yjs compatibility branches such as
  `ui.scenarios`, `registry.scenarios`, and `data.scenarios`
- updates `ui.current_scenario`
- schedules a rebuild that computes and writes effective branches such as
  `ui.application`, `registry.merged`, and `data.catalog`

This has several costs:

- switch latency still grows with scenario payload size when pointer-first is
  disabled
- Yjs churn is still higher than necessary because compatibility caches are
  materialized on switch
- ownership boundaries are better than before, but compatibility materialization
  still keeps switch broader than the target contract
- rebuild logic remains coupled to legacy materialized scenario caches

## Target Architecture

The target model separates pointer state from resolved runtime state.

It also makes resolved runtime state explicitly phased instead of treating the
entire visible interface as a single all-or-nothing materialization unit.

### Canonical and live inputs

These are the authoritative inputs to a webspace scenario rebuild:

- `WebspaceManifest` and related persistent metadata
- `home_scenario`
- `ui.current_scenario` as the live runtime selection
- scenario content loaded from `scenario.json`
- skill UI contributions loaded from `webui.json`
- projection configuration loaded from `scenario.yaml` and `skill.yaml`
- persistent overlays and current runtime state where applicable

### Derived outputs

These are resolved runtime projections, not primary storage:

- `ui.application`
- `ui.application.structure`
- `ui.application.data`
- `data.catalog`
- `data.installed`
- `data.desktop`
- `registry.merged`

The split above is conceptual first and does not require an immediate hard
schema break. The important boundary is:

- structure/shell projection may become ready earlier
- data-bearing and enrichment-heavy branches may hydrate later
- off-focus pages, tabs, panels, or modals may remain deferred until visible

### Compatibility caches

The following branches may exist during migration, but must no longer be
treated as required source-of-truth inputs:

- `ui.scenarios`
- `registry.scenarios`
- `data.scenarios`

They become compatibility caches only.

## Governing Rules

1. Switching the active scenario should primarily update a pointer, not
   materialize a full scenario payload.
2. Effective UI and catalog state should be produced by one semantic rebuild
   pipeline.
3. Rebuild must be able to resolve a scenario directly from loader-backed
   sources without depending on materialized Yjs scenario payload.
4. Compatibility branches may remain during migration, but rebuild must not
   require them.
5. Yjs remains the live collaborative medium; this roadmap changes ownership
   and rebuild semantics, not the transport layer.
6. Scenario switch should optimize for time-to-first-structure, not only for
   time-to-fully-materialized-state.
7. The target renderer contract should permit phased readiness for staged or
   multi-page scenarios where some UI surfaces are not yet in focus.
8. "Fragmented rebuild" is the preferred end state for scenario switch: keep
   one semantic owner, but allow it to apply focused and deferred slices
   separately.

## Source Taxonomy

### Canonical

- persistent webspace metadata
- `scenario.json`
- skill `webui.json`
- `scenario.yaml` / `skill.yaml` projection declarations

### Live

- `ui.current_scenario`
- workflow/runtime state
- ephemeral collaborative state

### Derived

- `ui.application`
- structure-first compatibility projections
- deferred data/enrichment projections
- `data.catalog`
- `data.installed`
- `data.desktop`
- `registry.merged`
- optional compatibility caches under `ui.scenarios`, `registry.scenarios`,
  and `data.scenarios`

## Target Switch Contract

In the target architecture, `desktop.scenario.set` should do only the
following:

1. Validate that the requested scenario exists in the relevant source space.
2. Update `ui.current_scenario`.
3. Optionally update `home_scenario` when policy requires it.
4. Schedule or trigger semantic rebuild.
5. Return quickly with rebuild state metadata.

It should not be responsible for eagerly copying the full scenario payload into
the active runtime tree.

## Target Rebuild Contract

The semantic rebuild operation becomes the single owner of effective runtime
materialization.

Its responsibilities are:

1. Resolve source mode for the target webspace.
2. Resolve active scenario from explicit override or `ui.current_scenario`.
3. Load scenario content directly from loader-backed canonical sources.
4. Load skill UI contributions and overlay state.
5. Refresh projection rules in a declared ordering step.
6. Compute a phased resolved runtime state with explicit structure-first and
   deferred hydration slices.
7. Apply first-paint structure and critical interaction state into
   compatibility Yjs branches.
8. Apply deferred data, enrichments, and off-focus surfaces when they become
   eligible.
9. Emit rebuild status and completion signals with phase-aware metadata.

The target backend primitive is therefore closer to a cancellable reconcile
pipeline than to a single monolithic rebuild call:

1. pointer update / scenario selection
2. structure-first projection
3. interactive essentials
4. deferred hydration for background or off-focus surfaces

An internal queue may still be used for coalescing, cancellation, and stale
work suppression, but it is not the architectural center of gravity.

Transport recovery and semantic recovery should also stay distinct here:

- websocket/provider autoreconnect remains a transport concern
- frontend recovery controls may request semantic reload, but should debounce
  transient disconnects instead of recreating sync providers aggressively

## Focus and Staging Model

The target switch/rebuild path should support staged and multi-page interface
models similar to Prompt IDE-style shells:

- only the currently focused page, pane, or shell segment must block
  first-paint readiness
- inactive tabs, background pages, secondary panels, or unopened modals may be
  reconciled later
- the system should tolerate a scenario being partially ready while still
  exposing an explicit readiness contract

The preferred readiness ladder is:

- `pending_structure`
- `first_paint`
- `interactive`
- `hydrating`
- `ready`
- `degraded`

This is intentionally a contract-level model first. The exact field names may
evolve when the renderer and ABI are updated.

## Migration Strategy

The migration should preserve compatibility for existing UI/runtime consumers
until the new resolver path is stable.

### Phase A: Loader-first resolve

Rebuild learns to read scenario content from loader-backed canonical sources
first, and uses materialized Yjs scenario branches only as legacy fallback.

### Phase B: Pointer-first switch

Scenario switching stops writing full payloads into active runtime branches and
only updates the active pointer plus minimal metadata.

### Phase C: Legacy cache demotion

Legacy scenario materialization branches become optional caches and are no
longer required for normal rebuild operation.

### Phase D: Projection caching and diff apply

Resolved projection writes become more selective:

- skip unchanged writes when possible
- add top-level diff application where cheap and reliable
- cache resolver inputs and/or outputs by stable version key

Current operator-facing diagnostics now also expose temporary timing snapshots
through switch/rebuild results and rebuild status surfaces, so heavy scenarios
can be compared before larger cache/diff changes land.

Current control surfaces also expose provisional `phase_timings_ms`
(`time_to_accept`, `time_to_first_structure`, `time_to_interactive_focus`,
`time_to_full_hydration`) derived from the still-mostly-monolithic runtime.
These numbers are intentionally best-effort until fragmented reconcile lands,
but they already make pointer-first acceptance and full rebuild latency easier
to compare in production.

Scenario content/manifests and skill `webui.json` loads now also use
file-stamp-aware in-process caches, reducing repeated parse cost during
frequent rebuilds while still picking up on-disk updates without a process
restart.

Resolved projection rebuilds now also derive stable resolver fingerprints from
scenario payload, skill UI contributions, overlay snapshot, and live runtime
inputs. Those fingerprints back an in-process resolved-output cache and are
surfaced in rebuild diagnostics together with resolver source, cache-hit state,
and whether the rebuild had to fall back to legacy Yjs scenario branches.

Rebuild diagnostics now also expose an `apply_summary` for the top-level
derived branches (`ui.application`, `data.catalog`, `data.installed`,
`data.desktop`, `data.routing`, `registry.merged`) so we can distinguish
"resolver cache hit" from "apply was effectively a no-op". In parallel,
`switch_webspace_scenario()` now short-circuits repeat requests for the
already-active scenario when the current rebuild state is already `ready` for
that scenario, and also deduplicates same-target retries while a rebuild for
that scenario is already `scheduled` or `running`. Control responses now
surface both skip metadata and the current rebuild status so retries can stay
cheap without becoming opaque.

The remaining materialize-and-copy path now also performs the same lightweight
scenario existence preflight before loading `scenario.json`, so both switch
modes reject missing targets before any heavy payload copy work begins.
Legacy materialize mode now limits itself to compatibility cache writes plus
`ui.current_scenario`; active runtime branches are left to semantic rebuild, so
normal scenario switch no longer duplicates effective writes before reconcile.
Compatibility cache payloads are now also trimmed to the sections the current
runtime still uses for fallback (`ui.application`, `registry`, `catalog`)
instead of copying arbitrary scenario `data` into switch-time caches.

Reader/writer audit for migration planning:

- reader of legacy materialized scenario branches:
  `WebspaceScenarioRuntime._read_legacy_materialized_scenario_sections()`
- writers of legacy materialized scenario branches:
  `ScenarioManager._project_to_doc()` for seed/sync flows and
  `_materialize_scenario_switch_content_in_doc()` for legacy switch
- no active runtime resolver path requires materialized Yjs scenario payload
  when loader-backed content is available; legacy branches are now fallback-only

### Phase E: Structure/data split and focus-aware hydration

The rebuild contract becomes explicitly phased:

- define structure-first versus deferred branches
- formalize readiness signals for partial UI availability
- allow off-focus page/panel/modal materialization to trail first render
- keep compatibility paths while the frontend migrates away from assuming
  monolithic readiness

## Roadmap Checklist

Use this checklist as the authoritative progress tracker for the migration.

### 0. Architecture Baseline

- [x] Fix the target architecture and migration rules in a dedicated document.
- [ ] Link all relevant implementation notes and future PRs back to this
  roadmap.

### 1. Resolver Decoupling

- [x] Audit all readers of `ui.scenarios`, `registry.scenarios`, and
  `data.scenarios`.
- [x] Identify consumers that require canonical loader-backed reads instead of
  Yjs materialized payload.
- [x] Refactor resolver input collection so loader-backed scenario content is
  the primary source.
- [x] Keep materialized Yjs scenario branches as fallback only.
- [x] Add tests that prove rebuild succeeds without pre-materialized scenario
  payload in Yjs.

### 2. Switch Contract Simplification

- [x] Introduce a feature-flagged pointer-first switch path.
- [x] Make `switch_webspace_scenario()` validate existence without eager full
  payload copy.
- [ ] Restrict switch writes to `ui.current_scenario` and minimal metadata.
- [x] Preserve `set_home` / dev-webspace policy behavior.
- [x] Ensure rebuild status is observable during asynchronous switch reconcile.

### 3. Semantic Rebuild Ownership

- [ ] Ensure semantic rebuild is the only owner of effective writes to
  `ui.application`, `data.catalog`, `data.installed`, `data.desktop`, and
  `registry.merged`.
- [x] Remove duplicated writes between switch and rebuild paths.
- [ ] Keep workflow/runtime sync aligned with the rebuilt active scenario.
- [ ] Verify reload/reset/restore/switch all use the same source resolution
  rules.
- [ ] Reshape rebuild around phase-aware reconcile steps instead of a single
  monolithic "all ready at once" materialization assumption.

### 4. Compatibility and Migration Safety

- [ ] Keep compatibility caches readable during migration.
- [x] Add explicit debug signals when legacy fallback branches are used.
- [ ] Define the removal criteria for legacy scenario materialization.
- [ ] Confirm current frontend contracts still work with pointer-first switch.
- [ ] Document failure modes and rollback path if the new switch contract
  regresses runtime behavior.
- [ ] Define a compatibility contract for partially ready UI so the renderer
  can distinguish degraded state from intentional deferred hydration.

### 5. Performance Hardening

- [x] Add per-stage timing around switch, scenario load, projection refresh,
  resolve, and apply.
- [x] Add `skip-if-unchanged` checks for large derived writes.
- [x] Add cache keys for scenario content, skill UI contributions, and overlay
  snapshots where helpful.
- [ ] Add diff-apply for top-level resolved branches when the implementation is
  simple and safe.
- [ ] Measure heavy scenarios such as `infrascope` before and after each slice.
- [x] Add focused metrics for `time_to_first_structure`,
  `time_to_interactive_focus`, and `time_to_full_hydration`.
- [x] Coalesce stale background hydration work when a newer scenario switch
  supersedes it.

### 5.5. ABI and Renderer Readiness Contract

- [x] Extend `src/adaos/abi/webui.v1.schema.json` with intent-level readiness
  hints rather than scheduler-specific backend details.
- [x] Define coarse-grained load semantics for page/widget/modal/catalog
  segments such as eager, visible, interaction, and deferred.
- [x] Keep granularity above leaf-node level so the runtime does not become a
  micro-scheduler for every small control.
- [x] Separate structure-lifecycle hints from data-lifecycle hints so staged
  rendering remains tractable.
- [x] Document how staged and multi-page scenarios report off-focus readiness
  without pretending the entire shell is blocked.

### 6. Legacy Cleanup

- [ ] Stop treating `ui.scenarios`, `registry.scenarios`, and `data.scenarios`
  as mandatory runtime inputs.
- [ ] Demote those branches to optional compatibility caches.
- [ ] Remove obsolete switch-time materialize-and-copy code paths.
- [ ] Update architecture and operator docs to describe the new ownership
  model.

## Success Criteria

The migration can be considered successful when all of the following are true:

- scenario switch latency no longer scales with full payload materialization
- time-to-first-structure improves independently from time-to-full-hydration
- rebuild works from canonical loader-backed sources even when Yjs scenario
  caches are absent
- effective runtime state is written by one semantic rebuild owner
- control surfaces still expose enough state to diagnose asynchronous rebuilds
- existing frontend/runtime behavior stays compatible or has an explicit
  migration path
- staged or multi-page scenarios can render the focused surface before
  off-focus surfaces finish hydrating

## Non-Goals

This roadmap does not propose:

- replacing Yjs as the live collaborative runtime medium
- redesigning `scenario.json` or skill `webui.json`
- forcing every UI contribution to declare fine-grained scheduler details
- removing semantic rebuild as an operator and recovery primitive
- replacing the current renderer contract in one step
- forcing a single-shot rewrite of `WebspaceScenarioRuntime`

The intended path remains evolutionary:

> Move ownership first, then remove duplication, then optimize.
