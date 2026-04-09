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
- slot apply / validate / rollback orchestration
- skill runtime migration orchestration for installed skills during core slot transition
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
  - owns local execution semantics, storage, skills, scenarios, and APIs
- `adaos-realtime`
  - optional transport-only sidecar
  - owns selected long-lived transport loops such as hub-root realtime transport

This means:

- the runtime is restartable without losing the local control surface
- the realtime sidecar can also be restarted independently
- update progress remains inspectable during shutdown, apply, and validate phases

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
- `POST /api/supervisor/update/rollback`
- `POST /api/supervisor/runtime/restart`

This API is the source of truth for:

- update attempt state
- restart reason
- validation deadlines
- rollback decisions
- current managed child processes
- current skill runtime migration diagnostics for the active core update attempt
- runtime liveness separate from listener bind and runtime API readiness
- active managed runtime command/executable source for the current slot

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

The important rule is that this state is committed by the supervisor, not inferred only from whether the runtime currently listens on `127.0.0.1:8777`.

## Update flow

Target flow:

1. operator or root-triggered action reaches supervisor
2. supervisor writes `update_attempt.json`
3. supervisor requests graceful runtime shutdown
4. supervisor prepares inactive slot
5. supervisor migrates installed skill runtimes against the target core interpreter
6. supervisor launches runtime from target slot
7. supervisor validates required runtime checks
8. on success, supervisor commits update and clears active attempt
9. on failure or deadline expiry, supervisor rolls back and records failure

Important invariants:

- the attempt record is not cleared before validation succeeds
- `restarting` and `applying` are bounded by deadlines
- interrupted supervisor boot resumes or resolves the last incomplete attempt
- installed skills do not silently inherit old runtime dependencies after core migration

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
- `adaos node reliability` reports `runtime_restarting_under_supervisor` instead of only connection failure
- Infra State surfaces supervisor attempt state alongside runtime readiness
- Infra State and Infrascope surface skill runtime migration diagnostics for the current or last core update attempt

## Exit criteria

The supervisor target state is complete when:

- update status remains available while runtime is down
- interrupted updates resolve to validated, failed, or rolled_back without manual file edits
- stale `restarting` / `applying` states expire deterministically
- rollback is a supervisor decision, not only a runtime-side best effort
- sidecar remains transport-only and does not absorb process/update authority
