# Webspace Scenario Pointer/Projection Roadmap

This document fixes the target architecture and migration plan for scenario
switching in Yjs-backed webspaces.

The goal is to move from the legacy `materialize-and-copy` approach toward a
`pointer + resolved projection` model without breaking the current renderer,
runtime recovery flows, or operational control surfaces.

The target is not "remove rebuild". The target is to keep semantic rebuild as
the backend-owned reconcile primitive while taking it off the latency-critical
path for full scenario payload copy and evolving it toward fragmented,
phase-aware materialization.

## Status

- Current implementation: `pointer_only` switch by default; semantic rebuild
  owns effective runtime branches, and switch-time compatibility cache writes
  have been removed from the hot path
- Target implementation: `pointer + resolved projection`
- Migration mode: phased, compatibility-first, with explicit readiness
  diagnostics for partially hydrated UI
- Event Model dependency note: for `Operational Event Model Roadmap` Phase 0,
  this track is materially aligned on webspace rebuild/materialization
  ownership; the remaining blockers sit in Realtime Reliability rather than in
  pointer/projection ownership
- Browser/platform checkpoint as of 2026-05-01:
  the current desktop client already consumes node-owned apps/widgets,
  carries `node_id` through install flows, and surfaces node identity in
  widget/workspace chrome, but these browser affordances still sit on top of
  compatibility-era catalog/runtime branches rather than a finalized
  node-aware projection ABI
- Compatibility checkpoint as of 2026-05-01:
  scenario compatibility caches are now written and read only in a
  node-scoped form under `ui.scenarios.<node_id>.<scenario_id>`,
  `registry.scenarios.<node_id>.<scenario_id>`, and
  `data.scenarios.<node_id>.<scenario_id>`.
  For the current desktop/subnet migration scope, client readers are being
  moved off direct `ui.scenarios.*` fallback access and onto effective
  runtime branches plus explicit API/stream contracts.

## Implementation Anchors

- Runtime resolver + semantic rebuild owner:
  `src/adaos/services/scenario/webspace_runtime.py`
- Scenario projection into compatibility caches:
  `src/adaos/services/scenario/manager.py`
- Bootstrap seed + rebuild nudge path:
  `src/adaos/services/yjs/bootstrap.py`
- Operator diagnostics + benchmark/reporting:
  `src/adaos/apps/api/node_api.py`,
  `src/adaos/apps/cli/commands/node.py`
- Regression coverage:
  `tests/test_webspace_phase2.py`,
  `tests/test_node_yjs_phase2.py`,
  `tests/test_yjs_doc_store.py`,
  `tests/test_push_registry_sync.py`,
  `tests/test_yjs_bootstrap.py`

Future PRs that move checklist items in sections 3, 5, or 6 should link back
to this roadmap and mention the affected checklist bullets explicitly.

## Problem Statement

The migration started from a `switch_webspace_scenario()` implementation that
spanned both:

- a runtime selection operation
- a migration-era compatibility cache materialization operation

In practice that legacy switch path used to:

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

Current control surfaces already expose this as `readiness_state`, together
with `missing_branches`, in both hub-side Yjs diagnostics and the client-side
materialization snapshot. Non-ready states before `degraded` are expected to
mean "still hydrating" rather than "hard failure".

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

For repeated operator measurements, `adaos node yjs benchmark-scenario` now
wraps target scenario switch + baseline restore loops, waits for terminal
background rebuild state by default, and aggregates `time_to_accept`,
`time_to_first_structure`, `time_to_interactive_focus`, and
`time_to_full_hydration` across multiple runs. `--detail` additionally exposes
aggregated switch/rebuild/semantic timing breakdowns and observed end-to-end
materialization milestones (`time_to_first_paint`, `time_to_interactive`,
`time_to_ready`). The benchmark now also derives a server-side ready estimate
from `phase_timings_ms.time_to_full_hydration` (falling back to rebuild /
materialization timestamps when needed) and reports the remaining control-path
observation lag as `summary.ready_observation_lag`.

To keep those measurements cheap under pressure, operator polling now also has
lightweight `/api/node/yjs/webspaces/<id>/rebuild` and
`/api/node/yjs/webspaces/<id>/materialization` surfaces, plus a matching
`adaos node yjs materialization` CLI entrypoint, so the benchmark loop no
longer needs to re-fetch the full webspace snapshot on every readiness check.
The benchmark now also reports rebuild/materialization poll counts per run and
stops materialization polling once `ready` has been observed, so the
measurement tool itself adds less load during repeated switch loops.

Rebuild status now also carries a cached `materialization` snapshot that is
updated from semantic rebuild phases (`structure`, then `interactive/ready`).
That lets hot-path operator polling reuse last-known readiness metadata without
reopening the YDoc on every request while a switch rebuild is in flight.
During pending rebuilds, the lightweight materialization endpoint may now serve
that cached snapshot directly; recent ready snapshots are also reused briefly
so benchmark loops can read the final readiness state from the same fast path.

Control and diagnostic reads that only inspect current state now also have a
read-only YDoc path, so materialization/catalog/operational probes can skip
flush/rebroadcast work on exit instead of paying the full read-write YDoc
session envelope on every poll.

Browser fallback diagnostics now also prefer those lightweight materialization
surfaces for reload/catalog recovery paths instead of fetching the full
`/api/node/yjs/webspaces/<id>` snapshot when only readiness metadata is needed.

Benchmark control resolution now also self-heals around local supervisor
prewarm/candidate transitions: when the requested local runtime endpoint stops
answering the Yjs control surface, the CLI can consult supervisor public
status, pick the currently active runtime URL, and continue the benchmark from
that control path instead of failing on the first timeout.

Current control surfaces also expose provisional `phase_timings_ms`
(`time_to_accept`, `time_to_first_structure`, `time_to_interactive_focus`,
`time_to_full_hydration`). They are now derived from measured
`structure -> interactive` semantic rebuild slices rather than from one
synthetic "full ready" number, although deferred/off-focus hydration is still
reported best-effort until later fragmented slices land.

Scenario content/manifests and skill `webui.json` loads now also use
file-stamp-aware in-process caches, reducing repeated parse cost during
frequent rebuilds while still picking up on-disk updates without a process
restart.

Resolved projection rebuilds now also derive stable resolver fingerprints from
scenario payload, skill UI contributions, overlay snapshot, and live runtime
inputs. Those fingerprints back an in-process resolved-output cache and are
surfaced in rebuild diagnostics together with resolver source, cache-hit state,
and whether the rebuild had to fall back to legacy Yjs scenario branches.

Semantic rebuild apply now also keeps lightweight fingerprints for effective
top-level branches under internal runtime metadata. When the rebuilt output for
`ui.application`, `data.catalog`, `data.installed`, `data.desktop`,
`data.routing`, or `registry.merged` matches the last applied fingerprint, the
runtime can skip reopening that heavy branch for deep equality checks. This is
an intermediate fast-path before full top-level diff-apply, and benchmark/apply
diagnostics now surface those `fingerprint_unchanged_branches` counts.

Bootstrap compatibility fallback now also stays on the incremental YStore path
when possible: if the default webspace has to seed legacy compatibility caches,
bootstrap writes a diff update over the already-open document and only falls
back to a full snapshot write if incremental persistence is unavailable.

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

Default scenario switch now uses a `pointer_only` contract: validate the
target, update `ui.current_scenario`, and let semantic rebuild own effective
branch hydration. The older feature flag
`ADAOS_WEBSPACE_POINTER_SCENARIO_SWITCH=1` remains accepted as an explicit
pointer-path compatibility alias, but pointer writes are now the normal path
instead of the opt-in experiment.

The remaining `materialize_and_copy` path has now been removed entirely.
Scenario switch no longer loads `scenario.json` or writes
`ui.scenarios`, `registry.scenarios`, or `data.scenarios` on the hot path.
The only switch-time state mutation is the scenario pointer plus minimal
manifest/home bookkeeping; semantic rebuild remains the sole owner of
effective runtime branches after reconcile.

Bootstrap fallback now follows the same ownership model. When canonical
scenario projection is unavailable during startup, bootstrap seeds only the
legacy compatibility cache branches plus `ui.current_scenario` and nudges a
normal semantic rebuild. Emergency startup no longer writes `ui.application`,
`data.catalog`, `data.installed`, `data.desktop`, or `registry.merged`
directly.

When an active live room is already attached, pointer-only switch can now also
update `ui.current_scenario` in-memory first and persist that pointer
stale-safely in the background. This removes one avoidable store-backed YDoc
open from the hot path while keeping persistence convergence explicit.

Current rollback guidance if pointer-only switch regresses runtime behavior:

- if `ui.current_scenario` flips quickly but the visible UI stays stale until a
  late rebuild or never catches up, suspect a remaining consumer that still
  depends on legacy `ui.scenarios`, `registry.scenarios`, or `data.scenarios`
- compare `phase_timings_ms`, `compatibility_caches`, and resolver diagnostics
  before changing code: `legacy_fallback_active`, `missing_branches`, and the
  rebuild/materialization state now identify whether the gap is in backend
  reconcile or in a client that still expects legacy cache timing
- treat any regression that would previously have required switch-time cache
  writes as a bug to fix in the remaining consumer path rather than a mode to
  restore permanently

Semantic rebuild target resolution is now also centralized behind a shared
backend helper instead of being partially inferred in each action path.
Current precedence is:

- scenario switch: explicit scenario from the command remains authoritative
- reload/reset: explicit override -> stored manifest home -> live current
  scenario -> `web_desktop`
- restore / generic rebuild refresh: explicit override -> live current
  scenario -> effective home scenario -> `web_desktop`

This keeps the action-specific semantics explicit while ensuring projection
refresh, rebuild status, and workflow sync all operate on the same resolved
scenario token and resolution label.

Workflow synchronization is now also aligned with semantic rebuild for
`reload`, `reset`, `restore`, and asynchronous scenario switch reconcile, so
the workflow layer no longer lags behind the rebuilt active scenario after
recovery operations.

Frontend migration coverage is now also explicit in tests and diagnostics:

- client readers prefer merged runtime branches such as
  `ui.application.desktop.pageSchema` and `ui.application.modals.*`
- for the current desktop/subnet migration scope, desktop schema and dynamic
  modal reads no longer fall back to `ui.scenarios/<current>`
- `/api/node/yjs/webspaces/<id>` now reports `materialization.readiness_state`
  and `materialization.missing_branches`, so the renderer can distinguish
  deferred hydration from truly degraded materialization
- the same snapshot now also reports
  `materialization.compatibility_caches`, including whether current client
  fallback is readable, whether switch-time compat writes are still enabled,
  whether resolver had to use legacy branches, and whether runtime-side
  removal blockers remain

Semantic rebuild itself now also applies effective branches in two ordered
phases:

- `structure`: `ui.application` and `registry.merged`
- `interactive`: `data.catalog`, `data.installed`, `data.desktop`,
  and `data.routing`

This does not yet defer off-focus payload to a separate background slice, but
it does remove the earlier "all ready at once" assumption from runtime timing
and apply summaries.

Recent `benchmark-scenario --detail` runs have now moved the immediate
optimization priority again. On the attached live-room path, current heavy
scenario measurements show:

- `time_to_accept` is already around `2 ms`
- `time_to_first_structure` is already around `25-35 ms`
- `semantic_rebuild_timings_ms.total` is already around `25-35 ms`
- `ydoc_timings_ms.ystore_apply_updates` has collapsed to `0` on the steady
  hot path, so `ydoc_timings_ms.total` is now close to the in-doc rebuild cost
- compatibility fallback is no longer required in the measured pointer-first
  path (`client_fallback=no`, `runtime_removal_ready=yes`)

That means the remaining long tail seen in some operator runs is no longer the
steady-state semantic rebuild itself. It is now more often one of:

- control-path observation lag between server-ready and the next successful
  readiness poll
- cold-room / reload / reconnect paths that still reopen or rebuild whole-room
  state under pressure
- memory churn during repeated room reloads or scenario oscillation

The next short-term performance slices should therefore focus on:

- separating server-ready from observation lag in operator tooling so
  regressions are attributable
- reducing whole-room reload/reconnect churn and memory spikes
- continuing YStore replay/open work for detached recovery paths rather than
  for the already-hot attached switch loop

The cheaper writeback path and live-room reuse are still important because they
made this shift visible: normal YDoc session flush now persists the incremental
diff it just produced, attached read/control/rebuild flows can reuse the
in-memory live room instead of replaying the store again, and cold room
creation now seeds the new live room in one pass instead of paying a duplicate
bootstrap replay. The remaining store work is therefore increasingly
concentrated in detached cold reopen and recovery cases, not in the main
pointer-first switch benchmark.
- the remaining replay bottleneck becomes more clearly a cold-room /
  reconnect / reload path problem

YStore replay pressure is now also bounded by bytes, not only by entry count.
The bounded replay window can compact when either the update count or the
byte-size tail grows too large. Runtime diagnostics now surface:

- `replay_window_bytes`
- `replay_window_byte_limit`
- `base_snapshot_present`
- `last_compact_reason`
- `auto_backup_total`
- `last_auto_backup_reason`

The byte budget is controlled by `ADAOS_YSTORE_MAX_REPLAY_BYTES` and should
help repeated switch/rebuild loops converge toward smaller cold-room replay
windows over time, even when many small diff updates accumulate.

To reduce repeated cold-open cost after replay pressure, YStore can now also
schedule a debounced auto-backup after pressure compaction. That backup writes
an up-to-date snapshot file and, when no newer writes raced ahead, collapses
the in-memory log to a single base snapshot (`backup_compaction`). The knobs
are:

- `ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT`
- `ADAOS_YSTORE_AUTOBACKUP_COOLDOWN_SEC`
- `ADAOS_YSTORE_AUTOBACKUP_DEBOUNCE_SEC`

Operator recovery paths were also tightened so performance work is not masked
by recovery fan-out:

- `desktop.webspace.reload` now keeps the live room attached and refreshes the
  scenario projection in place
- only `desktop.webspace.reset` performs destructive room/store reset
- reload/reset no longer intentionally trigger both `scenarios.synced`
  side-effect rebuild and an explicit rebuild for the same operator action

Cold-open room bootstrap now also stays on a single YDoc pass: room creation
seeds the new live `YRoom.ydoc` directly instead of replaying YStore into a
temporary bootstrap doc and then replaying the same updates again into the
fresh room. Gateway/reliability diagnostics now distinguish:

- total cold opens versus room reuses
- single-pass bootstrap count for new rooms
- last cold-open envelope timing (`apply_updates` and overall bootstrap/open)

That means the remaining reopen cost is more clearly concentrated in true
detached recovery paths after a room/store drop, rather than in routine room
creation for an otherwise healthy runtime.

Reader/writer audit for migration planning:

- reader of legacy materialized scenario branches:
  `WebspaceScenarioRuntime._read_legacy_materialized_scenario_sections()`
- writers of legacy materialized scenario branches:
  `ScenarioManager._project_to_doc()` for seed/sync flows
- no active runtime resolver path requires materialized Yjs scenario payload
  when loader-backed content is available; legacy branches are now fallback-only
- `ScenarioManager._project_to_doc()` now skips runtime-owned top-level data
  keys (`catalog`, `installed`, `desktop`, `routing`) so scenario sync no
  longer overwrites effective semantic-rebuild branches through `data.*`

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
- [x] Link all relevant implementation notes and future PRs back to this
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

- [x] Introduce a fast pointer-based switch path.
- [x] Make `switch_webspace_scenario()` validate existence without eager full
  payload copy.
- [x] Restrict switch writes to `ui.current_scenario` and minimal metadata.
- [x] Preserve `set_home` / dev-webspace policy behavior.
- [x] Ensure rebuild status is observable during asynchronous switch reconcile.

### 3. Semantic Rebuild Ownership

- [x] Ensure semantic rebuild is the only owner of effective writes to
  `ui.application`, `data.catalog`, `data.installed`, `data.desktop`, and
  `registry.merged`.
- [x] Remove duplicated writes between switch and rebuild paths.
- [x] Keep workflow/runtime sync aligned with the rebuilt active scenario.
- [x] Verify reload/reset/restore/switch all use the same source resolution
  rules.
- [x] Reshape rebuild around phase-aware reconcile steps instead of a single
  monolithic "all ready at once" materialization assumption.

### 4. Compatibility and Migration Safety

- [x] Keep compatibility caches readable during migration.
- [x] Add explicit debug signals when legacy fallback branches are used.
- [x] Define the removal criteria for legacy scenario materialization.
- [x] Confirm current frontend contracts still work with pointer-only switch.
- [x] Document failure modes and rollback path if the new switch contract
  regresses runtime behavior.
- [x] Define a compatibility contract for partially ready UI so the renderer
  can distinguish degraded state from intentional deferred hydration.

### 5. Performance Hardening

- [x] Add per-stage timing around switch, scenario load, projection refresh,
  resolve, and apply.
- [x] Add read-only YDoc access paths for diagnostic/control reads that do not
  need writeback.
- [x] Expose YDoc session-envelope timings in rebuild and benchmark detail
  output.
- [x] Add `skip-if-unchanged` checks for large derived writes.
- [x] Add cache keys for scenario content, skill UI contributions, and overlay
  snapshots where helpful.
- [x] Use live-room fast paths for `ui.current_scenario` reads/writes when an
  active room is already attached.
- [x] Split operator `reload` from destructive `reset` so reload stays on the
  live room and does not fan out into duplicate semantic rebuilds.
- [x] Reduce `YStore.apply_updates` / `encode_state_as_update` envelope cost
  now that pointer-switch latency is no longer the dominant bottleneck.
  Current slice: diff-writeback replaced the full-state flush on normal YDoc
  exit, attached live-room rebuild/read paths now bypass repeated replay, and
  bounded replay now also compacts by byte budget (`ADAOS_YSTORE_MAX_REPLAY_BYTES`).
  Backup flows now also collapse the in-memory log to a single base snapshot,
  pressure compaction can trigger a debounced auto-backup so later cold-room
  opens have a shorter replay path, and bootstrap compatibility fallback now
  prefers incremental diff writes over snapshot rewrites. Backup/restore now
  also reuse an already-compacted runtime base snapshot when possible and skip
  redundant backup writes when the persisted generation is already current.
  Room reset/reload now also releases dropped YRoom references aggressively and
  can request an idle runtime compaction pass so the old replay tail is
  collapsed after the room is torn down. Cold-room bootstrap now seeds the live
  `YRoom.ydoc` in one pass, detached restore/reopen paths cache the compacted
  snapshot state vector so later `async_get_ydoc()` / `get_ydoc()` sessions can
  skip one extra `encode_state_vector` pass on entry, and effective-branch
  fingerprint bookkeeping now rides inside the existing rebuild phase
  transactions instead of paying an extra post-apply YDoc write envelope.
  Reliability/CLI snapshots now also surface reconnect-storm pressure, room
  counts, replay-byte pressure, and detached state-vector fast-path reuse more
  explicitly.
- [x] Add diff-apply for top-level resolved branches when the implementation is
  simple and safe.
  Current slice: dict-shaped top-level effective branches now lazily migrate to
  attached `YMap` storage the first time they need a semantic write, after
  which rebuild applies recurse through mapping subtrees instead of replacing
  the full top-level branch payload on every change. Lists intentionally remain
  atomic leaves for now, which keeps the implementation on a safe `y_py` path
  while still eliminating most avoidable full-branch rewrites for
  `ui.application`, `data.desktop`, `data.routing`, `data.installed`,
  `data.catalog`, and `registry.merged`.
- [x] Measure heavy scenarios such as `infrascope` before and after each slice.
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

- [x] Stop treating `ui.scenarios`, `registry.scenarios`, and `data.scenarios`
  as mandatory runtime inputs.
- [x] Demote those branches to optional compatibility caches.
- [x] Remove obsolete switch-time materialize-and-copy code paths.
- [x] Update architecture and operator docs to describe the new ownership
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
