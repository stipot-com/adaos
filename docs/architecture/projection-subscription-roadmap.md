# Projection Subscription Roadmap

This roadmap is the priority delivery track for moving AdaOS runtime interaction, browser-facing skills, and scenarios from monolithic Yjs snapshots and ad hoc refresh logic to demand-driven projections.

It is intentionally separate from the broader target-state architecture documents so that implementation work can start with a focused checklist.

The target architecture is defined in [Operational Event Model](operational-event-model.md).
The master implementation order across adjacent workstreams is defined in
[Operational Event Model Roadmap](operational-event-model-roadmap.md).

## Priority Rule

This roadmap now takes precedence for browser-facing projection work.

Before adding more large scenario snapshots, new widget-specific caches, or more ad hoc event debouncing, AdaOS should establish the shared projection/subscription runtime contract described here.

This contract is intended as an architectural layer above the communication model, not as a one-off adaptation for one skill.

## Goals

- make projection demand explicit
- materialize projections per webspace, not globally
- allow page, widget, modal, and panel consumers to coexist
- reduce Yjs write noise and broad client invalidation
- keep richer semantic state in skill memory while publishing only demanded views
- treat platform diagnostics, system messages, and browser/runtime errors as first-class emitted projections where appropriate
- reuse the same contract across skills and scenarios

## Non-Goals for MVP

- per-user payload forks inside the same webspace
- mandatory deletion of inactive projections
- universal generic renderer semantics for every possible UI surface
- replacing the existing domain event bus

## Checklist

### 0. Communication and Runtime Ordering

- [ ] `ordering.fixed`: place this projection/event work explicitly after node-browser and runtime communication hardening
- [ ] `ordering.runtime_first`: treat the new model as a core/skill/platform interaction contract first, and a browser materialization contract second
- [ ] `ordering.aligned_with_comm_phases`: align the implementation order with the communication phases described in the runtime reliability roadmap

### 1. Architectural Fixation

- [ ] `arch.event_model_published`: publish `Operational Event Model` as the shared target-state contract for runtime and browser projection work
- [ ] `arch.event_taxonomy_fixed`: define the canonical distinction between `domain events`, `core-skill interaction events`, `projection demand`, `projection lifecycle`, `ui intent`, and `platform operational events`
- [ ] `arch.webspace_scope_fixed`: define `projection scope` as `per-webspace`
- [ ] `arch.node_scope_reserved`: define room for `node scope` inside shared Yjs state
- [ ] `arch.audience_contract_fixed`: define the MVP access/audience metadata contract with `shared`, `owner`, `guest`, and `dev`
- [ ] `arch.shared_payload_rule_fixed`: explicitly freeze the MVP rule that owner and guest do not get separate payload branches

### 2. Core and Shared Runtime ABI

- [ ] `runtime.core_skill_contract`: define the core-to-skill invalidation and refresh contract before browser-specific consumption logic
- [ ] `runtime.ownership_split`: define which runtime transitions are core-owned and which projection rebuilds are skill-owned
- [ ] `runtime.platform_emitters_defined`: define platform-emitted projections for notifications, warnings, diagnostics, and system errors
- [ ] `runtime.restore_demand_from_yjs`: define startup restoration rules for core and skills reading active demand from Yjs

### 3. Projection ABI

- [ ] `abi.projection_record_shape`: define the canonical projection record shape: `status`, `data`, `meta`, `error`
- [ ] `abi.projection_keys_fixed`: define deterministic `projection_key` rules for page, widget, modal, panel, platform-emitted, and node-scoped projections
- [ ] `abi.client_subscription_shape`: define the browser-written client subscription record shape
- [ ] `abi.node_aware_yjs_envelope`: define the node-scoped top-level Yjs envelope so shared subnet state can preserve multiple node emitters
- [ ] `abi.pinned_consumer_semantics`: define `pinned` consumer semantics

### 4. Client Subscription Runtime

- [ ] `client.subscription_registry`: add browser-side projection subscription registry support
- [ ] `client.full_subscription_overwrite`: make each client write its full active subscription set on change
- [ ] `client.surface_lifecycle_to_subscriptions`: ensure modal open/close, widget mount/unmount, and visibility changes update the client subscription record
- [ ] `client.multi_projection_support`: add support for multiple active projections in one webspace
- [ ] `client.node_multiplicity_ready`: prepare the browser to consume node multiplicity from shared Yjs instead of assuming one anonymous node view
- [ ] `client.soft_session_sanitation`: keep stale-client cleanup as a soft client/session sanitation mechanism, not as projection activity logic

### 5. Skill, Scenario, and Platform Dispatcher

- [ ] `dispatcher.shared_pattern`: add a shared dispatcher pattern for `domain/core/platform event -> in-memory update -> demanded projection refresh`
- [ ] `dispatcher.per_webspace_refresh`: make demanded projection refresh run per webspace
- [ ] `dispatcher.no_cross_webspace_churn`: prevent one webspace from forcing writes into unrelated webspaces
- [ ] `dispatcher.memory_richer_than_yjs`: allow skills and platform services to keep richer semantic caches in memory than they publish into Yjs
- [ ] `dispatcher.lifecycle_exposed`: expose projection lifecycle transitions through the shared projection record

### 6. Yjs Granularity and Client Adapter

- [ ] `yjs.adapter_projection_records`: update the client-side Yjs adapter to read projection records instead of one giant scenario snapshot
- [ ] `yjs.cache_by_projection_key`: cache projection payloads by `projection_key`
- [ ] `yjs.reuse_cached_views`: reuse cached payloads when switching back to recently materialized views
- [ ] `yjs.reduce_broad_observers`: avoid broad `observeDeep(data)` patterns where a stable nested projection path is available
- [ ] `yjs.legacy_compat_rules`: document the compatibility rules for legacy plain-JSON projection branches during migration

### 7. Early Pilot Sequence

- [ ] `pilot.platform_surfaces_first`: prepare `web_desktop` and the shared platform surfaces first: notifications, diagnostics, workspace manager, and related modals
- [ ] `pilot.platform_emitter_validated`: validate platform-as-emitter semantics before migrating one heavy skill
- [ ] `pilot.infrascope_after_prereqs`: migrate `Infrascope` only after the core/runtime and client projection contracts are in place
- [ ] `pilot.infrastate_aligned`: align `infrastate`-style shared operational overlays with the same contract
- [ ] `pilot.dev_scenario_followup`: choose one dev-oriented scenario such as `prompt_engineer_scenario` as the first non-operator follow-up
- [ ] `pilot.simple_skills_deferred`: postpone low-churn simple skills until the core contract and adapter behavior are stable

### 8. Infrascope Migration Slice

- [ ] `infrascope.split_projection_families`: split `overview`, `inventory`, `inspector`, `topology`, and modal/widget payloads into separate projections
- [ ] `infrascope.stop_full_inspector_snapshot`: stop pre-materializing all inspectors into one Yjs snapshot
- [ ] `infrascope.demanded_only_per_webspace`: publish only the projections actively demanded by each webspace
- [ ] `infrascope.shared_payload_access_metadata`: verify that owner and guest use the same payload but can still receive different display/action treatment through access metadata
- [ ] `infrascope.platform_errors_separate`: publish platform-originated warnings and materialization errors as separate operator-facing projections instead of hiding them inside one skill snapshot

### 9. Cross-Skill Rollout

- [ ] `rollout.monolith_inventory`: identify other browser-facing skills that currently publish monolithic Yjs JSON subtrees
- [ ] `rollout.migrate_to_shared_contract`: migrate them onto the shared projection/subscription contract
- [ ] `rollout.shared_helpers`: provide a common helper layer so each skill does not reimplement subscription parsing and dispatch logic
- [ ] `rollout.manifest_rules`: document how scenario manifests and skill manifests declare projection roots without inventing incompatible shapes

### 10. Cleanup and Hardening

- [ ] `cleanup.remove_monolith_paths`: remove monolithic snapshot paths where the new projection contract fully replaces them
- [ ] `cleanup.remove_inline_debounce`: remove event-specific inline debounce logic that the dispatcher now supersedes
- [ ] `cleanup.operator_projection_diagnostics`: add operator diagnostics for active projections per webspace
- [ ] `cleanup.test_multi_webspace_and_consumers`: add tests for multi-webspace demand routing and multiple simultaneous consumers
- [ ] `cleanup.test_access_metadata_and_dev`: add tests for guest-visible access metadata and `dev` audience handling
- [ ] `cleanup.test_platform_emitters`: add tests for platform-emitted diagnostics and error projections

## Priority Candidates and Critical Assessment

The new model is best used first where all of the following are true:

- the UI has multiple independently visible surfaces
- updates are frequent or bursty
- more than one webspace may demand different projections
- the current implementation uses large plain-JSON Yjs branches

Recommended order:

1. core/runtime plus client preparation
   The architectural contract should exist before one heavy skill becomes the pilot.
2. `web_desktop` platform surfaces
   Best place to validate platform-as-emitter semantics for system messages, diagnostics, and shared browser/runtime failures.
3. `Infrascope`
   Strongest heavy-skill pressure test after the shared architecture exists.
4. `infrastate` and similar operational overlays
   Good follow-up once operator-facing demand dispatch is proven.
5. one dev-oriented scenario
   Good for testing `dev` audience behavior and more panel-heavy view switching.
6. voice/media or other bursty interactive surfaces
   Valuable after the core dispatcher and client adapter are stable.

Counter-example:

- simple low-churn skills with one small projection do not need to be forced onto this model immediately

## Acceptance Criteria

This roadmap is successful when:

- at least one complex operator scenario uses demand-driven projections instead of one monolithic snapshot
- multiple browser consumers in one webspace can demand different projections concurrently
- multiple webspaces can receive different projection refreshes from the same domain and platform event streams
- skills and platform services no longer need to publish their whole UI model into Yjs to keep the browser working
- the browser can switch back to a recently opened view without forcing a full rebuild every time
