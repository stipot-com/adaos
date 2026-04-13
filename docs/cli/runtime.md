# Runtime And Operations

## Runtime inspection

```bash
adaos runtime status
adaos runtime logs
adaos node status
adaos node reliability
```

These commands are useful for checking local readiness, runtime slots, and the broader node health model.

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

Current MVP operator flow for bootstrap/self-update:

1. run `adaos autostart update-start`
2. wait until `update-status` reports `phase: root_promotion_pending` when bootstrap-managed files changed
3. run `adaos autostart update-complete`
4. if bootstrap files were promoted, `update-status` may briefly show `supervisor attempt: awaiting_root_restart` while the restarted service is still converging under the new supervisor/bootstrap code

If the supervisor enforces a minimum interval between updates, `update-start` may return a planned transition instead of an immediate countdown. In that case:

1. `update-status` shows `state: planned` and `scheduled for: ...`
2. another update signal refreshes or annotates the queued plan instead of starting a second concurrent transition
3. `adaos autostart update-defer --delay-sec <sec>` can move the scheduled window forward
4. if a second signal arrives while a real transition is already active, supervisor records `subsequent transition: queued` and executes it once after the current transition finishes

`update-promote-root` creates a backup snapshot of the replaced bootstrap-managed files before copying them from the validated active slot into the root checkout.
`update-complete` is the higher-level Linux operator command: it performs that promotion and then runs `systemctl restart adaos.service` (or `systemctl --user restart ...` for user-scope installs). If root promotion already finished and only the supervisor/bootstrap restart is still pending, rerunning `update-complete` retries only the service restart and does not promote root again.
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
