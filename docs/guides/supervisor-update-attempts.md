# Supervisor Update Attempt Fields

This note is a short operator reference for the normalized `attempt` object exposed by:

- `GET /api/supervisor/update/status`
- `GET /api/supervisor/public/update-status`
- `adaos autostart update-status`

The goal is to keep update intent and outcome readable even while the runtime is restarting or the transition has not fully converged.

## Core fields

- `contract_version`: schema version for the persisted attempt payload. Operators can use this to distinguish stable supervisor-owned state from older ad-hoc payloads.
- `authority`: the component that owns the attempt lifecycle. In the current baseline this should be `supervisor`.
- `state`: current lifecycle state such as `planned`, `countdown`, `applying`, `awaiting_root_restart`, `succeeded`, or `failed`.
- `action`: high-level operation the attempt is trying to perform, typically a managed core update or follow-up transition.

## Timing fields

- `requested_at`: when the attempt was first created.
- `transitioned_at`: when the attempt most recently changed lifecycle state.
- `scheduled_for`: when a planned update is expected to begin.
- `updated_at`: when the persisted payload was last rewritten by supervisor.
- `completed_at`: when the attempt reached a terminal state.

## Reason fields

- `reason`: original trigger or request reason, usually the source of the update request.
- `planned_reason`: the operator-facing explanation for why the attempt is queued or scheduled.
- `completion_reason`: the operator-facing explanation for how the attempt finished, failed, or paused at a restart boundary.

`adaos autostart update-status` now renders `authority`, `planned_reason`, and `completion_reason` explicitly because these are the quickest fields to check during an incident or controlled rollout.

## Restart and transition fields

- `awaiting_restart`: `true` when supervisor is waiting for the required restart boundary to complete.
- `restart_required`: `true` when the current plan cannot finish without a restart.
- `restart_mode`: restart style requested by supervisor, for example a managed root/runtime restart path.
- `restart_requested_at`: when supervisor asked for the restart.
- `subsequent_transition`: `true` when an additional transition is already queued after the current one.
- `subsequent_transition_requested_at`: when that follow-up transition was scheduled.

## Candidate prewarm fields

- `candidate_prewarm_state`: readiness state for a warm candidate runtime.
- `candidate_prewarm_message`: short diagnostic message for the warm candidate.
- `candidate_prewarm_ready_at`: when the candidate reached a ready state, if it did.

## Status carry-over fields

- `last_status`: compact snapshot of the most recent status supervisor attached to the attempt.
- `accepted`: whether supervisor accepted the request.
- `target_rev` and `target_version`: requested update target.

## Quick read order

When diagnosing a live rollout, check fields in this order:

1. `state`
2. `authority`
3. `planned_reason` or `completion_reason`
4. `scheduled_for` / `transitioned_at`
5. `awaiting_restart` and `restart_mode`

If `authority=supervisor` and the runtime is temporarily unavailable, the supervisor surfaces should still be treated as the source of truth for the active update attempt.

Browser delivery now has two layers:

- `supervisor.update.status.raw`: browser-safe raw supervisor transition payload. Clients should prefer this for live transition UI because it keeps the supervisor-owned shape (`status`, `attempt`, `runtime`) intact.
- `core.update.status`: normalized semantic event for the wider control plane. Keep using this for generic update notifications and compatibility with older consumers.

When both are present, browser/runtime code should treat `supervisor.update.status.raw` as the higher-priority source and keep HTTP polling of `/api/supervisor/public/update-status` only as a fallback.
