# Runtime And Operations

## Runtime inspection

```bash
adaos runtime status
adaos runtime logs
adaos runtime memory-status
adaos runtime memory-telemetry --limit 20
adaos runtime memory-incidents --limit 20
adaos runtime memory-sessions
adaos runtime memory-session <SESSION_ID>
adaos runtime memory-artifact <SESSION_ID> <ARTIFACT_ID>
adaos runtime memory-profile-start --profile-mode sampled_profile
adaos runtime memory-profile-stop <SESSION_ID>
adaos runtime memory-profile-retry <SESSION_ID>
adaos runtime memory-publish <SESSION_ID>
adaos node status
adaos node reliability
```

These commands are useful for checking local readiness, runtime slots, and the broader node health model.
If `node reliability` falls back to the supervisor during a controlled restart, it also prints the compact public memory summary when available.
The `runtime memory-*` commands expose the supervisor-owned profiling workflow directly through the memory APIs.
In the current Phase 2 baseline, `memory-profile-start` creates a requested profiling session and supervisor converges the runtime into the requested mode through a controlled restart.
`memory-session` now includes compact operation/telemetry/artifact context for one profiling session, `memory-telemetry` gives a quick tail view of rolling growth samples, and `memory-incidents`/`memory-artifact` make it easier to inspect completed or failed profiling incidents without opening state files directly. If a profiling session failed or was cancelled, `memory-profile-retry` opens a fresh requested session using the same profile mode.

## Autostart and service mode

```bash
adaos autostart status
adaos autostart inspect
adaos autostart enable
adaos autostart disable
```

`autostart` is the operational path for running AdaOS as a managed service on the host OS.

`autostart inspect` helps debug cases where the hub is "up" but the UI times out or a CPU core is pinned:
it prints the autostart bind, the active PID, top CPU-consuming child processes, and currently running service-skills.
When supervisor mode is active it also prints the managed runtime/candidate slot state and the last recorded start/stop reasons,
which is useful when validating slot migration, candidate prewarm, and restart handoff behavior.

## Core-update controls

```bash
adaos autostart update-status
adaos autostart update-start
adaos autostart update-cancel
adaos autostart update-defer --delay-sec 300
adaos autostart update-rollback
adaos autostart update-promote-root
adaos autostart update-restore-root --backup-dir <PATH>
adaos autostart update-complete
adaos autostart smoke-update
```

These commands integrate with the runtime lifecycle and the `/api/admin/update/*` endpoints.

In service mode the authoritative update surface is the supervisor, not the transient runtime listener:

- production runtime is launched from the active slot manifest, not from the root checkout
- `update-status` should remain inspectable through supervisor-backed state even while `:8777` is restarting
- root/bootstrap code may be promoted after a successful slot validation, but the restarted production runtime still comes from slot `A|B`
- `update-status` may also include the compact Phase 1 supervisor memory summary so operators can see profiling intent/session state during the same rollout window

Current autostart-managed flow for bootstrap/self-update:

1. run `adaos autostart update-start`
2. wait for slot validation; if bootstrap-managed files changed, supervisor will automatically move through root promotion and request an autostart-service restart
3. during that handoff `update-status` may briefly show `phase: root_promotion_pending`, then `phase: root_promoted` / `supervisor attempt: awaiting_root_restart`, before the restarted service converges under the updated root-based supervisor/bootstrap code

If the supervisor enforces a minimum interval between updates, `update-start` may return a planned transition instead of an immediate countdown. In that case:

1. `update-status` shows `state: planned` and `scheduled for: ...`
2. another update signal refreshes or annotates the queued plan instead of starting a second concurrent transition
3. `adaos autostart update-defer --delay-sec <sec>` can move the scheduled window forward
4. if a second signal arrives while a real transition is already active, supervisor records `subsequent transition: queued` and executes it once after the current transition finishes

`update-promote-root` creates a backup snapshot of the replaced bootstrap-managed files before copying them from the validated active slot into the root checkout.
`update-complete` is now primarily a retry/compatibility command:

- on current autostart-managed Linux deployments, supervisor tries to complete root promotion and request the service restart automatically
- `update-complete` first asks supervisor to finish that flow server-side
- if the running supervisor is older or cannot self-request the restart, the CLI falls back to the legacy operator path and restarts the autostart service explicitly

If root promotion already finished and only the supervisor/bootstrap restart is still pending, rerunning `update-complete` retries only the restart and does not promote root again.
If a promoted supervisor/bootstrap revision fails to come back cleanly, `update-restore-root --backup-dir <PATH>` restores the root checkout from that backup snapshot without requiring a live supervisor process. Add `--restart` to request an immediate autostart service restart after the restore.

## Hub and member operations

```bash
adaos hub join-code create
adaos hub root status
adaos hub root reconnect
adaos node join --join-code <CODE>
adaos node role set --role member
```

## Yjs webspace operations

```bash
adaos node yjs status
adaos node yjs create --webspace default
adaos node yjs describe --webspace default
adaos node yjs scenario --webspace default --scenario-id web_desktop
```

The `node yjs` command group is the current operator-facing interface for synchronized webspace and desktop state.
