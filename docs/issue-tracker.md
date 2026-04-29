# Issue Tracker

This document is the single living issue tracker for active AdaOS stabilization
and delivery work.

Use sections as goals. Each goal owns task groups that can be extended,
executed, and closed without creating a separate tracker document.

## Realtime First 3 Minutes

### Goal

Provide stable hub-root connectivity and error-free runtime behavior during the
first 3 minutes after startup.

Success means:

- NATS-over-WS stays connected for at least 180 seconds without watchdog reconnects.
- Root-routed HTTP and WS requests do not timeout during normal startup probes.
- Browser `/ws` and `/yws` handshakes complete without fallback-only operation.
- Yjs persistence does not create sustained high-pressure warnings.
- Startup and first browser attach do not block the event loop above diagnostic thresholds.

### Current Status

Snapshot date: 2026-04-28.

Overall completion: 80%.

Done:

- Structured terminal/log diagnostics are now available for NATS WS receive failures, direct control frames, route reply lifecycle, root log extracts, event loop lag/hang, and Yjs owner pressure.
- Hot-path `load_config()` was removed from route key matching.
- Skill runtime status reads no longer force slot prepare/path creation during snapshot calls.
- Selected synchronous skill subscription handlers can run in worker threads.
- Root log extracts now summarize repeated incidents instead of flooding the terminal by default.
- Windows Selector loop is now an explicit diagnostic mode only.
- Startup native capacity and subnet directory registry work now runs off the event loop thread.
- YRoom pressure diagnostics no longer call ystore runtime filesystem/SQLite snapshot code from the realtime hot path by default.
- In the active local `infrascope_skill` workspace/runtime copy, background refresh target discovery now runs in a worker thread.

In progress:

- Yjs write pressure is now attributed to `_by_owner/gateway_ws`, but persistence write batching/debouncing still needs a durability decision.
- `sys.ready` sync handlers are off the event loop, but their wall-clock duration is still around 3 seconds.
- Shutdown `ui.notify` can still take around 0.8-1.0 seconds.

Latest verification:

- `first3m_20260428_225403`: 180-second soak, no NATS recv failure, no route timeout, no open ack fallback, no event loop lag; one shutdown idle wait was classified as a false-positive hang.
- `first3m_20260428_230658`: after YRoom hot-path diagnostic changes, no NATS recv failure, no route timeout, no open ack fallback, no event loop lag/hang, no `runtime_snapshot()`/`Path.stat()` stack.
- `first3m_20260428_231152`: after `infrascope_skill` target-discovery offload, ready in about 13 seconds, 180-second soak completed, no NATS recv failure/watchdog, no route timeout, no open ack fallback, no event loop lag/hang, no control resume warning stack. NATS diagnostics showed Proactor loop, connected read task, `pending_data_size=0`, and no task errors.

### Tasks

#### F3M-001: NATS-over-WS disconnects after 25-60 seconds

Status: closed for the current 3-minute goal.

Evidence:

- `nats ws recv failed ... ConnectionClosedError: no close frame received or sent code=1006`
- `ConnectionResetError: [WinError 10054]`
- watchdog reports `_reading_task terminated`.
- `nats_ws_diag.jsonl` shows `last_rx_ago_s` and `last_ping_rx_ago_s` growing before disconnect while `pending_data_size` stays near zero.

Working hypothesis:

- The disconnect is not caused by local NATS pending queue starvation.
- The likely cause is upstream/proxy/socket idle handling, Windows event loop behavior, or reconnect supersede overlap.

Actions:

- [x] Log structured close/error diagnostics from the Python WS transport.
- [x] Keep client-side NATS ping interval task disabled for WS transport by default.
- [x] Add backend WS/NATS proxy keepalive and supersede diagnostics.
- [x] Make Windows Selector loop an explicit diagnostic mode only.
- [x] Run multiple 180-second soaks with `ADAOS_WIN_SELECTOR_LOOP=0`.
- [x] Confirm the current loop is `WindowsProactorEventLoopPolicy` / `ProactorEventLoop`.
- [x] Confirm no local pending-data backpressure during the verified 180-second runs.
- [ ] Reopen only if a new 180-second run captures a watchdog reconnect.
- [ ] If reopened, compare root proxy upstream ping/pong cadence against client-side `last_rx_ago_s`.
- [ ] If reopened, decide whether the proxy should proactively close/reopen quiet upstream sockets before hard reset.

#### F3M-002: Root-routed HTTP requests timeout during startup

Status: closed for the current 3-minute goal.

Evidence:

- Repeated `http route: timeout` and `http proxy failed` for `/api/node/status`, `/api/node/reliability`, `/api/node/reliability/summary`, and `/api/node/infrastate/snapshot`.
- Root logs show many `route.v2.to_hub` requests reach the WS/NATS proxy and are sent downstream, but not all responses return before `15000ms`.

Working hypothesis:

- Route request delivery is not the only failure point.
- Missing or delayed hub/browser response handling, reconnect overlap, or slow local handlers may leave route replies unpublished.

Actions:

- [x] Add route request/reply lifecycle diagnostics.
- [x] Add route publish/flush slow warnings and pending-data diagnostics.
- [x] Remove route key hot-path config reload.
- [x] Verify latest 180-second soaks have no `http route: timeout` and no `http proxy failed`.
- [ ] Reopen and correlate one timed-out `keyTag` from root logs with hub route callback logs if a timeout recurs.
- [ ] Add a compact route timeout summary grouped by path and keyTag.
- [ ] Rate-limit or defer non-critical root probes while NATS is reconnecting.

#### F3M-003: Browser WS open ack fallback is still observed

Status: closed for the current 3-minute goal.

Evidence:

- `ws route: open ack fallback elapsed`
- early frame counters are present before `open_ack`.

Working hypothesis:

- The hub can receive early frames before the route open acknowledgement is returned to root.
- This may be harmless for some sessions, but it hides handshake latency and complicates timeout diagnosis.

Actions:

- [x] Add early frame count/bytes to open ack fallback logs.
- [x] Verify latest 180-second soaks have no `open ack fallback`.
- [ ] Reopen and correlate fallback cases with NATS reconnect and route timeout windows if fallback recurs.
- [ ] Decide whether fallback should become a degraded state rather than only a warning if fallback recurs.

#### F3M-004: Yjs write pressure during first attach

Status: in progress.

Evidence:

- `YJS owner flow above threshold ... source=yjs.gateway_ws channel=core.yjs.gateway.live_room.persist`
- High write count and byte bursts around first browser attach.

Working hypothesis:

- Initial replay/persist activity or repeated stream snapshot projection writes may be too chatty during the first attach window.

Actions:

- [x] Attribute Yjs pressure by source/channel.
- [x] Split gateway persistence out of `_by_owner/unknown` as `_by_owner/gateway_ws`.
- [x] Move YRoom diagnostic ystore runtime snapshots out of the realtime hot path by default.
- [x] Confirm latest 180-second soak has no `_by_owner/unknown` pressure and no YRoom `runtime_snapshot()`/`Path.stat()` blocking stack.
- [ ] Decide whether to batch/debounce `gateway_ws` ystore writes during the first 180 seconds; this has a persistence durability tradeoff.
- [ ] If batching is accepted, implement a bounded flush interval and confirm no sustained `_by_owner/gateway_ws` pressure warning remains.

#### F3M-005: Event loop lag/hang during startup and shutdown

Status: in progress.

Evidence:

- Loop lag warnings still appear, but latest deep stacks no longer point to route key config reload or skill runtime path preparation.
- Some hang dumps show idle Windows selector wait during shutdown.
- `ui.notify` and `webio.stream.snapshot.requested` handlers can still be slow.

Working hypothesis:

- The biggest known synchronous hot paths were removed.
- Remaining lag is a mix of diagnostic selector behavior, slow notification/network IO, and Yjs/snapshot bursts.

Actions:

- [x] Add structured loop lag/hang logs.
- [x] Move selected sync subscriptions to worker threads.
- [x] Make Selector loop opt-in diagnostics only.
- [x] Add per-topic/adapted-handler labels to slow handler warnings.
- [x] Move startup native capacity/subnet registry work to a worker thread.
- [x] Suppress idle Proactor wait stacks as hang false positives.
- [x] Move active local `infrascope_skill` background target discovery to a worker thread.
- [ ] Persist the `infrascope_skill` fix in the skill source of truth if it is outside the ignored `.adaos/` workspace.
- [ ] Move slow `ui.notify` network work away from eventbus critical path.
- [ ] Decide whether off-thread `sys.ready` wall-clock durations need optimization, or only continued monitoring.

### Operating Checklist

Before a 3-minute soak:

- [ ] `ADAOS_WIN_SELECTOR_LOOP=0`.
- [ ] `HUB_NATS_WS_DIAG_FILE=.adaos/diagnostics/nats_ws_diag.jsonl`.
- [ ] `HUB_ROOT_LOG_SNAPSHOT=1`.
- [ ] Deep trace is off unless investigating one focused case.

During analysis:

- [ ] Start with `.adaos/logs/adaos.log`.
- [ ] Use `.adaos/diagnostics/nats_ws_diag.jsonl` to distinguish RX silence from local backpressure.
- [ ] Use `.adaos/root_log_snapshots/*__extract.log` to correlate root route timeout keyTags.
- [ ] Only request full terminal output when the local logs are missing the incident window.

### Next Milestone

Reach 100% by resolving the remaining durability/performance choice:

- choose whether `gateway_ws` ystore persistence may batch/debounce small updates during the first 180 seconds,
- implement the chosen persistence policy,
- move shutdown `ui.notify` work off the eventbus critical path,
- run a final 180-second soak with no NATS reconnect, no route/open-ack errors, no event loop lag/hang, and no sustained Yjs pressure warning.
