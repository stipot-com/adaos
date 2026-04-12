# AdaOS Supervisor

## Goal

Introduce a small always-on local supervisor process that remains available while the main AdaOS runtime restarts, updates, validates a new slot, or rolls back.

`adaos-supervisor` is not a transport sidecar.
It is the local process-lifecycle and update-orchestration authority for one node.

Read this together with:

- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Transport Ownership](transport-ownership.md)
- [AdaOS Realtime Sidecar](adaos-realtime-sidecar.md)
- [Hub-Root Protocol](hub-root-protocol.md)

## Why this exists

The current autostart/update flow couples:

- local admin API availability
- runtime process lifetime
- update status visibility
- slot validation and rollback logic

This creates an operator gap during restart-heavy paths:

- `update-status` becomes unavailable exactly when update progress matters most
- `node reliability` collapses into connection-refused even when the system is in a known transitional state
- stale `restarting` or `applying` state can survive longer than intended

The supervisor solves this by keeping the update and process-control surface alive while the runtime is stopped or being replaced.

## Scope

`adaos-supervisor` owns:

- local runtime process lifecycle
- runtime start/stop/restart sequencing
- persisted update attempt state
- candidate-to-slot prepare / validate / rollback orchestration
- post-validation bootstrap/root promotion orchestration when bootstrap-managed files changed
- skill runtime migration orchestration for installed skills during core slot transition, including deferred commit after old runtime shutdown when early slot preparation is used
- restart and validation deadlines
- local admin/update API availability during runtime downtime
- recovery from interrupted update attempts

`adaos-supervisor` does not own:

- hub-root protocol semantics
- root-issued trust or cross-subnet truth
- business semantics of skills, scenarios, or local event handling
- realtime transport authority
- browser/member semantic channel policy

## Target runtime split

Target local node layout:

- `adaos-supervisor`
  - always-on local control and update authority
  - owns persisted attempt state and process supervision
- `adaos-runtime`
  - main FastAPI/runtime process
  - production runtime is launched from the active slot manifest
  - owns local execution semantics, storage, skills, scenarios, and APIs
- `adaos-realtime`
  - optional transport-only sidecar
  - owns selected long-lived transport loops such as hub-root realtime transport

This means:

- the runtime is restartable without losing the local control surface
- the realtime sidecar can also be restarted independently
- update progress remains inspectable during shutdown, apply, and validate phases
- root checkout stays out of the production runtime path unless a developer explicitly launches it

## Runtime source rule

Production runtime must always come from slot `A|B`.

Root checkout is reserved for:

- bootstrap install
- supervisor/autostart/update-control code
- candidate preparation
- explicit developer-run workflows

This keeps slot switching fast and keeps production runtime independent from root checkout drift.

For runtime processes, slot resolution is process-local first:

- if `ADAOS_ACTIVE_CORE_SLOT` is set in the runtime environment, that process treats the slot as its effective runtime source
- otherwise the global slot marker from `state/core_slots/active` remains authoritative

This is important for warm-switch work because it allows a `candidate` runtime to boot from the inactive slot for prewarm/diagnostics without mutating the global active-slot marker before cutover is committed.

## Slot-bound runtime ports

Supervisor should treat runtime ports as slot-owned rather than as one global mutable bind.

Target rule:

- slot `A` keeps a stable local runtime port
- slot `B` keeps a different stable local runtime port
- supervisor stays on its own always-on control port

Current MVP direction is:

- default `A` runtime port: `8777`
- default `B` runtime port: `8778`
- supervisor port: `8776`

This creates a clean foundation for warm-switch behavior because the inactive slot can be prepared and, later, prewarmed without fighting for the active slot's listener.

The mapping must remain supervisor-visible in diagnostics and browser-safe transition payloads so local clients can discover:

- active runtime URL
- candidate runtime URL
- current transition mode
- whether warm-switch was admitted or downgraded to stop-and-switch

## Warm-switch admission

Warm-switch is desirable, but not always safe on constrained hardware.

Supervisor should therefore make an explicit admission decision before using a dual-runtime transition:

- if candidate slot uses a different reserved port and memory headroom is sufficient, transition mode is `warm_switch`
- if memory headroom is not sufficient, transition mode is `stop_and_switch`
- this decision must be visible to operator and browser-facing status surfaces before shutdown starts

Admission should be driven by a simple local resource gate such as:

- available memory
- current runtime RSS
- estimated candidate runtime footprint
- configured reserve that must remain free after candidate start

The important rule is that low-memory devices must fail safe into stop-and-switch instead of trying to start two full runtimes and getting stuck mid-transition.

## Runtime instance identity

Warm-switch means `active` and `candidate` may overlap for a short period.

That requires identity stronger than just `hub_id` or `subnet_id`.

Each runtime process should therefore carry:

- `runtime_instance_id`
- `transition_role` (`active` or `candidate`)
- slot-bound runtime URL/port

This identity must flow through:

- supervisor runtime status
- browser-safe transition status
- root control lifecycle reports
- root core-update reports
- hub root/NATS session issuance and logs

Without that, a candidate process can look indistinguishable from the active process and accidentally steal or overwrite control-plane state.

## Candidate passive mode

Before fast cutover is implemented, a prewarmed `candidate` runtime must stay passive on root-facing traffic subjects.

That means a candidate may establish root control connectivity for diagnostics, but it must not yet subscribe to the same root-routed traffic subjects as the active runtime:

- `tg.input.<hub_id>`
- `io.tg.in.<hub_id>.text`
- `route.v2.to_hub.<hub_id>.*`

The intent is:

- root can see that a candidate runtime exists
- active runtime remains the only consumer of live hub traffic before cutover
- candidate reconnects or retries do not supersede the active runtime's root/NATS session merely because they share the same `hub_id`

The same rule must apply to local control discovery:

- `candidate` runtime surfaces must self-identify through lightweight probes such as `/api/ping` and `/api/admin/update/status`
- local fallback control resolvers must ignore a runtime that reports `transition_role=candidate` or `admin_mutation_allowed=false`
- a candidate runtime must reject mutating local update operations (`update.start`, `update.cancel`, `update.rollback`) with an explicit conflict instead of behaving like a second control plane

## Authority boundary

### Supervisor

The supervisor is the authority for:

- whether the runtime is expected to be running
- whether an update attempt is pending, active, failed, validated, or rolled back
- whether a rollback must be triggered after deadline expiry or failed validation
- operator-visible local state of runtime lifecycle

The supervisor is not the authority for:

- whether root-side protocol acks were accepted as global truth
- whether transport-level path selection is healthy beyond its delegated runtimes
- business-policy decisions inside degraded hub execution

### Runtime

The runtime remains the authority for:

- local API semantics
- local scenario and skill execution
- local persistence and event bus behavior
- local degraded-mode execution once it is running

### Realtime sidecar

The realtime sidecar remains the authority only for:

- transport lifecycle
- reconnect loops
- socket diagnostics
- local relay IO

It must not absorb supervisor responsibilities.

## Local control surfaces

The target local APIs are:

### Supervisor API

Always available while the node is booted:

- `GET /api/supervisor/status`
- `GET /api/supervisor/update/status`
- `POST /api/supervisor/update/start`
- `POST /api/supervisor/update/cancel`
- `POST /api/supervisor/update/defer`
- `POST /api/supervisor/update/rollback`
- `POST /api/supervisor/runtime/restart`
- `POST /api/supervisor/runtime/candidate/start`
- `POST /api/supervisor/runtime/candidate/stop`

This API is the source of truth for:

- update attempt state
- restart reason
- validation deadlines
- rollback decisions
- current managed child processes
- active and candidate runtime process identity/state (`runtime_instance_id`, `transition_role`, slot, port, readiness)
- current skill runtime migration diagnostics for the active core update attempt
- runtime liveness separate from listener bind and runtime API readiness
- active managed runtime command/executable source for the current slot
- active slot structure diagnostics (`manifest` / `repo` / `venv` / nested-slot anomalies)

For browser-facing observability, supervisor should also expose a limited read-only transition surface that can be polled without admin-mutating privileges.
That surface is intended only for restart/update visibility such as:

- `hub restarting`
- `update planned`
- `update applying`
- `rollback in progress`
- `root promotion pending`
- `root restart in progress`
- `subsequent transition queued`
- `update failed`

It must not expose mutating control operations or become a substitute for the authenticated operator API.

Current MVP browser behavior may preserve and display the last known transition state during reconnect windows, and routed hub sessions can now poll a live browser-safe supervisor view at `/hubs/<id>/api/supervisor/public/update-status`.
The target end state is stronger: every supported browser entry topology should be able to poll that read-only supervisor transition surface directly, so the shell can keep moving from `hub restarting` to `rollback in progress` or `root promotion pending` from supervisor truth rather than only from the last runtime-visible snapshot.
Operator-facing surfaces are also expected to consume that same supervisor truth through the canonical control-plane model, so Infrascope and related overview projections can show core-runtime transition state in `active_runtimes`, health strips, and recent changes instead of presenting a restart only as generic hub instability.
That browser-safe surface now also includes candidate runtime diagnostics needed for warm-switch work:

- `candidate_runtime_instance_id`
- `candidate_runtime_state`
- `candidate_runtime_api_ready`
- `candidate_transition_role`

### Runtime API

Available only while `adaos-runtime` is running:

- current node APIs
- current admin APIs that belong to runtime semantics
- reliability, scenario, skill, Yjs, media, and operator surfaces

## Persisted state

The supervisor should persist explicit local attempt state, separate from transient runtime liveness:

- `state/supervisor/runtime.json`
- `state/supervisor/update_attempt.json`
- `state/supervisor/last_result.json`

Recommended fields for `update_attempt.json`:

- `attempt_id`
- `action`
- `state`
- `phase`
- `target_slot`
- `target_rev`
- `target_version`
- `reason`
- `started_at`
- `deadline_at`
- `validated_at`
- `restored_slot`
- `failure_summary`
- `skill_runtime_migration`
- `scheduled_for`
- `planned_reason`
- `subsequent_transition`
- `subsequent_transition_requested_at`
- `subsequent_transition_request`

The important rule is that this state is committed by the supervisor, not inferred only from whether the runtime currently listens on `127.0.0.1:8777`.

## Update flow

Target flow:

1. operator or root-triggered action reaches supervisor
2. supervisor writes `update_attempt.json`
3. supervisor materializes the candidate source/artifact
4. supervisor prepares the inactive slot from that candidate while the active runtime is still serving traffic
5. supervisor starts countdown only after the target slot is materially ready
6. supervisor requests graceful runtime shutdown
7. supervisor commits deferred installed-skill runtime migration against the target core interpreter after the old runtime is down
8. supervisor launches production runtime from the target slot
9. supervisor validates required runtime checks against that slot runtime
10. on slot-validation success, supervisor commits the slot switch
11. if bootstrap-managed files changed, supervisor records `root_promotion_required` and promotes root from the same validated candidate
12. on failure or deadline expiry, supervisor rolls back the slot and records failure

Important invariants:

- the attempt record is not cleared before validation succeeds
- `restarting` and `applying` are bounded by deadlines
- interrupted supervisor boot resumes or resolves the last incomplete attempt
- if a new update signal arrives during an active transition, supervisor records exactly one deferred `subsequent_transition` and executes it once after the current transition reaches a terminal state
- minimum update interval gating schedules a future update window instead of rejecting the request outright
- installed skills do not silently inherit old runtime dependencies after core migration
- root/bootstrap promotion never happens before the candidate already passed slot validation
- root promotion must preserve any already-queued subsequent transition metadata so a self-update handoff does not lose the next requested transition
- prepared slot contents must not inherit another slot's git remotes or become the authority for future updates

## Bootstrap/root promotion

Bootstrap-managed code such as supervisor, autostart, and core-update orchestration is a separate promotion step.

Rules:

- slot validation always happens first
- root promotion is allowed only after the candidate is proven in a slot
- production runtime still restarts from the active slot after root promotion
- root promotion should use the same validated candidate source, not a fresh mutable branch tip
- current MVP implementation promotes bootstrap-managed files into root with a backup snapshot, records an explicit supervisor attempt state while waiting for that restart, and requires an explicit service restart to activate the new supervisor/bootstrap code
- if another transition request arrives before that restart completes, it is queued as `subsequent_transition` on the supervisor attempt instead of being dropped or run concurrently

This keeps root updates out of the fast rollback path while preserving the slot-runtime model.

## Skill runtime migration lifecycle

Installed skills are not automatically valid just because the core slot booted.
Their runtime dependencies must be prepared against the new core interpreter and surfaced as explicit diagnostics.
If a skill uses optional `data/internal/a|b`, that data must evolve together with the prepared runtime slot, either by default copy or by an explicit `data_migration_tool`.

Target lifecycle per installed skill:

1. `prepare`
2. `test`
3. `activate`
4. `rollback` on activation or post-activation failure
5. `deactivate` if core transition is committed but a subset of skills must be quarantined afterward

The supervisor remains the authority for the overall core-update decision, but individual skill runtime outcomes must be persisted as part of the update result.

Current MVP implementation now splits this into two moments:

- early slot preparation may build the inactive core slot and mark skill migration as deferred while the old runtime is still live
- the mutating skill runtime commit step still happens only after the old runtime has stopped, so countdown traffic does not start reading partially-switched skill runtime state

Recommended per-skill diagnostic fields:

- `skill`
- `ok`
- `failed_stage`
- `prepared_version`
- `prepared_slot`
- `active_slot_before`
- `active_slot_after`
- `rollback_performed`
- `deactivated`
- `tests`
- `error`

Operator surfaces such as Infra State and Infrascope should be able to answer:

- which skill failed migration
- whether the failure happened during prepare, tests, activate, rollback, or deactivate
- whether rollback was performed
- whether the node committed the core update with some skills intentionally deactivated

After a successful core switch, `deactivate` is the preferred local containment mechanism for individual broken skills when the node should keep the new core slot rather than trigger a full rollback.
The default target behavior is:

1. runtime passes core post-switch validation
2. supervisor runs post-commit checks for active skill runtimes
3. failing skills are selectively deactivated
4. core update remains committed, but operator surfaces show degraded skill set

## Relationship to systemd

Target deployment:

- systemd manages `adaos-supervisor`
- `adaos-supervisor` manages `adaos-runtime`
- optional `adaos-realtime` may be managed directly by supervisor

This is preferred over systemd managing the main runtime process directly because systemd alone does not hold AdaOS-specific update semantics, slot state, or validation rules.

## Relationship to realtime sidecar

The supervisor and realtime sidecar solve different problems:

- `adaos-supervisor`: process and update authority
- `adaos-realtime`: transport isolation

The supervisor may launch and monitor the sidecar, but the sidecar must remain transport-only.
The sidecar must not become the hidden owner of update status, rollback state, or degraded-mode business policy.

## Migration plan

### Phase 1 - Documentation and state model

- freeze supervisor authority boundary
- define persisted attempt schema
- teach CLI to prefer supervisor-style state when available

### Phase 2 - Resilience before full split

- add stale-attempt timeout handling
- stop clearing update plan before validation commit
- emit explicit failure state for interrupted restart/apply paths

### Phase 3 - Introduce standalone supervisor process

- add `adaos supervisor serve`
- move update state and admin/update endpoints into supervisor
- make systemd unit target supervisor instead of runtime

### Phase 4 - Child runtime management

- launch runtime as a child process of supervisor
- move runtime restart and validation logic out of `autostart_runner`
- persist child process metadata and restart reason in supervisor state

### Phase 5 - Sidecar alignment

- allow supervisor to manage `adaos-realtime`
- remove runtime-lifespan ownership of sidecar startup/shutdown
- keep sidecar contract transport-only

### Phase 6 - Operator UX

- `adaos autostart/update-status` resolves to supervisor API first
- `adaos autostart update-defer` can reschedule a planned/countdown update window without losing the current supervisor attempt context
- `adaos node reliability` now falls back to browser-safe supervisor transition state and reports `runtime_restarting_under_supervisor` instead of only connection failure when runtime `:8777` is temporarily unavailable during a managed transition
- Infra State surfaces supervisor attempt state alongside runtime readiness, including `planned update`, `root promotion pending`, `root restart in progress`, and `subsequent transition queued`
- Infra State and Infrascope surface skill runtime migration diagnostics for the current or last core update attempt
- browser header/status surfaces poll a read-only supervisor transition view so controlled restarts are not shown only as generic `offline`
- canonical control-plane projections keep supervisor-owned restart/promotion phases visible even when runtime API readiness has not converged yet

## Exit criteria

The supervisor target state is complete when:

- update status remains available while runtime is down
- interrupted updates resolve to validated, failed, or rolled_back without manual file edits
- stale `restarting` / `applying` states expire deterministically
- rollback is a supervisor decision, not only a runtime-side best effort
- sidecar remains transport-only and does not absorb process/update authority
