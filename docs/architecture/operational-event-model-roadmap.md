# Operational Event Model Roadmap

This roadmap is the master implementation plan for the event, projection, and browser/runtime work discussed across the control-plane documents.

It exists to prevent roadmap drift between:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)
- [Infrascope Roadmap](infrascope-roadmap.md)
- communication and webspace/runtime hardening tracks

This document is intentionally orchestration-first.
It should not duplicate the detailed target-state contracts from the source documents above.
Instead, it defines order, dependencies, milestones, and pilot strategy.

## Primary Sources

Use these documents as the authoritative sources for detailed design:

- [Operational Event Model](operational-event-model.md)
  Event taxonomy, ownership, node scope, projection lifecycle, Yjs envelope, and platform emitters.
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)
  Detailed checklist for projection ABI, client demand registration, dispatcher behavior, and migration work.
- [Infrascope Roadmap](infrascope-roadmap.md)
  Operator-workspace sequencing and the later heavy-skill migration target.
- [Webspace Scenario Pointer/Projection Roadmap](webspace-scenario-pointer-projection-roadmap.md)
  Webspace rebuild, semantic ownership, and projection/materialization evolution.
- [Realtime Reliability Roadmap](realtime-reliability-roadmap.md)
  Communication hardening and ordering constraints that must come first.

## Why This Roadmap Exists

AdaOS now has several related but distinct workstreams:

- communication hardening
- core/runtime event ownership
- skill/core interaction semantics
- demand-driven projections
- Yjs shape evolution
- browser/client projection consumption
- platform-emitted diagnostics and system messages
- heavy-skill migration such as `Infrascope`

Without a shared roadmap, these tracks can easily fork into:

- one-off skill adaptations
- duplicated Yjs conventions
- browser-only fixes that ignore core/runtime semantics
- platform diagnostics hidden inside unrelated skill payloads

This roadmap defines one implementation order across all those branches.

## Guiding Rules

- contracts before migrations
- communication guarantees before projection runtime adoption
- core/runtime ownership before browser specialization
- node-aware shared Yjs shape before heavy scenario pilots
- platform emitters before skill-specific pressure tests
- `Infrascope` as a later architectural pilot, not the starting point

## Global Ordering

The intended order across all workstreams is:

1. communication prerequisites
2. core/runtime event contract
3. node-aware Yjs envelope
4. client projection adapter and subscription runtime
5. shared dispatcher for demanded projection refresh
6. platform-emitted projections
7. heavy-skill pilots such as `Infrascope`
8. cross-skill rollout
9. cleanup and hardening

## Checklist

### Phase 0. Communication Prerequisites

- [x] `phase0.comm_order_locked`: treat communication hardening as a prerequisite for this roadmap
- [x] `phase0.node_browser_ready`: Realtime Reliability now treats browser/member semantic channels, `Yjs as SyncChannel`, and the current transport-only `/yws` handoff through sidecar local websocket ingress as complete for the current scope
- [x] `phase0.runtime_comm_ready`: hub-root Class A hardening, browser-safe supervisor transition state, routed-browser active-runtime selection, and the current transport-only `/ws` plus `/yws` sidecar handoff are now explicit and complete for the current scope
- [x] `phase0.webspace_runtime_baseline`: webspace rebuild/materialization ownership is aligned with the pointer/projection roadmap, and the browser runtime now consumes that baseline through lightweight diagnostics plus shared page-runtime adapters instead of bespoke component-only reads

Current checkpoint as of 2026-04-21:

- communication-first ordering is now explicitly locked in this roadmap and the companion projection roadmap
- browser runtime consumers now treat `infrastate`, `infrascope`, and `subnet_env` as one operational-overlay class, observing root `data` updates consistently instead of drifting per branch
- page runtime now exposes `runtime.sync`, `runtime.channels`, `runtime.materialization`, and `runtime.phase0.baseline` transforms so declarative surfaces can consume local communication/materialization prerequisites directly
- page runtime now also exposes `runtime.reliability`, `runtime.supervisor`, and `runtime.phase0.communication` transforms, so declarative browser surfaces can consume hub-root hardening, sidecar handoff, and browser-safe supervisor state without re-implementing component-local probes
- focused client tests cover the Phase 0 baseline for operational-overlay reads, observer placement, semantic communication snapshots, and runtime prerequisite snapshots
- browser and hub runtime now expose an explicit SyncChannel contract for Yjs, and the current transport-only `/yws` handoff now closes the remaining browser/member transport prerequisite for this phase
- Realtime Reliability runtime now exposes explicit Yjs ownership boundaries for `ui.current_scenario`, effective `ui/data/registry` branches, compatibility caches, and `yws` transport/session lifecycle, so Phase 0 blockers are no longer hidden behind implicit subtree semantics
- full sidecar-owned Yjs room/session continuity is now explicitly tracked as a separate deferred block in the subordinate Realtime Reliability and sidecar docs; it is not an extra hidden acceptance criterion for current Event Model `Phase 0` beyond the existing `/yws` transport ownership work
- browser header semantic diagnostics now surface `hub_root` Class A coverage, sidecar continuity, and current browser handoff state, so runtime communication evidence stays visible in the same surface that already carries sync-contract and transport-state evidence
- `GET /api/node/reliability`, `adaos node reliability`, canonical control-plane reliability projection, and browser/page runtime now share one explicit `event_model_phase0_communication` checkpoint for the tracked communication prerequisites, so Phase 0 status no longer has to be inferred independently per surface
- those same shared reliability surfaces now also carry `supervisor_runtime`, so browser-safe transition state, candidate runtime visibility, and warm-switch evidence no longer depend on per-surface local heuristics
- routed browser continuity is now also explicit through shared reliability surfaces: `event_model_phase0_communication` carries supervisor-aware active-runtime base selection for root-routed `/ws`, and browser header diagnostics surface that same `supervisor-route` checkpoint instead of hiding it inside bootstrap-only behavior
- `event_model_phase0_communication` now treats sidecar continuity as blocking only when the current runtime/media contract actually requires it, so default Phase 0 runtime communication debt no longer gets overstated beyond the current transport-only scope
- sidecar rollout policy is now also explicit through shared reliability/browser surfaces, so opt-in hub transport adoption can be audited separately from deeper post-Phase-0 continuity and session-runtime work
- `adaos-realtime` now boots dedicated local websocket listeners for `/ws` and `/yws`, root-routed browser ingress prefers those listeners for matching paths, and shared reliability/browser surfaces therefore report both handoffs as `ready` instead of only `proxy_ready`
- for Event Model `Phase 0`, the current transport-only communication prerequisites are now complete across runtime, CLI, control-plane, and browser surfaces; deeper sidecar continuity, media, and sidecar-owned Yjs session runtime remain separate follow-on work rather than hidden acceptance criteria for this phase
- webspace pointer/projection ownership remains materially aligned, and with realtime transport cutover now complete for the current scope, all four Event Model `Phase 0` checklist items are closed
- dependency reading rule for this roadmap: subordinate status still wins over local convenience adapters, but the subordinate Realtime Reliability note now marks the current transport-only prerequisite set as sufficient to close Event Model `Phase 0`

References:

- [Realtime Reliability Roadmap](realtime-reliability-roadmap.md)
- [Webspace Scenario Pointer/Projection Roadmap](webspace-scenario-pointer-projection-roadmap.md)

### Phase 1. Event Model Fixation

- [ ] `phase1.master_event_taxonomy`: freeze the shared taxonomy from the Operational Event Model
- [ ] `phase1.core_skill_contract`: define the core-skill interaction contract as a first-class runtime layer
- [ ] `phase1.platform_emitters`: define the platform as a first-class emitter of notifications, diagnostics, and system errors
- [ ] `phase1.scope_model`: freeze `per-webspace` projection scope plus reserved `node scope`
- [ ] `phase1.access_contract`: freeze MVP access metadata with `shared`, `owner`, `guest`, and `dev`

Primary source:

- [Operational Event Model](operational-event-model.md)

### Phase 2. Shared Runtime ABI

- [ ] `phase2.runtime_ownership_split`: define which invalidations are core-owned and which rebuilds are skill-owned
- [ ] `phase2.refresh_contract`: define the shared invalidation and refresh contract before browser-specific migration
- [ ] `phase2.restore_demand`: define startup restoration from Yjs demand state for core and skills
- [ ] `phase2.platform_projection_families`: define the initial platform-owned projection families

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 3. Node-Aware Yjs Shape

- [ ] `phase3.projection_record_shape`: lock the canonical projection record shape
- [ ] `phase3.client_subscription_shape`: lock the client-written subscription shape
- [ ] `phase3.node_top_level_reserved`: add a reserved node-aware top-level envelope in shared Yjs state
- [ ] `phase3.compat_layer_defined`: define compatibility rules for legacy skill/scenario JSON branches

Current checkpoint as of 2026-05-01:

- browser/platform surfaces in `web_desktop` now already propagate lightweight
  node ownership metadata for catalog items, pinned widgets, workspace labels,
  and marketplace install targeting
- compatibility-era Yjs scenario caches used by the current desktop/subnet
  migration are now node-scoped only:
  `...scenarios.<node_id>.<scenario_id>`
- this is intentionally a compatibility-first client step, not yet the final
  shared Yjs node envelope described by this phase
- node multiplicity is therefore now visible in the browser contract, but the
  backend projection record shape and reserved top-level Yjs ownership branches
  are still open work

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 4. Client Projection Runtime

- [ ] `phase4.subscription_registry`: implement browser-side projection subscription registry
- [ ] `phase4.full_overwrite_model`: make the browser write full active subscription sets
- [ ] `phase4.multi_projection_consumers`: support concurrent page, widget, modal, and panel consumers
- [ ] `phase4.node_multiplicity_visible`: prepare the client to consume node multiplicity from shared Yjs
- [ ] `phase4.lifecycle_consumption`: consume `pending/refreshing/ready/stale/error` as first-class projection state
- [ ] `phase4.cache_by_projection_key`: cache projection payloads by `projection_key`

Current checkpoint as of 2026-05-01:

- browser page runtime now supports node-aware stream receiver hints
  (`nodeId`, `transport`) in addition to the existing transport-independent
  receiver abstraction
- `WebIoStreamService` can subscribe in `auto`, `member`, or `hub` mode and
  bridge node-qualified and hub-routed stream topics
- backend router/runtime now emits those node-qualified stream topics when
  `_meta.node_id` is present, and browser transport layers propagate `node_id`
  through snapshot/subscription control events
- the desktop client now reads desktop schema and dynamic modal definitions
  only from effective runtime branches (`ui.application.*`) for the current
  subnet-migration scope, leaving scenario-specific structure in Yjs/API
  ownership rather than in client fallback logic
- this partially advances `phase4.node_multiplicity_visible`, but the general
  subscription registry and projection lifecycle ABI for all consumers are not
  complete yet

Primary source:

- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 5. Shared Dispatcher

- [ ] `phase5.dispatcher_exists`: implement one reusable dispatcher for `event -> in-memory update -> demanded projection refresh`
- [ ] `phase5.per_webspace_dispatch`: ensure dispatch runs per webspace
- [ ] `phase5.no_cross_webspace_churn`: ensure one webspace cannot force unrelated Yjs churn
- [ ] `phase5.memory_vs_yjs_boundary`: preserve the rule that runtime memory may be richer than published Yjs projections

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 6. Platform Emitters First

- [ ] `phase6.notifications_pilot`: migrate notifications through the shared projection contract
- [ ] `phase6.diagnostics_pilot`: migrate diagnostics and operator-visible failures through the shared projection contract
- [ ] `phase6.workspace_manager_pilot`: migrate shared workspace-manager and similar platform surfaces
- [ ] `phase6.emitter_validation`: validate that platform emitters exercise the architecture before one heavy skill is migrated

Current checkpoint as of 2026-05-01:

- `web_desktop` now acts as an early node-aware platform pilot:
  workspace manager surfaces show node ownership,
  home-scenario choices can surface scenarios seen across node-owned webspaces,
  desktop catalogs/widgets show node identity,
  and install requests may target a concrete node
- desktop apps/widgets ordering is now emitted through shared desktop state
  (`data.desktop.iconOrder`, `data.desktop.widgetOrder`) instead of staying in
  browser-local storage only
- this means the pilot has started, but the roadmap item should remain open
  until the same semantics are emitted through the shared dispatcher/projection
  ABI instead of compatibility-era catalog/runtime branches

Why this comes first:

- it validates the contract with platform-owned state
- it avoids coupling the first pilot to the complexity of one heavy skill
- it gives the browser a real projection consumer path before `Infrascope`

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 7. Heavy Skill Pilot

- [ ] `phase7.infrascope_gate`: do not start `Infrascope` migration before Phases 0-6 are materially in place
- [ ] `phase7.infrascope_split`: migrate `Infrascope` from monolithic snapshots to projection families
- [ ] `phase7.infrascope_platform_errors_outside_skill`: keep platform-originated diagnostics separate from skill-owned payloads
- [ ] `phase7.infrascope_access_metadata`: validate shared payload plus access metadata behavior for owner/guest/dev audiences

Primary source:

- [Infrascope Roadmap](infrascope-roadmap.md)

### Phase 8. Follow-Up Pilots

- [ ] `phase8.infrastate_followup`: align `infrastate`-style operational overlays
- [ ] `phase8.dev_scenario_followup`: choose one dev-oriented scenario such as `prompt_engineer_scenario`
- [ ] `phase8.bursty_surface_followup`: test one bursty interactive surface such as voice/media/browser-session-heavy UX

Primary source:

- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 9. Cross-Skill Rollout and Cleanup

- [ ] `phase9.monolith_inventory`: identify remaining monolithic Yjs publishers
- [ ] `phase9.shared_helpers`: provide shared helper layers for subscriptions, dispatcher use, and projection records
- [ ] `phase9.compat_cleanup`: remove legacy monolith paths once replacements are stable
- [ ] `phase9.test_matrix`: add tests for multi-webspace, multi-consumer, node-aware Yjs, platform emitters, and access metadata

## Pilot Priority

The intended pilot order is:

1. platform surfaces in `web_desktop`
2. `Infrascope`
3. `infrastate` overlays
4. one dev-oriented scenario
5. later bursty interactive surfaces

Counter-priority:

- simple low-churn skills should not be forced into the new model first

## Done When

This roadmap is successful when:

- the communication model and projection model no longer compete for ownership
- the core/skill/platform event contract is explicit and documented
- shared Yjs space is ready for node-scoped ownership
- browser clients can demand and cache multiple projections safely
- platform-owned diagnostics and system errors use the same projection runtime as skills
- `Infrascope` can migrate as an architectural pilot rather than a one-off workaround
