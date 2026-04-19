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

For the target media-continuity path, this split also enables a more selective policy:

- if a member currently owns an active live media session, that member update should be deferred
- a hub runtime restart may still be allowed if the hub-side realtime sidecar can stay alive and continue serving the delegated realtime continuity path
- supervisor should therefore treat "restart runtime" and "tear down sidecar" as different actions once live media continuity depends on sidecar ownership

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

Before cutover is explicitly committed, a prewarmed `candidate` runtime must stay passive on root-facing traffic subjects.

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

Fast cutover does not remove that rule.
It only defines the moment when supervisor may end passive mode:

- supervisor explicitly authorizes the already-running candidate to promote itself through `POST /api/admin/runtime/promote-active`
- the candidate flips to `transition_role=active`, reconnects root-facing transport under that new authority, and only then becomes eligible to own live hub traffic
- supervisor adopts that promoted process as the managed active runtime instead of launching a second fresh process when warm-switch succeeds
- if promotion or adoption fails, supervisor tears the candidate down and falls back to the existing stop-and-switch launch path

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

But the supervisor must remain able to enforce locally visible continuity guards exposed by the runtime model, for example:

- defer member update while member-owned live media remains active
- refuse a hub restart path that would drop a sidecar continuity contract the system currently depends on
- distinguish "runtime restart allowed with sidecar continuity" from "full local media teardown"

The first conservative version of this policy is now implemented:

- supervisor reads runtime continuity guard data from `GET /api/node/reliability`
- update transitions are deferred into explicit `planned/live_media_guard` state when the continuity contract says restart would be unsafe
- manual runtime restart is refused until sidecar continuity becomes a real ready capability rather than only a planned target

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
- `POST /api/supervisor/update/complete`
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

Current MVP browser behavior may preserve and display the last known transition state during reconnect windows, and routed hub sessions can now consume that browser-safe transition state primarily as pushed `core.update.status` events over the control `/ws` channel, with `/hubs/<id>/api/supervisor/public/update-status` retained as a fallback when the control channel is unavailable.
The target end state is stronger: every supported browser entry topology should be able to poll that read-only supervisor transition surface directly, so the shell can keep moving from `hub restarting` to `rollback in progress` or `root promotion pending` from supervisor truth rather than only from the last runtime-visible snapshot.
Operator-facing surfaces are also expected to consume that same supervisor truth through the canonical control-plane model, so Infrascope and related overview projections can show core-runtime transition state in `active_runtimes`, health strips, and recent changes instead of presenting a restart only as generic hub instability.
That browser-safe surface now also includes candidate runtime diagnostics needed for warm-switch work:

- `action`
- `candidate_runtime_instance_id`
- `candidate_runtime_state`
- `candidate_runtime_api_ready`
- `candidate_transition_role`
- `candidate_prewarm_state`
- `candidate_prewarm_message`
- `candidate_prewarm_ready_at`

### Runtime API

Available only while `adaos-runtime` is running:

- current node APIs
- current admin APIs that belong to runtime semantics
- cutover-only runtime identity operations such as `POST /api/admin/runtime/promote-active`
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
8. supervisor activates the target slot and either promotes the prewarmed candidate runtime to active authority or launches production runtime from that slot
9. supervisor validates required runtime checks against that target-slot runtime
10. on slot-validation success, supervisor commits the transition result
11. if bootstrap-managed files changed, supervisor records `root_promotion_required` and promotes root from the same validated candidate
12. on autostart-managed deployments, supervisor requests autostart-service restart so the root-based supervisor/bootstrap code actually switches over
13. on failure or deadline expiry, supervisor rolls back the slot and records failure

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
- current implementation promotes bootstrap-managed files into the explicit validated root target recorded for that slot, writes a backup snapshot plus restore metadata, records an explicit supervisor attempt state while waiting for restart, and on autostart-managed Linux deployments requests the service restart automatically so the new supervisor/bootstrap code becomes active
- if another transition request arrives before that restart completes, it is queued as `subsequent_transition` on the supervisor attempt instead of being dropped or run concurrently
- manual `adaos autostart update-complete` remains the compatibility and retry path for older supervisors or environments where self-requested restart is unavailable

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

Current MVP implementation also starts a best-effort passive `candidate` runtime prewarm when:

- the inactive slot is already prepared
- slot ports are reserved distinctly
- supervisor admitted `warm_switch`

That prewarm now feeds a real fast-cutover path:

- candidate readiness is surfaced through supervisor runtime/public status
- candidate remains passive on root-routed traffic subjects until supervisor explicitly commits cutover
- once the old runtime is down and the prepared slot is activated, supervisor may promote/adopt the already-running candidate instead of starting a fresh runtime process
- if candidate promotion, root reconnect, or supervisor adoption fails, supervisor falls back to the existing stop-and-switch launch path from the same prepared slot

This keeps warm-switch opportunistic and reversible: the node gets a genuine low-downtime cutover path when the candidate is ready, but constrained or unhealthy cases still converge through the proven fallback path.

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
- `adaos-supervisor` also manages `adaos-realtime` when sidecar mode is enabled in managed topology

This is preferred over systemd managing the main runtime process directly because systemd alone does not hold AdaOS-specific update semantics, slot state, or validation rules.

## Relationship to realtime sidecar

The supervisor and realtime sidecar solve different problems:

- `adaos-supervisor`: process and update authority
- `adaos-realtime`: transport isolation

In the managed topology, the supervisor launches, monitors, and restarts the sidecar.
Standalone runtime-owned sidecar startup remains only as compatibility fallback when supervisor is absent.
The sidecar must remain transport-only.
The sidecar must not become the hidden owner of update status, rollback state, or degraded-mode business policy.

## Memory leak detection and profiling

The supervisor should also become the local authority for memory leak detection, profiling escalation, and remote retrieval of profiling evidence.

This work is intentionally prioritized ahead of the broader supervisor rollout because:

- core-slot evolution can change memory behavior even when update flow is otherwise healthy
- skill runtime evolution can introduce long-lived leaks outside the narrow core-update path
- low-memory devices need an explicit local guard before a leak turns into process death or unstable restart loops

### Goal

Provide an always-on supervisor-owned memory watchdog that can:

- observe runtime process-family memory after start, restart, and slot switch
- distinguish normal warm-up from suspicious sustained growth
- restart the runtime in an explicit profiling mode when policy thresholds are crossed
- correlate memory growth with top-level runtime operations
- persist local profiling sessions and summaries
- publish profiling summaries and artifacts to root so they can be retrieved remotely by zone-scoped operator workflows

### Operating model

The target model is policy-driven rather than "always profile everything".

Supervisor should run the managed runtime in one of these modes:

- `normal`
- `sampled_profile`
- `trace_profile`

Target behavior:

1. runtime starts in `normal`
2. supervisor samples process-family memory and records a rolling baseline
3. if the memory policy detects suspicious growth, supervisor records a profiling session intent
4. supervisor restarts the same slot in `sampled_profile`
5. if the profile confirms continued abnormal growth, supervisor records a leak incident and keeps the node recoverable through restart / rollback / quarantine policy
6. operator or root workflows can later retrieve the profiling summary and, when configured, the heavier profiling artifacts

The important rule is that profiling is an escalated diagnostic mode under supervisor policy, not a permanent runtime tax.

### Signals and admission rules

Supervisor should avoid triggering profiling from one instantaneous RSS sample.

Memory suspicion should be based on a combination of:

- absolute process-family RSS over a configured threshold
- positive RSS growth slope over a time window
- post-switch RSS significantly above the pre-switch baseline
- threshold breach sustained beyond a stabilization grace period

This is especially important because AdaOS runtimes may legitimately allocate memory during:

- slot boot and dependency import
- skill runtime preparation or activation
- workspace materialization
- model/session warm-up
- cache rebuild after update or rollback

### Profiler strategy

The preferred default profiler strategy is:

- built-in `tracemalloc` for automatic supervisor-triggered profiling sessions
- optional heavier profiler adapters for deep-dive workflows on supported environments

The current target adapter split is:

- `TracemallocProfilerAdapter`
  - lowest operational complexity
  - safe for automated restart-into-profile mode
  - useful for Python allocation growth snapshots and diffs
- `MemrayProfilerAdapter`
  - optional deep-dive adapter for environments where native-allocation analysis is worth the overhead and platform support is available
  - not required for the first implementation

The supervisor must treat profilers as pluggable adapters.
The policy engine decides when to escalate; the adapter decides how profiling is started, stopped, and materialized into artifacts.

### Runtime launch contract

Escalation into profiling mode must not depend on ad-hoc runtime-specific flags.

The supervisor-owned launch contract should therefore reserve explicit runtime environment keys for memory profiling:

- `ADAOS_SUPERVISOR_PROFILE_MODE`
- `ADAOS_SUPERVISOR_PROFILE_SESSION_ID`
- `ADAOS_SUPERVISOR_PROFILE_TRIGGER`

Rules:

- `normal` remains the default when these keys are absent
- Phase 1 may expose these keys as part of the contract before restart-into-profile is implemented
- Phase 2 uses the same keys for the actual restart-into-profile flow instead of inventing a second launch mechanism
- the runtime may treat these keys as read-only diagnostic context and must not promote itself into profiling mode by local guesswork alone

### Top-level operation log

Profiling artifacts are much more useful when they can be aligned with top-level runtime activity.

The runtime should therefore emit a compact supervisor-consumable operation log for events such as:

- `slot_started`
- `slot_promoted`
- `skill_loaded`
- `skill_activated`
- `skill_unloaded`
- `scenario_started`
- `workspace_opened`
- `model_session_started`
- `tool_invoked`
- `core_update_prepare`
- `core_update_apply`
- `core_update_activate`

This log should stay high-level and bounded.
It is not intended to mirror every internal event-bus message.

Recommended `operations.ndjson` record shape:

- `contract_version`
- `event_id`
- `event`
- `emitted_at`
- `session_id`
- `profile_mode`
- `slot`
- `runtime_instance_id`
- `transition_role`
- `sample_source`
- `sequence`
- `details`

Phase 1 should freeze this envelope even if the runtime is not yet emitting real operation traffic beyond supervisor-owned control intents.

### Persisted profiling state

In addition to the current supervisor state files, the target model should add local profiling storage under supervisor state.

Recommended target layout:

- `state/supervisor/memory/runtime.json`
- `state/supervisor/memory/telemetry.ndjson`
- `state/supervisor/memory/sessions/<session_id>/summary.json`
- `state/supervisor/memory/sessions/<session_id>/operations.ndjson`
- `state/supervisor/memory/sessions/<session_id>/artifacts/...`
- `state/supervisor/memory/sessions/index.json`

Recommended summary fields:

- `session_id`
- `slot`
- `runtime_instance_id`
- `transition_role`
- `profile_mode`
- `trigger_reason`
- `trigger_threshold`
- `baseline_rss_bytes`
- `peak_rss_bytes`
- `rss_growth_bytes`
- `started_at`
- `finished_at`
- `suspected_leak`
- `top_growth_sites`
- `operation_window`
- `published_to_root`
- `artifact_refs`

The important rule is that supervisor owns the diagnostic session record even if runtime produces the raw snapshots.

### Phase 1 implementation baseline

The current Phase 1 implementation establishes the contract and storage baseline without yet enabling automatic restart-into-profile behavior.

Current implementation artifacts:

- `src/adaos/services/supervisor_memory.py`
  - supervisor-owned schema normalization for memory telemetry samples, runtime state, session summaries, operation events, and artifact refs
  - dedicated state-path helpers for supervisor memory storage
- `state/supervisor/memory/runtime.json`
  - persisted memory profiling runtime contract and current live-mode summary
- `state/supervisor/memory/sessions/index.json`
  - persisted index for profiling sessions
- `state/supervisor/memory/sessions/<session_id>/operations.ndjson`
  - contract-stable per-session operation log used for later growth correlation

Current implementation surfaces:

- `GET /api/supervisor/memory/status`
- `GET /api/supervisor/memory/telemetry`
- `GET /api/supervisor/memory/incidents`
- `GET /api/supervisor/memory/sessions`
- `GET /api/supervisor/memory/sessions/{session_id}`
- `GET /api/supervisor/memory/sessions/{session_id}/artifacts/{artifact_id}`
- `POST /api/supervisor/memory/profile/start`
- `POST /api/supervisor/memory/profile/{session_id}/stop`
- `POST /api/supervisor/memory/publish`

Current implementation scope now spans the completed Phase 1 baseline plus the first active Phase 2 slice:

- keeps the authority boundary, launch contract, operation log, and local session store under supervisor ownership
- records rolling process-family telemetry under `state/supervisor/memory/telemetry.ndjson`
- persists baseline RSS, growth, slope, telemetry cadence, and compact suspicion state in `runtime.json`
- exposes implemented launch modes `normal`, `sampled_profile`, and `trace_profile` as supervisor-managed runtime truth
- lets `profile/start` and policy-created sessions converge through the same requested-profile workflow instead of separate ad-hoc paths
- applies requested profile mode through a controlled supervisor restart using the Phase 1 launch contract keys
- creates a supervisor-owned profiling session automatically when telemetry crosses both growth and slope thresholds
- materializes local `tracemalloc` start/final/top-growth artifacts under `state/supervisor/memory/sessions/<session_id>/artifacts`
- adds trace-oriented `tracemalloc` traceback artifacts when `trace_profile` is selected so that trace mode yields richer diagnostics than the sampled baseline
- records top growth sites and artifact refs back into the supervisor-owned session summary when a profiled runtime exits
- suppresses repeated policy-triggered restart loops with cooldown/circuit-breaker guards before opening another automatic profiling session
- exposes telemetry tail and richer per-session inspection so operators can inspect growth samples, operation log, and collected artifacts together
- exposes explicit retry flow for failed/cancelled/stopped profiling sessions instead of overloading manual start semantics

Current implementation control mode is `phase2_supervisor_restart`:

- `profile/start` creates a supervisor-owned profiling request and the monitor applies it through restart-into-profile
- `profile/stop` clears the requested mode and lets the monitor converge the runtime back to `normal`
- suspicion policy can create a `sampled_profile` request automatically when growth remains both large and steep
- `publish` records operator intent locally and now attempts the first dedicated root summary publication path, persisting `published_ref` / `publish_result` back into the supervisor-owned session record
- retry-created sessions now carry explicit retry-chain metadata (`retry_of_session_id`, `retry_root_session_id`, `retry_depth`) so later incidents can be grouped without reconstructing lineage from operations alone

Current implementation deliberately does not yet:

- publish heavy profiling artifacts to root
- provide remote retrieval of heavy artifacts for the new memory-profile report family

The first active Phase 3 slice now exists:

- supervisor publishes memory-profile summaries to root through a dedicated `memory_profile` report family
- root-side retrieval can list those summaries by hub, optional session id, compact state filters, and suspected-only filters before heavy artifact transport is added
- operator surfaces can open one remotely published memory-profile session directly to inspect RSS deltas, retry lineage, telemetry tail size, and artifact summary metadata
- operator surfaces can inspect that remote summary path through `adaos hub root reports --kind memory-profile`

### Local control surfaces

The target local supervisor memory API should include read-only status and explicit operator controls:

- `GET /api/supervisor/memory/status`
- `GET /api/supervisor/memory/sessions`
- `GET /api/supervisor/memory/sessions/{session_id}`
- `POST /api/supervisor/memory/profile/start`
- `POST /api/supervisor/memory/profile/{session_id}/stop`
- `POST /api/supervisor/memory/publish`

The browser-safe read-only surface should eventually expose a compact memory incident summary without exposing mutating controls.

Phase 1 and the early Phase 2 slice expose a compact browser-safe memory summary:

- `GET /api/supervisor/public/memory-status`

That surface is intentionally small and read-only:

- current profile/control mode
- requested profiling intent, if any
- suspicion state
- compact baseline/growth summary
- session counters
- compact last-session summary

For manual controls, the safety policy should be explicit:

- manual profile start must be rejected while a core transition is already active
- only one active profiling intent/session may exist at a time unless a future multi-session policy is documented explicitly
- `publish` may record an operator request during Phase 1, but must not claim that root publication has completed until an explicit ack exists
- low-memory or degraded nodes may still downgrade artifact collection even when restart-into-profile is supported

### Root retrieval model

Profiling evidence should follow the same general remote-access philosophy as other root control reports:

- the node keeps local authoritative copies first
- supervisor publishes summaries asynchronously
- root indexes those summaries by hub, subnet, and zone
- heavier artifacts can be fetched only when requested and authorized

Target root-facing capabilities:

- ingest a memory-profile summary report from a hub
- list memory-profile incidents by `hub_id`, `subnet_id`, and `zone`
- fetch a single profiling session summary
- retrieve profiling artifacts when policy and size constraints allow it

This should remain a separate report family rather than overloading generic lifecycle reports.

### Safety and recovery rules

Memory profiling must not reduce the node to an unrecoverable state.

Supervisor policy should therefore preserve these rules:

- profiling restarts must stay bounded by timeouts
- repeated profile-trigger loops must trip a circuit breaker
- low-memory devices may skip heavy artifact collection and keep only summaries
- candidate prewarm and warm-switch memory admission must remain separate from leak suspicion policy
- a confirmed leak may trigger rollback or quarantine policy, but profiling itself must not silently mutate slot authority

## Migration plan

### Phase 1 - Memory watchdog architecture and state model

- freeze the supervisor-owned memory profiling authority boundary
- define memory telemetry, profiling session, and artifact metadata schemas
- document profiler adapter strategy with `tracemalloc` as the default automated path
- define top-level operation-log contracts needed to correlate memory growth with runtime behavior
- freeze the runtime launch contract for future restart-into-profile mode
- expose explicit operator profiling intents in supervisor-owned APIs and state, even before policy-driven automatic restart is implemented

### Phase 2 - Local memory telemetry and profiler-mode restart

- add supervisor-owned rolling process-family memory telemetry
- add suspicion policy based on threshold + slope + stabilization window
- add explicit runtime launch modes `normal`, `sampled_profile`, and `trace_profile`
- implement restart-into-profile flow for the active slot when memory policy is breached using the Phase 1 launch contract
- persist local profiling sessions and summaries under `state/supervisor/memory`

### Phase 3 - Root publication and remote retrieval

- publish memory-profile summaries to root as a dedicated report family
- scope retrieval by `hub_id`, `subnet_id`, and `zone`
- expose lightweight operator retrieval flows before large artifact transport
- keep local-first retention so profiling evidence survives root/network outages

### Phase 4 - Documentation and baseline supervisor state model

- freeze supervisor authority boundary
- define persisted attempt schema
- teach CLI to prefer supervisor-style state when available

### Phase 5 - Resilience before full split

- add stale-attempt timeout handling
- stop clearing update plan before validation commit
- emit explicit failure state for interrupted restart/apply paths

### Phase 6 - Introduce standalone supervisor process

- add `adaos supervisor serve`
- move update state and admin/update endpoints into supervisor
- make systemd unit target supervisor instead of runtime

### Phase 7 - Child runtime management

- launch runtime as a child process of supervisor
- move runtime restart and validation logic out of `autostart_runner`
- persist child process metadata and restart reason in supervisor state

### Phase 8 - Sidecar alignment

- keep `adaos-realtime` lifecycle under supervisor in managed topology
- keep runtime-owned startup/shutdown only as standalone fallback when supervisor is absent
- keep sidecar contract transport-only
- keep warm candidates memory-bounded: warm-switch admission should account for runtime process-family RSS, and candidate prewarm should defer external service-skill startup until cutover

### Phase 9 - Operator UX

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
