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

Overall completion: 95% for the expanded local + root-routed browser goal.

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
- Local API serve disables WebSocket per-message deflate to avoid CPU-heavy compression during root-routed Yjs first-sync bursts.
- `/api/node/reliability` and `/api/node/reliability/summary` build reliability payloads off the event loop, so browser polling no longer runs `load_config()` / runtime-state filesystem checks on the loop thread.

In progress:

- Validate root-routed browser stability while both local and remote browsers are connected.
- Deploy/verify backend WS-NATS keepalive default for public root `/nats` tunnels.

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
- `root_remote_browser_20260429_0753Z`: live local + root-routed browser load reopened the goal. Local browser remained usable, but the remote browser repeatedly reconnected through root. Evidence: root reverse-proxy accepted `/hubs/sn_6acf0c01/yws/desktop` with `101`, then nginx emitted repeated `SSL_read() failed ... bad record mac` on keepalive/upgraded paths; backend `ws-nats-proxy` reported `/nats` close `1006` with `natsKeepalivesSent=0`, `lastClientPongAgo_s=67.0`, and only one client ping; hub-side `nats_ws_diag.jsonl` showed `pending_data_size=0` while `last_rx_ago_s` grew above 300s and `ka_pings_rx=1`. Conclusion: this is not local pending-queue starvation; the root WS-NATS tunnel lacks regular hub<->root application-level liveness traffic under remote browser load.
- `root_remote_after_summary_offload_20260429_111439`: 3+ minute local + root-routed browser diagnostic after disabling API WebSocket compression and offloading reliability summary generation. Counts in the verification window: NATS recv failure/ConnectionClosedError/WinError 10054/watchdog `_reading_task` = 0, event loop lag = 0, control-lifecycle warning stack = 0, `node_reliability_summary` / `current_reliability_payload` warning stack = 0. Expected signals only: one `nats bridge connected`, one `yws connection open`, one `yws connection closed` during requested shutdown, and one NATS disconnect during requested shutdown. Caveat: this run used local raw NATS keepalive diagnostics; public backend keepalive deployment still needs final root-side confirmation.

### Tasks

#### F3M-001: NATS-over-WS disconnects after 25-60 seconds

Status: reopened for root-routed browser load.

Evidence:

- `nats ws recv failed ... ConnectionClosedError: no close frame received or sent code=1006`
- `ConnectionResetError: [WinError 10054]`
- watchdog reports `_reading_task terminated`.
- `nats_ws_diag.jsonl` shows `last_rx_ago_s` and `last_ping_rx_ago_s` growing before disconnect while `pending_data_size` stays near zero.
- Under remote browser load, backend `ws-nats-proxy` close diagnostics show `code=1006`, `natsKeepalivesSent=0`, and long `lastClientPongAgo_s` / `lastClientPingAgo_s`, while root-routed `/yws` requests are repeatedly accepted with `101`.

Working hypothesis:

- The disconnect is not caused by local NATS pending queue starvation.
- The likely cause is upstream/proxy/socket idle handling, Windows event loop behavior, or reconnect supersede overlap.
- For the root-routed browser case, the strongest current cause is missing proxy-to-hub NATS data keepalive: the backend has `nats keepalive -> client` support, but it was disabled by default, leaving quiet `/nats` tunnels dependent on incidental route traffic.

Actions:

- [x] Log structured close/error diagnostics from the Python WS transport.
- [x] Keep client-side NATS ping interval task disabled for WS transport by default.
- [x] Add backend WS/NATS proxy keepalive and supersede diagnostics.
- [x] Make Windows Selector loop an explicit diagnostic mode only.
- [x] Run multiple 180-second soaks with `ADAOS_WIN_SELECTOR_LOOP=0`.
- [x] Confirm the current loop is `WindowsProactorEventLoopPolicy` / `ProactorEventLoop`.
- [x] Confirm no local pending-data backpressure during the verified 180-second runs.
- [x] Reopen after root-routed browser load captured repeated remote reconnects and quiet NATS WS diagnostics.
- [x] Compare root proxy upstream ping/pong cadence against client-side `last_rx_ago_s`.
- [x] Enable backend WS-NATS `nats keepalive -> client` by default, still overridable with `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`.
- [ ] Deploy backend keepalive default to root.
- [ ] Re-run 180-second local + root-routed browser soak and confirm `natsKeepalivesSent>0`, recurring client PONGs, no NATS watchdog reconnect, and no remote yws close loop.
- [x] Run a local diagnostic with raw NATS keepalive and both browsers connected; confirm no NATS watchdog reconnect or remote YWS close loop before requested shutdown.
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

#### F3M-008: Remote root-routed Yjs attach closes under browser load

Status: partially verified; backend deploy verification remains.

Evidence:

- With a local browser and a root-routed remote browser connected at the same time, the remote browser repeatedly hit `connection closed` while local access remained usable.
- Root reverse-proxy accepted remote `/hubs/sn_6acf0c01/yws/desktop` upgrades with `101`, then emitted repeated `SSL_read() failed ... bad record mac` around keepalive/upgraded traffic.
- Hub logs showed `yws connection closed webspace=desktop` around the same window as NATS WS reconnects.
- Control lifecycle delay stacks under load pointed at WebSocket/Yjs send/write paths, including expensive websocket compression and Yjs load-mark history append.

Working hypothesis:

- The primary remote disconnect is downstream of root route/NATS liveness, not local browser failure.
- Large first-sync Yjs bursts should avoid WebSocket compression and avoid avoidable synchronous diagnostics on the event loop.

Actions:

- [x] Disable local uvicorn WebSocket per-message deflate for API serve.
- [x] Re-run a 3+ minute local + root-routed browser diagnostic with local raw NATS keepalive and confirm no NATS watchdog reconnect, no unexpected YWS close, and no compression-related control-lifecycle warning stack before requested shutdown.
- [ ] Re-run root-routed browser soak after backend keepalive deployment.
- [ ] Confirm no `permessage_deflate.encode` control-lifecycle delay stack recurs.
- [ ] If load-mark history append still appears in loop-delay stacks, move history append off the event loop or batch it under diagnostics-only mode.

#### F3M-009: Reliability summary polling blocks the event loop

Status: closed for the current 3-minute goal.

Evidence:

- While local and root-routed browsers were connected, repeated client polling of `/api/node/reliability/summary` produced a control-lifecycle warning stack through `node_reliability_summary -> current_reliability_payload -> load_config -> runtime_state_mtime_ns -> Path.resolve`.
- The endpoint response is relatively large and mostly stable, so the long-term architecture should move toward a reusable status plane with thin monitoring deltas.

Resolution:

- `/api/node/reliability` and `/api/node/reliability/summary` now build the reliability payload in an AnyIO worker thread.
- The follow-up architecture work is tracked under `Reusable Status Plane And Thin Monitoring`.

Actions:

- [x] Move reliability payload construction for the high-frequency HTTP endpoints off the event loop.
- [x] Update the isolated reliability endpoint test double so it reflects current bootstrap imports.
- [x] Verify targeted reliability endpoint tests pass.
- [x] Re-run a 3+ minute local + root-routed browser diagnostic and confirm no `node_reliability_summary` / `current_reliability_payload` warning stack recurs.

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

## Reusable Status Plane And Thin Monitoring

### Goal

Replace high-frequency polling of large monitoring payloads with a reusable
status-plane architecture that core services and skills can both feed.

Success means:

- The client no longer polls large mostly-static payloads such as
  `/api/node/reliability/summary` for badge/status UI.
- Core services and skills can publish small versioned status cards through one
  common SDK/service contract.
- Heavy diagnostic data stays behind lazy streams, explicit details requests, or
  debug-only full snapshots.
- `infrastate_skill` and `infrascope_skill` use the same reusable status
  projection pattern instead of maintaining parallel ad-hoc debounce,
  fingerprint, stream snapshot, and last-good-cache logic.
- Existing UI views remain compatible during migration through thin summary
  endpoints and backward-compatible stream receivers.

### Current Status

Snapshot date: 2026-04-29.

Overall completion: 0% for the new status-plane goal.

Problem statement:

- The client periodically requests
  `http://127.0.0.1:8777/api/node/reliability/summary`.
- The response is large, while most values are unchanged between requests.
- This creates unnecessary local CPU/serialization work, route traffic, and
  diagnostic noise during the realtime startup window.
- `infrastate_skill` and `infrascope_skill` already demonstrate the desired
  split: compact Yjs state plus heavy webio stream receivers, but each skill
  implements its own projection helpers.

Design direction:

- Treat monitoring as a materialized status plane, not as repeated full
  snapshot construction.
- Use small status cards for hot UI state, stream receivers for warm/cold
  details, and explicit debug endpoints/tools for raw full diagnostics.
- Make the pattern reusable for current and future skills.

### Tasks

#### STATUS-001: Define the shared status card contract

Status: planned.

Target shape:

- A status card has stable identity: `id`, `owner`, `kind`, `scope`, and
  optional `webspace_id`.
- A status card has operator-facing state: `status`, `summary`, `severity`,
  `updated_at`, `ttl_ms`, and optional `incident_id`.
- A status card has change tracking: `version`, `fingerprint`, and
  `changed_at`.
- A status card can point to details without embedding them:
  `details_ref.kind`, `details_ref.receiver`, `details_ref.path`, or
  `details_ref.tool`.

Actions:

- [ ] Define status values and normalization rules shared with
  `CanonicalStatus`.
- [ ] Define JSON schema or typed dataclass for status cards.
- [ ] Define staleness semantics when `ttl_ms` expires.
- [ ] Define how cards map to incidents and active warnings.
- [ ] Document examples for core, `infrastate_skill`, `infrascope_skill`, and a
  future third-party skill.

#### STATUS-002: Add a materialized status registry/service

Status: planned.

Expected behavior:

- Producers publish small cards into an in-memory materialized registry.
- The registry deduplicates unchanged cards by fingerprint.
- The registry increments versions only on meaningful changes.
- The registry exposes cheap reads for thin UI summaries.
- The registry emits changed events for stream/push consumers.

Actions:

- [ ] Add a core status registry service.
- [ ] Add per-card fingerprinting that ignores volatile fields such as
  `updated_at`, `_age_s`, and `_ago_s`.
- [ ] Add TTL/staleness sweep.
- [ ] Add compact registry diagnostics: card count, changed count, stale count,
  and last publish latency.
- [ ] Add unit tests for dedupe, versioning, TTL expiry, and owner scoping.

#### STATUS-003: Add skill-facing SDK helpers

Status: planned.

Expected API:

- `publish_status(...)` publishes one card.
- `publish_status_many(...)` publishes a small batch.
- `publish_status_stream(...)` binds a card to an existing webio stream receiver.
- Helpers normalize status tokens, compute fingerprints, and preserve
  skill/handler ownership.

Actions:

- [ ] Add `adaos.sdk.status` or equivalent SDK module.
- [ ] Preserve current skill identity in status ownership metadata.
- [ ] Provide helpers for `details_ref` pointing to webio stream receivers.
- [ ] Add tests showing a skill can publish status without touching Yjs or
  rebuilding a full snapshot.
- [ ] Add migration notes for skill authors.

#### STATUS-004: Convert `infrastate_skill` to the shared status plane

Status: planned.

Current useful pattern:

- Compact durable UI data is projected into `infrastate.snapshot`.
- High-churn sections use stream receivers such as
  `infrastate.operations.active`, `infrastate.realtime`,
  `infrastate.yjs.load_mark`, and `infrastate.core_update_diagnostics`.
- Projection helpers already perform fingerprinting and rate limiting, but the
  logic is local to the skill.

Actions:

- [ ] Identify `infrastate` status cards: runtime, route/realtime, Yjs,
  operations, core update, marketplace, and skill/scenario registry.
- [ ] Publish those cards through the shared SDK helpers.
- [ ] Keep existing stream receivers as `details_ref` targets.
- [ ] Remove or reduce duplicated local projection bookkeeping where the shared
  helper covers it.
- [ ] Confirm existing `infrastate` UI still receives current streams.
- [ ] Add regression tests around unchanged snapshot/card dedupe.

#### STATUS-005: Convert `infrascope_skill` to the shared status plane

Status: planned.

Current useful pattern:

- Compact durable UI data is projected into `infrascope.snapshot`.
- High-churn and large sections use receivers such as
  `infrascope.overview.*`, `infrascope.inventory.*`,
  `infrascope.operations.active`, and `infrascope.inspector.*`.
- It already maintains last-good snapshots and per-webspace projection
  fingerprints locally.

Actions:

- [ ] Identify `infrascope` status cards: overview, active incidents,
  inventory, browser/runtime state, registry, and operations.
- [ ] Publish cards through the shared SDK helpers.
- [ ] Keep overview/inventory/inspector streams as details targets.
- [ ] Ensure inspector data stays lazy and is not embedded in status cards.
- [ ] Add tests proving the overview badge can update without full inventory
  reconstruction.

#### STATUS-006: Make `/api/node/reliability/summary` thin and versioned

Status: planned.

Expected behavior:

- Default response is small and backed by the materialized status registry.
- Full diagnostic snapshot requires `?full=1` or a separate debug endpoint.
- The endpoint supports ETag or explicit version checks.
- Unchanged polling returns `304 Not Modified` or a minimal unchanged response.

Actions:

- [ ] Measure current response size and polling frequency.
- [ ] Add `mode=thin` or make thin mode the default with a compatibility flag
  for full mode.
- [ ] Add `ETag` / `If-None-Match` support or `since_version`.
- [ ] Keep a migration-safe full snapshot path for existing debug tools.
- [ ] Add tests for unchanged response behavior and full-mode compatibility.

#### STATUS-007: Move client monitoring from polling to push/delta

Status: planned.

Expected behavior:

- Client bootstraps from a small status snapshot.
- Client receives status changes through a stream or existing realtime channel.
- Client requests full details only when a panel/inspector is opened.

Actions:

- [ ] Identify the current caller(s) polling
  `/api/node/reliability/summary`.
- [ ] Replace badge/status polling with thin status snapshot plus updates.
- [ ] Wire existing webio stream receivers as lazy detail sources.
- [ ] Add client-side cache keyed by status card version.
- [ ] Verify the client no longer requests large summary payloads repeatedly
  during the first 3 minutes.

#### STATUS-008: Acceptance and observability

Status: planned.

Acceptance criteria:

- Repeated first-3-minute run shows no high-frequency large
  `/api/node/reliability/summary` responses.
- Thin status payload size is bounded and recorded.
- Full details remain available on demand.
- `infrastate_skill` and `infrascope_skill` both publish status cards through
  the shared path.
- Existing realtime stability criteria from `Realtime First 3 Minutes` remain
  green.

Actions:

- [ ] Add log/metric for reliability summary mode, response bytes, and
  unchanged/304 counts.
- [ ] Add status registry diagnostics to the final soak analysis.
- [ ] Run a 180-second acceptance with browser attached.
- [ ] Record payload size reduction and polling reduction in this tracker.
- [ ] Close this goal only after logs confirm no large repeated monitoring
  responses during normal UI operation.
