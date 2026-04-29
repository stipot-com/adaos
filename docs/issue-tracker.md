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
- Process memory is sampled during loading-to-ready and through the first 3 minutes; it reaches a stable startup plateau and does not show runaway growth.

### Current Status

Snapshot date: 2026-04-29.

Overall completion: 100% for the current local runtime goal.

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
- `ui.notify` delivery no longer holds the eventbus critical path; RouterService schedules notification delivery in background and drains briefly on shutdown.
- Root MCP local SDK calls use a local-first embedded registry path for local runtime queries, so normal startup no longer probes the public Root MCP bridge or emits `fetch failed` fallback diagnostics.
- Yjs gateway persistence keeps immediate writes for durability, while owner-pressure diagnostics now treat gateway first-attach peak bursts separately from sustained pressure.
- The hub subnet-directory staler heartbeat/stale sweep no longer commits SQLite work on the event loop thread.
- NATS WS diagnostic JSONL writes are emitted from a worker thread instead of the NATS supervisor hot path.
- Active local `infra_access_skill` and `infrastate_skill` workspace/runtime copies no longer perform heavy snapshot refresh from `sys.ready` subscription callbacks.
- Active local `infrastate_skill` runtime event handling now returns from `sys.ready` without a worker-thread hop, eliminating the last startup slow-handler warning.
- Active `.adaos` skill hotfixes are present in the workspace skill registry repo through `d208cd3`; DEV Forge publish dry-run is not applicable because these are workspace-registry skills, not DEV Forge drafts.
- Final soak verification now includes process-tree memory sampling during loading-to-ready and the full 180-second window.

In progress:

- None for the current Realtime First 3 Minutes goal.

Known follow-up outside the current goal:

- If public remote Root MCP access to local hubs is required, design and deploy a backend/infra route that resolves upstream by hub route/NATS instead of direct `ADAOS_BASE` HTTP proxying.

Latest verification:

- `first3m_20260428_225403`: 180-second soak, no NATS recv failure, no route timeout, no open ack fallback, no event loop lag; one shutdown idle wait was classified as a false-positive hang.
- `first3m_20260428_230658`: after YRoom hot-path diagnostic changes, no NATS recv failure, no route timeout, no open ack fallback, no event loop lag/hang, no `runtime_snapshot()`/`Path.stat()` stack.
- `first3m_20260428_231152`: after `infrascope_skill` target-discovery offload, ready in about 13 seconds, 180-second soak completed, no NATS recv failure/watchdog, no route timeout, no open ack fallback, no event loop lag/hang, no control resume warning stack. NATS diagnostics showed Proactor loop, connected read task, `pending_data_size=0`, and no task errors.
- `first3m_20260429_065606`: after RouterService background `ui.notify` delivery, ready in about 15 seconds, 180-second soak completed, no NATS recv failure/watchdog, no route timeout, no open ack fallback, no event loop lag/hang, no slow `ui.notify`, and no router background delivery failure. Remaining warnings were two off-thread `sys.ready` durations and two `_by_owner/gateway_ws` Yjs pressure warnings.
- `first3m_20260429_080100`: final 180-second soak completed and stopped cleanly. Counts: NATS recv failure/watchdog/ConnectionClosedError/WinError 10054 = 0, route timeout/proxy failed = 0, open ack fallback = 0, real event loop lag/hang = 0, slow async handler = 0, slow `ui.notify` = 0, Yjs owner pressure/unknown/gateway warning = 0, infra_access snapshot failure = 0, traceback = 0. Expected non-failing signals: one embedded Root MCP fallback debug line, one idle-wait hang suppression during shutdown, one NATS disconnect during requested shutdown.
- `first3m_20260429_final_mem4`: final 180-second soak with process-tree memory sampling. Ready in 13.461s. Counts: NATS recv failure/watchdog/ConnectionClosedError/WinError 10054 = 0, route timeout/proxy failed = 0, open ack fallback = 0, event loop lag = 0, real event loop hang = 0, slow async handler = 0, slow `ui.notify` = 0, Yjs owner pressure = 0, infra_access snapshot failure = 0, traceback = 0, Root MCP `fetch failed` = 0, embedded Root MCP fallback = 0. Expected non-failing signals: one idle-wait hang suppression during shutdown and one NATS disconnect during requested shutdown. Memory: process tree WorkingSet first/ready/peak/last = 121.695/238.305/250.066/248.066 MB; PrivateMemory first/ready/peak/last = 95.172/218.930/230.555/228.117 MB; loading-to-ready sampled 121.695 -> 238.305 MB WorkingSet and 95.172 -> 218.930 MB PrivateMemory; no runaway growth observed.
- `first3m_20260429_final_accept`: repeat final acceptance run. Ready in 12.795s, browser `/ws` accepted, and `YRoom ready webspace=desktop` observed. Counts: NATS recv failure/watchdog/ConnectionClosedError/WinError 10054 = 0, route timeout/proxy failed = 0, open ack fallback = 0, event loop lag = 0, real event loop hang = 0, slow async handler = 0, slow `ui.notify` = 0, Yjs owner pressure = 0, infra_access snapshot failure = 0, traceback = 0, Root MCP `fetch failed` = 0, embedded Root MCP fallback = 0. Expected non-failing signals: one idle-wait hang suppression during shutdown and one NATS disconnect during requested shutdown. Memory: process tree WorkingSet first/ready/peak/last = 30.949/136.863/146.445/145.836 MB; PrivateMemory first/ready/peak/last = 21.930/137.117/145.211/145.211 MB; no runaway growth observed.

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

Status: closed for the current 3-minute goal.

Evidence:

- `YJS owner flow above threshold ... source=yjs.gateway_ws channel=core.yjs.gateway.live_room.persist`
- High write count and byte bursts around first browser attach.

Resolution:

- Initial gateway first-attach bursts are expected and preserve durable YStore/subnet replication semantics.
- The current fix keeps immediate persistence and changes diagnostics to alert on sustained gateway pressure rather than peak-only attach bursts.

Actions:

- [x] Attribute Yjs pressure by source/channel.
- [x] Split gateway persistence out of `_by_owner/unknown` as `_by_owner/gateway_ws`.
- [x] Move YRoom diagnostic ystore runtime snapshots out of the realtime hot path by default.
- [x] Confirm latest 180-second soak has no `_by_owner/unknown` pressure and no YRoom `runtime_snapshot()`/`Path.stat()` blocking stack.
- [x] Decide not to batch/debounce `gateway_ws` ystore writes for this goal; durability wins over cosmetic write smoothing.
- [x] Tune gateway-owner pressure alerts to suppress peak-only first-attach warnings while preserving sustained-pressure alerts.
- [x] Confirm final 180-second soak has no Yjs owner pressure warning.

#### F3M-005: Event loop lag/hang during startup and shutdown

Status: closed for the current 3-minute goal.

Evidence resolved:

- Earlier lag stacks pointed to route key config reload, skill runtime path preparation, subnet-directory SQLite commit, NATS diagnostic file append, and skill snapshot refreshes triggered by `sys.ready`.
- The final accepted run has no real event loop lag/hang, no control lifecycle delayed warning, and no slow async handlers.
- Shutdown can still emit an expected idle-wait suppression debug line and a requested NATS disconnect warning.

Resolution:

- Known synchronous startup/hot-path operations have been moved off the event loop or deferred out of `sys.ready`.
- Diagnostic writes remain enabled, but NATS WS JSONL append now runs in a worker thread.

Actions:

- [x] Add structured loop lag/hang logs.
- [x] Move selected sync subscriptions to worker threads.
- [x] Make Selector loop opt-in diagnostics only.
- [x] Add per-topic/adapted-handler labels to slow handler warnings.
- [x] Move startup native capacity/subnet registry work to a worker thread.
- [x] Suppress idle Proactor wait stacks as hang false positives.
- [x] Move active local `infrascope_skill` background target discovery to a worker thread.
- [x] Move slow `ui.notify` network work away from eventbus critical path.
- [x] Move hub subnet-directory staler heartbeat/stale sweep SQLite work off the event loop.
- [x] Move NATS WS diagnostic file writes off the NATS supervisor hot path.
- [x] Preserve skill/handler labels through SDK bus adaptation so slow warnings identify the exact skill.
- [x] Remove heavy `sys.ready` refresh work from active local `infra_access_skill` and `infrastate_skill` workspace/runtime copies.
- [x] Avoid worker-thread hop for `infrastate_skill.on_runtime_event` on `sys.ready`.
- [x] Confirm final 180-second soak has no real event loop lag/hang and no slow async handler warnings.

#### F3M-006: Root MCP local startup uses fallback as the normal path

Status: closed for the current 3-minute goal.

Evidence resolved:

- Earlier accepted runs emitted `Root MCP bridge upstream unavailable; using embedded local Root MCP operation=surface` because local SDK calls went through the public Root MCP bridge.
- The public backend bridge is a direct HTTP proxy to `ADAOS_BASE`/`X-AdaOS-Base`, which is not a reliable route from the public root service back to a local hub.

Resolution:

- Local SDK `get_local_*` Root MCP calls now mark local target contexts and use embedded local registry/session/token/audit operations first.
- The remote bridge fallback remains available for explicit non-local Root MCP usage and for resilience when local-first is disabled.

Actions:

- [x] Add `ADAOS_ROOT_MCP_LOCAL_FIRST` with local-first enabled by default.
- [x] Keep `ADAOS_ROOT_MCP_LOCAL_FIRST=0` as an escape hatch for explicit bridge validation.
- [x] Add a regression test proving local runtime calls do not probe the bridge.
- [x] Confirm final 180-second soak has `root_mcp_fetch_failed=0` and `embedded_fallback=0`.

#### F3M-007: First-3-minute memory footprint

Status: closed for the current 3-minute goal.

Evidence:

- User requested memory state as part of the final loading evaluation.
- A naive first sampler captured only a launcher stub; the final accepted sampler measures the whole process tree and the heaviest child process.

Resolution:

- The final accepted run sampled the `adaos api serve` process tree during loading-to-ready and throughout the 180-second soak.
- Memory reached a startup plateau and stayed bounded: process-tree peak PrivateMemory was 230.555 MB and the last sample was 228.117 MB.
- Repeat final acceptance with browser `/ws` attach stayed bounded as well: process-tree peak PrivateMemory was 145.211 MB and the last sample was 145.211 MB.

Actions:

- [x] Add process-tree memory sampling to the final soak verification.
- [x] Capture first, ready, peak, and final memory samples.
- [x] Confirm peak and final memory values are in the same plateau range.
- [x] Confirm no memory-related traceback, supervisor failure, or event-loop lag appears in the final accepted run.

### Operating Checklist

Before a 3-minute soak:

- [ ] `ADAOS_WIN_SELECTOR_LOOP=0`.
- [ ] `HUB_NATS_WS_DIAG_FILE=.adaos/diagnostics/nats_ws_diag.jsonl`.
- [ ] `HUB_ROOT_LOG_SNAPSHOT=1`.
- [ ] Capture process-tree memory samples during loading-to-ready and final acceptance runs.
- [ ] Deep trace is off unless investigating one focused case.

During analysis:

- [ ] Start with `.adaos/logs/adaos.log`.
- [ ] Use `.adaos/diagnostics/nats_ws_diag.jsonl` to distinguish RX silence from local backpressure.
- [ ] Use `.adaos/root_log_snapshots/*__extract.log` to correlate root route timeout keyTags.
- [ ] Only request full terminal output when the local logs are missing the incident window.

### Post-Goal Follow-Ups

The current local runtime goal is complete. Keep these follow-ups in the issue
tracker so they are not lost:

- [ ] If public remote Root MCP access to local hubs is required, design a hub-routed backend/infra bridge rather than a direct upstream HTTP proxy.
- [ ] If any future 180-second run reopens NATS/route/Yjs/loop symptoms, add a new task under this same goal with the run id and exact log evidence.
