from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from adaos.services.hub_root_protocol_store import protocol_streams_snapshot


class MessageTaxonomy(str, Enum):
    COMMAND = "command"
    REQUEST = "request"
    RESPONSE = "response"
    STATE_REPORT = "state_report"
    EVENT = "event"
    SYNC_UPDATE = "sync_update"
    PRESENCE = "presence"
    ROUTE_FRAME = "route_frame"
    MEDIA_FRAME = "media_frame"


class DeliveryClass(str, Enum):
    MUST_NOT_LOSE = "must_not_lose"
    NICE_TO_REPLAY = "nice_to_replay"
    DROP_ALLOWED = "drop_allowed"


class ChannelType(str, Enum):
    COMMAND = "command_channel"
    EVENT = "event_channel"
    SYNC = "sync_channel"
    PRESENCE = "presence_channel"
    ROUTE = "route_channel"
    MEDIA = "media_channel"


class Authority(str, Enum):
    ROOT = "root"
    HUB = "hub"
    MEMBER_BROWSER = "member_browser"
    SIDECAR = "sidecar"
    SHARED = "shared"


class ReadinessStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class FlowSpec:
    flow_id: str
    channel_type: ChannelType
    message_types: tuple[MessageTaxonomy, ...]
    delivery_class: DeliveryClass
    authority: Authority
    ordered: bool
    durable: bool
    replayable: bool
    current_paths: tuple[str, ...]
    description: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "channel_type": self.channel_type.value,
            "message_types": [item.value for item in self.message_types],
            "delivery_class": self.delivery_class.value,
            "authority": self.authority.value,
            "ordered": self.ordered,
            "durable": self.durable,
            "replayable": self.replayable,
            "current_paths": list(self.current_paths),
            "description": self.description,
            "notes": self.notes,
        }


HUB_ROOT_FLOW_SPECS: tuple[FlowSpec, ...] = (
    FlowSpec(
        flow_id="hub_root.control.lifecycle",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.STATE_REPORT),
        delivery_class=DeliveryClass.MUST_NOT_LOSE,
        authority=Authority.SHARED,
        ordered=True,
        durable=True,
        replayable=True,
        current_paths=("mtls_http:/hub/nats/token", "nats_ws", "subnet.nats.up/down"),
        description="Hub-root control session establishment and lifecycle state exchange.",
        notes="Critical control-plane flow. Requires explicit idempotency and resume semantics.",
    ),
    FlowSpec(
        flow_id="hub_root.route.control",
        channel_type=ChannelType.ROUTE,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.EVENT),
        delivery_class=DeliveryClass.NICE_TO_REPLAY,
        authority=Authority.ROOT,
        ordered=True,
        durable=False,
        replayable=True,
        current_paths=("nats:route.v2.to_hub.<hubId>.*",),
        description="Route-install and browser<->hub relay control traffic crossing root.",
        notes="Control metadata for route proxy. Must not share a pressure domain with core control acks.",
    ),
    FlowSpec(
        flow_id="hub_root.route.frame",
        channel_type=ChannelType.ROUTE,
        message_types=(MessageTaxonomy.ROUTE_FRAME,),
        delivery_class=DeliveryClass.DROP_ALLOWED,
        authority=Authority.SHARED,
        ordered=True,
        durable=False,
        replayable=False,
        current_paths=("nats:route.v2.to_hub.<hubId>.*", "nats:route.v2.to_browser.<hubId>.*"),
        description="Relay frames for proxied HTTP/WS browser traffic.",
        notes="Wrapped logical flow defines higher-level semantics; route frames themselves are not durable.",
    ),
    FlowSpec(
        flow_id="hub_root.integration.telegram",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.COMMAND, MessageTaxonomy.EVENT),
        delivery_class=DeliveryClass.MUST_NOT_LOSE,
        authority=Authority.ROOT,
        ordered=False,
        durable=True,
        replayable=True,
        current_paths=("root_http", "root_nats_bridge"),
        description="Root-backed Telegram actions and related integration state transitions.",
        notes="Retries require stable operation keys to avoid duplicate user-visible sends.",
    ),
    FlowSpec(
        flow_id="hub_root.integration.github_core_update",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.REQUEST, MessageTaxonomy.RESPONSE, MessageTaxonomy.STATE_REPORT),
        delivery_class=DeliveryClass.MUST_NOT_LOSE,
        authority=Authority.ROOT,
        ordered=False,
        durable=True,
        replayable=True,
        current_paths=("root_http:/hub/core_update/*", "root_state"),
        description="Core update coordination and release/report exchange through root.",
        notes="Drives update orchestration and hub report persistence.",
    ),
    FlowSpec(
        flow_id="hub_root.integration.llm",
        channel_type=ChannelType.COMMAND,
        message_types=(MessageTaxonomy.REQUEST, MessageTaxonomy.RESPONSE, MessageTaxonomy.STATE_REPORT),
        delivery_class=DeliveryClass.NICE_TO_REPLAY,
        authority=Authority.ROOT,
        ordered=False,
        durable=False,
        replayable=True,
        current_paths=("root_http:/v1/llm/models", "root_http:/v1/llm/response"),
        description="Root-backed LLM model discovery and completion requests.",
        notes="Interactive LLM completions depend on root reachability but do not require durable transport replay.",
    ),
    FlowSpec(
        flow_id="hub_member.sync.yjs",
        channel_type=ChannelType.SYNC,
        message_types=(MessageTaxonomy.SYNC_UPDATE,),
        delivery_class=DeliveryClass.NICE_TO_REPLAY,
        authority=Authority.HUB,
        ordered=False,
        durable=True,
        replayable=True,
        current_paths=("yws", "webrtc_data:yjs", "member_link_ws"),
        description="Yjs sync as a transport-independent sync channel.",
        notes="Backed by snapshot/diff and bounded replay, not by transport-specific semantics alone.",
    ),
    FlowSpec(
        flow_id="hub_member.presence",
        channel_type=ChannelType.PRESENCE,
        message_types=(MessageTaxonomy.PRESENCE,),
        delivery_class=DeliveryClass.DROP_ALLOWED,
        authority=Authority.MEMBER_BROWSER,
        ordered=False,
        durable=False,
        replayable=False,
        current_paths=("ws", "webrtc_data", "root_route_proxy"),
        description="Awareness and ephemeral session hints for member/browser clients.",
        notes="Explicitly ephemeral. Never escalated into a durable control bus.",
    ),
)


AUTHORITY_BOUNDARIES: dict[str, Any] = {
    "root": {
        "owns": [
            "hub registration and identity validation",
            "hub NATS session issuance",
            "root-backed owner authentication",
            "cross-subnet coordination",
            "root-routed external integrations",
            "release and update coordination across hubs",
        ],
        "does_not_own": [
            "local hub execution state",
            "local skill runtime internals",
            "local Yjs in-memory session ownership",
        ],
    },
    "hub": {
        "owns": [
            "local skill and scenario execution",
            "local event bus",
            "local webspace and Yjs persistence",
            "admitted member/browser session handling",
            "local degraded-mode execution policy",
        ],
        "does_not_own": [
            "minting fresh root-backed trust",
            "claiming root integration delivery without acknowledgement",
            "global cross-subnet truth",
        ],
    },
    "member_browser": {
        "owns": [
            "local ephemeral session state",
            "local cached sync state",
            "local media device state",
        ],
        "does_not_own": [
            "shared durable control state",
            "global routing authority",
            "root-issued trust state",
        ],
    },
    "sidecar": {
        "may_own": [
            "transport lifecycle",
            "socket diagnostics",
            "reconnect loops",
            "local relay io",
        ],
        "must_not_own": [
            "command semantics",
            "idempotency rules",
            "durable cursor semantics",
            "degraded-mode business policy",
        ],
    },
}


@dataclass(slots=True)
class RuntimeSignal:
    status: ReadinessStatus = ReadinessStatus.UNKNOWN
    summary: str = ""
    updated_at: float = 0.0
    observed: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "updated_at": self.updated_at or None,
            "observed": self.observed,
            "details": dict(self.details or {}),
        }


_LOCK = threading.RLock()
_INTEGRATION_NAMES = ("telegram", "github", "llm")
_CHANNEL_NAMES = ("root_control", "route")
_CHANNEL_HISTORY_LIMIT = 128
_TRANSPORT_HISTORY_LIMIT = 64
_UNSET = object()
_ROOT_CONTROL = RuntimeSignal()
_ROUTE = RuntimeSignal()
_INTEGRATIONS: dict[str, RuntimeSignal] = {name: RuntimeSignal() for name in _INTEGRATION_NAMES}
_CHANNEL_HISTORY: dict[str, deque[dict[str, Any]]] = {
    name: deque(maxlen=_CHANNEL_HISTORY_LIMIT) for name in _CHANNEL_NAMES
}
_HUB_ROOT_TRANSPORT_STATE: dict[str, Any] = {
    "requested_transport": None,
    "effective_transport": None,
    "selected_server": None,
    "url_override": None,
    "current_ws_tag": None,
    "last_event": "",
    "last_error": "",
    "last_summary": "",
    "attempt_seq": 0,
    "last_attempt_at": 0.0,
    "last_connected_at": 0.0,
    "last_failure_at": 0.0,
    "candidates": [],
    "failover_policy": {},
    "hypothesis": {},
    "updated_at": 0.0,
}
_HUB_ROOT_TRANSPORT_HISTORY: deque[dict[str, Any]] = deque(maxlen=_TRANSPORT_HISTORY_LIMIT)

_HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES = ("control", "integration", "route", "sync_metadata")
_HUB_ROOT_PROTOCOL_CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "control": {
        "priority": "highest",
        "ack_policy": "required",
        "replay": "bounded",
        "idempotency": "strict",
        "drop_policy": "never",
        "worker_budget": 1,
        "pending_msgs_limit": 256,
        "pending_bytes_limit": 8 * 1024 * 1024,
        "stale_authority_after_s": 30,
    },
    "integration": {
        "priority": "medium",
        "ack_policy": "integration_specific",
        "replay": "selected_flows_only",
        "idempotency": "operation_key",
        "drop_policy": "buffer_then_drop_oldest",
        "worker_budget": 1,
        "pending_msgs_limit": 1024,
        "pending_bytes_limit": 16 * 1024 * 1024,
        "stale_authority_after_s": 120,
    },
    "route": {
        "priority": "lower_than_control",
        "ack_policy": "request_reply_only",
        "replay": "session_bounded",
        "idempotency": "session_scoped",
        "drop_policy": "slow_consumer_backpressure",
        "worker_budget": 1,
        "pending_msgs_limit": 4096,
        "pending_bytes_limit": 64 * 1024 * 1024,
        "stale_authority_after_s": 45,
    },
    "sync_metadata": {
        "priority": "below_control",
        "ack_policy": "negotiation_specific",
        "replay": "bounded",
        "idempotency": "cursor_scoped",
        "drop_policy": "drop_oldest_noncritical",
        "worker_budget": 1,
        "pending_msgs_limit": 512,
        "pending_bytes_limit": 8 * 1024 * 1024,
        "stale_authority_after_s": 60,
    },
}


def _protocol_env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def hub_root_protocol_class_policy(traffic_class: str) -> dict[str, Any]:
    key = str(traffic_class or "").strip().lower()
    defaults = _HUB_ROOT_PROTOCOL_CLASS_DEFAULTS.get(key)
    if defaults is None:
        raise ValueError(f"unsupported hub-root traffic class: {traffic_class!r}")
    prefix = f"HUB_PROTOCOL_{key.upper()}"
    return {
        **defaults,
        "traffic_class": key,
        "pending_msgs_limit": _protocol_env_int(
            f"{prefix}_PENDING_MSGS_LIMIT",
            int(defaults.get("pending_msgs_limit") or 0),
            minimum=1,
        ),
        "pending_bytes_limit": _protocol_env_int(
            f"{prefix}_PENDING_BYTES_LIMIT",
            int(defaults.get("pending_bytes_limit") or 0),
            minimum=1024,
        ),
        "stale_authority_after_s": _protocol_env_int(
            f"{prefix}_STALE_AUTHORITY_AFTER_S",
            int(defaults.get("stale_authority_after_s") or 0),
            minimum=1,
        ),
        "worker_budget": _protocol_env_int(
            f"{prefix}_WORKER_BUDGET",
            int(defaults.get("worker_budget") or 1),
            minimum=1,
        ),
    }


def hub_root_protocol_traffic_class(subject: str) -> str:
    subj = str(subject or "").strip().lower()
    if subj.startswith("hub.control."):
        return "control"
    if subj.startswith("route."):
        return "route"
    if subj.startswith("tg.input.") or subj.startswith("tg.output.") or subj.startswith("io.tg.in."):
        return "integration"
    if subj.startswith("sync.") or subj.startswith("cursor.") or subj.startswith("ystate."):
        return "sync_metadata"
    return "integration"


def _new_protocol_traffic_class_state(name: str) -> dict[str, Any]:
    return {
        "traffic_class": name,
        "policy": hub_root_protocol_class_policy(name),
        "active_subscriptions": 0,
        "subjects": [],
        "dispatch_count": 0,
        "publish_ok": 0,
        "publish_fail": 0,
        "handler_errors": 0,
        "pressure_events": 0,
        "last_dispatch_at": 0.0,
        "last_publish_at": 0.0,
        "last_error_at": 0.0,
        "last_error": "",
        "last_qsize": None,
        "max_qsize": 0,
        "last_pending_bytes": None,
        "max_pending_bytes": 0,
        "last_message_bytes": None,
    }


def _new_protocol_runtime() -> dict[str, Any]:
    return {
        "traffic_classes": {
            name: _new_protocol_traffic_class_state(name)
            for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES
        },
        "subscriptions": {},
        "route_runtime": {
            "active_tunnels": 0,
            "active_reader_tasks": 0,
            "pending_tunnels": 0,
            "pending_events": 0,
            "pending_chunks": 0,
            "max_pending_events": 0,
            "no_upstream_close_after_s": None,
            "legacy_v1_enabled": False,
            "v2_enabled": False,
            "last_force_close_at": 0.0,
            "last_no_upstream_at": 0.0,
            "last_publish_fail_at": 0.0,
        },
        "integration_outboxes": {
            "telegram": {
                "name": "telegram",
                "size": 0,
                "max_size": None,
                "drained_total": 0,
                "dropped_total": 0,
                "publish_ok": 0,
                "publish_fail": 0,
                "connected": None,
                "last_error": "",
                "last_error_at": 0.0,
                "updated_at": 0.0,
            }
        },
        "streams": {},
        "updated_at": 0.0,
    }


_HUB_ROOT_PROTOCOL_RUNTIME: dict[str, Any] = _new_protocol_runtime()


def _copy_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dedup_texts(items: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, (list, tuple)):
        return result
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _read_last_jsonl_record(path: Path, *, max_bytes: int = 131072) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in reversed(chunk.splitlines()):
        text = str(line or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _hub_root_transport_from_server(server: str | None, *, explicit_transport: str | None = None) -> str | None:
    explicit = str(explicit_transport or "").strip().lower()
    if explicit:
        return explicit
    text = str(server or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
        scheme = str(parsed.scheme or "").strip().lower()
    except Exception:
        scheme = ""
    if scheme in {"ws", "wss"}:
        return "ws"
    if scheme in {"nats", "tls"}:
        return "tcp"
    if scheme in {"http", "https"}:
        return "sidecar"
    return None


def configure_hub_root_transport_strategy(
    *,
    requested_transport: Any = _UNSET,
    effective_transport: Any = _UNSET,
    selected_server: Any = _UNSET,
    url_override: Any = _UNSET,
    current_ws_tag: Any = _UNSET,
    candidates: Any = _UNSET,
    failover_policy: Any = _UNSET,
    hypothesis: Any = _UNSET,
) -> None:
    with _LOCK:
        state = _HUB_ROOT_TRANSPORT_STATE
        if requested_transport is not _UNSET:
            state["requested_transport"] = str(requested_transport or "").strip().lower() or None
        if effective_transport is not _UNSET or selected_server is not _UNSET:
            state["effective_transport"] = _hub_root_transport_from_server(
                selected_server if selected_server is not _UNSET else state.get("selected_server"),
                explicit_transport=effective_transport if effective_transport is not _UNSET else None,
            )
        if selected_server is not _UNSET:
            state["selected_server"] = str(selected_server or "").strip() or None
        if url_override is not _UNSET:
            state["url_override"] = str(url_override or "").strip() or None
        if current_ws_tag is not _UNSET:
            state["current_ws_tag"] = str(current_ws_tag or "").strip() or None
        if candidates is not _UNSET:
            state["candidates"] = _dedup_texts(candidates)
        if failover_policy is not _UNSET:
            state["failover_policy"] = _copy_dict(failover_policy)
        if hypothesis is not _UNSET:
            state["hypothesis"] = _copy_dict(hypothesis)
        state["updated_at"] = time.time()


def record_hub_root_transport_event(
    event: str,
    *,
    transport: str | None = None,
    server: str | None = None,
    summary: str = "",
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    evt = str(event or "").strip().lower() or "event"
    srv = str(server or "").strip() or None
    tr = _hub_root_transport_from_server(srv, explicit_transport=transport)
    err = str(error or "").strip() or None
    ts = time.time()
    record = {
        "ts": ts,
        "event": evt,
        "transport": tr,
        "server": srv,
        "summary": str(summary or ""),
        "error": err,
        "details": dict(details or {}),
    }
    with _LOCK:
        state = _HUB_ROOT_TRANSPORT_STATE
        if tr:
            state["effective_transport"] = tr
        if srv:
            state["selected_server"] = srv
        state["last_event"] = evt
        state["last_summary"] = str(summary or "")
        if err:
            state["last_error"] = err
        if evt in {"attempt", "connect_try", "reconnect_requested"}:
            state["attempt_seq"] = int(state.get("attempt_seq") or 0) + 1
            state["last_attempt_at"] = ts
        if evt in {"connected", "ready", "reconnected"}:
            state["last_connected_at"] = ts
            state["last_error"] = ""
        if evt in {"connect_failed", "down", "disconnected", "watchdog_error", "supervisor_error", "reader_terminated"}:
            state["last_failure_at"] = ts
        state["updated_at"] = ts
        _HUB_ROOT_TRANSPORT_HISTORY.append(record)


def _hub_root_transport_assessment(history: list[dict[str, Any]], *, now_ts: float) -> dict[str, Any]:
    failure_events = {
        "connect_failed",
        "down",
        "disconnected",
        "watchdog_error",
        "supervisor_error",
        "reader_terminated",
    }
    connect_events = {"connected", "ready", "reconnected"}
    threshold_5m = now_ts - 300.0
    threshold_15m = now_ts - 900.0
    failures_5m = 0
    failures_15m = 0
    connects_15m = 0
    attempts_15m = 0
    transports_15m: list[str] = []
    last_event = ""
    last_failure_at: float | None = None
    last_connected_at: float | None = None
    for item in history:
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0.0:
            continue
        event = str(item.get("event") or "").strip().lower()
        if ts >= threshold_15m:
            if event in {"attempt", "connect_try", "reconnect_requested"}:
                attempts_15m += 1
            if event in connect_events:
                connects_15m += 1
            if event in failure_events:
                failures_15m += 1
            transport = str(item.get("transport") or "").strip().lower()
            if transport:
                transports_15m.append(transport)
        if ts >= threshold_5m and event in failure_events:
            failures_5m += 1
        if event in connect_events:
            last_connected_at = ts
        if event in failure_events:
            last_failure_at = ts
        last_event = event or last_event

    transport_switches_15m = 0
    prev_transport = ""
    for transport in transports_15m:
        if not transport:
            continue
        if prev_transport and transport != prev_transport:
            transport_switches_15m += 1
        prev_transport = transport

    last_failure_ago_s = _round_age(now_ts, last_failure_at)
    state = "unknown"
    reason = "hub-root transport has not been observed enough yet"
    if last_failure_ago_s is not None and last_failure_ago_s <= 30.0 and (
        last_connected_at is None or (isinstance(last_failure_at, (int, float)) and last_failure_at >= float(last_connected_at or 0.0))
    ):
        state = "down"
        reason = "latest hub-root transport incident is fresh and no newer successful reconnect is recorded"
    elif failures_5m >= 2 or transport_switches_15m >= 2:
        state = "flapping"
        reason = "multiple recent transport failures or transport switches were recorded"
    elif failures_15m >= 1 or attempts_15m > max(1, connects_15m):
        state = "unstable"
        reason = "recent reconnect attempts or failures indicate an unstable hub-root transport"
    elif last_connected_at is not None:
        state = "stable"
        reason = "hub-root transport has a recent successful connect without fresh failures"

    return {
        "state": state,
        "reason": reason,
        "last_event": last_event or None,
        "failures_5m": failures_5m,
        "failures_15m": failures_15m,
        "attempts_15m": attempts_15m,
        "connects_15m": connects_15m,
        "transport_switches_15m": transport_switches_15m,
        "last_failure_at": last_failure_at,
        "last_failure_ago_s": last_failure_ago_s,
        "last_connected_at": last_connected_at,
        "last_connected_ago_s": _round_age(now_ts, last_connected_at),
    }


def hub_root_transport_strategy_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        state = {
            "requested_transport": _HUB_ROOT_TRANSPORT_STATE.get("requested_transport"),
            "effective_transport": _HUB_ROOT_TRANSPORT_STATE.get("effective_transport"),
            "selected_server": _HUB_ROOT_TRANSPORT_STATE.get("selected_server"),
            "url_override": _HUB_ROOT_TRANSPORT_STATE.get("url_override"),
            "current_ws_tag": _HUB_ROOT_TRANSPORT_STATE.get("current_ws_tag"),
            "last_event": _HUB_ROOT_TRANSPORT_STATE.get("last_event"),
            "last_error": _HUB_ROOT_TRANSPORT_STATE.get("last_error"),
            "last_summary": _HUB_ROOT_TRANSPORT_STATE.get("last_summary"),
            "attempt_seq": int(_HUB_ROOT_TRANSPORT_STATE.get("attempt_seq") or 0),
            "last_attempt_at": _HUB_ROOT_TRANSPORT_STATE.get("last_attempt_at"),
            "last_connected_at": _HUB_ROOT_TRANSPORT_STATE.get("last_connected_at"),
            "last_failure_at": _HUB_ROOT_TRANSPORT_STATE.get("last_failure_at"),
            "candidates": list(_HUB_ROOT_TRANSPORT_STATE.get("candidates") or []),
            "failover_policy": _copy_dict(_HUB_ROOT_TRANSPORT_STATE.get("failover_policy")),
            "hypothesis": _copy_dict(_HUB_ROOT_TRANSPORT_STATE.get("hypothesis")),
            "updated_at": _HUB_ROOT_TRANSPORT_STATE.get("updated_at"),
        }
        history = list(_HUB_ROOT_TRANSPORT_HISTORY)
    state["effective_transport"] = _hub_root_transport_from_server(
        state.get("selected_server"),
        explicit_transport=state.get("effective_transport"),
    )
    state["assessment"] = _hub_root_transport_assessment(history, now_ts=now)
    state["updated_ago_s"] = _round_age(now, state.get("updated_at"))
    state["last_attempt_ago_s"] = _round_age(now, state.get("last_attempt_at"))
    state["last_connected_ago_s"] = _round_age(now, state.get("last_connected_at"))
    state["last_failure_ago_s"] = _round_age(now, state.get("last_failure_at"))
    state["recent_events"] = history[-10:]
    return state


def _set_signal(
    signal: RuntimeSignal,
    *,
    status: ReadinessStatus,
    summary: str = "",
    observed: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    signal.status = status
    signal.summary = str(summary or "")
    signal.updated_at = time.time()
    signal.observed = bool(observed)
    signal.details = dict(details or {})


def _record_channel_transition(
    channel: str,
    *,
    previous_status: ReadinessStatus,
    status: ReadinessStatus,
    summary: str,
    details: dict[str, Any] | None,
) -> None:
    if previous_status == status:
        return
    history = _CHANNEL_HISTORY.setdefault(str(channel), deque(maxlen=_CHANNEL_HISTORY_LIMIT))
    history.append(
        {
            "ts": time.time(),
            "previous_status": previous_status.value,
            "status": status.value,
            "summary": str(summary or ""),
            "details": dict(details or {}),
        }
    )


def _record_channel_incident(
    channel: str,
    *,
    status: str,
    summary: str,
    details: dict[str, Any] | None,
    previous_status: str | None = None,
) -> None:
    history = _CHANNEL_HISTORY.setdefault(str(channel), deque(maxlen=_CHANNEL_HISTORY_LIMIT))
    history.append(
        {
            "ts": time.time(),
            "previous_status": str(previous_status or ""),
            "status": str(status or ""),
            "summary": str(summary or ""),
            "details": dict(details or {}),
        }
    )


def _protocol_class_state(traffic_class: str) -> dict[str, Any]:
    key = str(traffic_class or "").strip().lower()
    traffic_classes = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("traffic_classes", {})
    state = traffic_classes.get(key)
    if not isinstance(state, dict):
        state = _new_protocol_traffic_class_state(key)
        traffic_classes[key] = state
    state["policy"] = hub_root_protocol_class_policy(key)
    return state


def _protocol_refresh_subjects_locked() -> None:
    subscriptions = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("subscriptions", {})
    traffic_classes = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("traffic_classes", {})
    active_by_class: dict[str, list[str]] = {name: [] for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES}
    for subject, entry in subscriptions.items():
        if not isinstance(entry, dict):
            continue
        traffic_class = str(entry.get("traffic_class") or hub_root_protocol_traffic_class(subject))
        if bool(entry.get("active", True)):
            active_by_class.setdefault(traffic_class, []).append(str(subject))
    for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES:
        cls = traffic_classes.setdefault(name, _new_protocol_traffic_class_state(name))
        subjects = sorted(active_by_class.get(name, []))
        cls["policy"] = hub_root_protocol_class_policy(name)
        cls["subjects"] = subjects
        cls["active_subscriptions"] = len(subjects)


def observe_hub_root_protocol_subscription(
    subject: str,
    *,
    traffic_class: str | None = None,
    pending_msgs_limit: int | None = None,
    pending_bytes_limit: int | None = None,
    qsize: int | None = None,
    pending_bytes: int | None = None,
    dispatched: bool = False,
    message_bytes: int | None = None,
    handler_error: str | None = None,
    worker_done: bool | None = None,
) -> None:
    subj = str(subject or "").strip()
    if not subj:
        return
    traffic = str(traffic_class or hub_root_protocol_traffic_class(subj)).strip().lower()
    now = time.time()
    with _LOCK:
        cls = _protocol_class_state(traffic)
        entry = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("subscriptions", {}).setdefault(
            subj,
            {
                "subject": subj,
                "traffic_class": traffic,
                "active": True,
                "dispatch_count": 0,
                "handler_errors": 0,
                "last_error": "",
                "last_error_at": 0.0,
                "last_dispatch_at": 0.0,
                "last_qsize": None,
                "max_qsize": 0,
                "last_pending_bytes": None,
                "max_pending_bytes": 0,
                "pending_msgs_limit": None,
                "pending_bytes_limit": None,
                "worker_done": False,
                "updated_at": 0.0,
                "last_message_bytes": None,
            },
        )
        entry["traffic_class"] = traffic
        entry["active"] = not bool(worker_done)
        if pending_msgs_limit is not None:
            entry["pending_msgs_limit"] = int(pending_msgs_limit)
        elif entry.get("pending_msgs_limit") is None:
            entry["pending_msgs_limit"] = int(cls["policy"].get("pending_msgs_limit") or 0)
        if pending_bytes_limit is not None:
            entry["pending_bytes_limit"] = int(pending_bytes_limit)
        elif entry.get("pending_bytes_limit") is None:
            entry["pending_bytes_limit"] = int(cls["policy"].get("pending_bytes_limit") or 0)
        if qsize is not None:
            q0 = max(0, int(qsize))
            entry["last_qsize"] = q0
            entry["max_qsize"] = max(int(entry.get("max_qsize") or 0), q0)
            cls["last_qsize"] = q0
            cls["max_qsize"] = max(int(cls.get("max_qsize") or 0), q0)
            limit = int(entry.get("pending_msgs_limit") or 0)
            if limit > 0 and q0 >= limit:
                cls["pressure_events"] = int(cls.get("pressure_events") or 0) + 1
        if pending_bytes is not None:
            b0 = max(0, int(pending_bytes))
            entry["last_pending_bytes"] = b0
            entry["max_pending_bytes"] = max(int(entry.get("max_pending_bytes") or 0), b0)
            cls["last_pending_bytes"] = b0
            cls["max_pending_bytes"] = max(int(cls.get("max_pending_bytes") or 0), b0)
        if message_bytes is not None:
            entry["last_message_bytes"] = int(message_bytes)
            cls["last_message_bytes"] = int(message_bytes)
        if dispatched:
            entry["dispatch_count"] = int(entry.get("dispatch_count") or 0) + 1
            entry["last_dispatch_at"] = now
            cls["dispatch_count"] = int(cls.get("dispatch_count") or 0) + 1
            cls["last_dispatch_at"] = now
        if handler_error:
            err = str(handler_error).strip()
            entry["handler_errors"] = int(entry.get("handler_errors") or 0) + 1
            entry["last_error"] = err
            entry["last_error_at"] = now
            cls["handler_errors"] = int(cls.get("handler_errors") or 0) + 1
            cls["last_error"] = err
            cls["last_error_at"] = now
        if worker_done is not None:
            entry["worker_done"] = bool(worker_done)
        entry["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now
        _protocol_refresh_subjects_locked()


def observe_hub_root_protocol_publish(
    subject: str,
    *,
    ok: bool,
    traffic_class: str | None = None,
    payload_bytes: int | None = None,
    latency_ms: float | None = None,
    error: str | None = None,
) -> None:
    subj = str(subject or "").strip()
    if not subj:
        return
    traffic = str(traffic_class or hub_root_protocol_traffic_class(subj)).strip().lower()
    now = time.time()
    with _LOCK:
        cls = _protocol_class_state(traffic)
        if ok:
            cls["publish_ok"] = int(cls.get("publish_ok") or 0) + 1
            cls["last_publish_at"] = now
        else:
            cls["publish_fail"] = int(cls.get("publish_fail") or 0) + 1
            cls["last_error_at"] = now
            cls["last_error"] = str(error or "").strip()
        if payload_bytes is not None:
            cls["last_message_bytes"] = int(payload_bytes)
        if latency_ms is not None:
            cls["last_publish_latency_ms"] = round(float(latency_ms), 3)
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def observe_hub_root_route_runtime(**details: Any) -> None:
    if not details:
        return
    now = time.time()
    with _LOCK:
        route_runtime = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("route_runtime", {})
        for key, value in details.items():
            route_runtime[key] = value
        route_runtime["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def observe_hub_root_integration_outbox(
    name: str,
    *,
    size: int | None = None,
    max_size: int | None = None,
    drained: int | None = None,
    dropped: int | None = None,
    publish_ok: int | None = None,
    publish_fail: int | None = None,
    connected: bool | None = None,
    last_error: str | None = None,
) -> None:
    key = str(name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _LOCK:
        outboxes = _HUB_ROOT_PROTOCOL_RUNTIME.setdefault("integration_outboxes", {})
        entry = outboxes.setdefault(
            key,
            {
                "name": key,
                "size": 0,
                "max_size": None,
                "drained_total": 0,
                "dropped_total": 0,
                "publish_ok": 0,
                "publish_fail": 0,
                "connected": None,
                "last_error": "",
                "last_error_at": 0.0,
                "updated_at": 0.0,
            },
        )
        if size is not None:
            entry["size"] = max(0, int(size))
        if max_size is not None:
            entry["max_size"] = max(0, int(max_size))
        if drained is not None:
            entry["drained_total"] = int(entry.get("drained_total") or 0) + max(0, int(drained))
        if dropped is not None:
            entry["dropped_total"] = int(entry.get("dropped_total") or 0) + max(0, int(dropped))
        if publish_ok is not None:
            entry["publish_ok"] = int(entry.get("publish_ok") or 0) + max(0, int(publish_ok))
        if publish_fail is not None:
            entry["publish_fail"] = int(entry.get("publish_fail") or 0) + max(0, int(publish_fail))
        if connected is not None:
            entry["connected"] = bool(connected)
        if last_error:
            entry["last_error"] = str(last_error).strip()
            entry["last_error_at"] = now
        entry["updated_at"] = now
        _HUB_ROOT_PROTOCOL_RUNTIME["updated_at"] = now


def reset_reliability_runtime_state() -> None:
    with _LOCK:
        _set_signal(_ROOT_CONTROL, status=ReadinessStatus.UNKNOWN)
        _set_signal(_ROUTE, status=ReadinessStatus.UNKNOWN)
        for name in _INTEGRATION_NAMES:
            _set_signal(_INTEGRATIONS[name], status=ReadinessStatus.UNKNOWN)
        for name in _CHANNEL_NAMES:
            _CHANNEL_HISTORY.setdefault(name, deque(maxlen=_CHANNEL_HISTORY_LIMIT)).clear()
        _HUB_ROOT_TRANSPORT_STATE.update(
            {
                "requested_transport": None,
                "effective_transport": None,
                "selected_server": None,
                "url_override": None,
                "current_ws_tag": None,
                "last_event": "",
                "last_error": "",
                "last_summary": "",
                "attempt_seq": 0,
                "last_attempt_at": 0.0,
                "last_connected_at": 0.0,
                "last_failure_at": 0.0,
                "candidates": [],
                "failover_policy": {},
                "hypothesis": {},
                "updated_at": 0.0,
            }
        )
        _HUB_ROOT_TRANSPORT_HISTORY.clear()
        _HUB_ROOT_PROTOCOL_RUNTIME.clear()
        _HUB_ROOT_PROTOCOL_RUNTIME.update(_new_protocol_runtime())


def mark_root_control_up(*, summary: str = "hub-root control session established", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROOT_CONTROL.status
        _set_signal(
            _ROOT_CONTROL,
            status=ReadinessStatus.READY,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "root_control",
            previous_status=previous_status,
            status=ReadinessStatus.READY,
            summary=summary,
            details=details,
        )


def mark_root_control_down(*, summary: str = "hub-root control session unavailable", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROOT_CONTROL.status
        _set_signal(
            _ROOT_CONTROL,
            status=ReadinessStatus.DOWN,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "root_control",
            previous_status=previous_status,
            status=ReadinessStatus.DOWN,
            summary=summary,
            details=details,
        )
        if _ROUTE.status == ReadinessStatus.READY:
            route_previous_status = _ROUTE.status
            _set_signal(
                _ROUTE,
                status=ReadinessStatus.DEGRADED,
                summary="route path lost authority while root control is down",
                observed=True,
                details={"cause": "root_control_down"},
            )
            _record_channel_transition(
                "route",
                previous_status=route_previous_status,
                status=ReadinessStatus.DEGRADED,
                summary="route path lost authority while root control is down",
                details={"cause": "root_control_down"},
            )


def mark_route_ready(*, summary: str = "hub route relay subscription installed", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROUTE.status
        _set_signal(
            _ROUTE,
            status=ReadinessStatus.READY,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "route",
            previous_status=previous_status,
            status=ReadinessStatus.READY,
            summary=summary,
            details=details,
        )


def mark_route_degraded(*, summary: str = "hub route relay degraded", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        previous_status = _ROUTE.status
        _set_signal(
            _ROUTE,
            status=ReadinessStatus.DEGRADED,
            summary=summary,
            observed=True,
            details=details,
        )
        _record_channel_transition(
            "route",
            previous_status=previous_status,
            status=ReadinessStatus.DEGRADED,
            summary=summary,
            details=details,
        )


def note_root_control_reconnect(
    *,
    summary: str = "hub-root transport session was re-established",
    details: dict[str, Any] | None = None,
) -> None:
    with _LOCK:
        _record_channel_incident(
            "root_control",
            previous_status=_ROOT_CONTROL.status.value,
            status="reconnect",
            summary=summary,
            details=details,
        )


def note_route_incident(*, status: str, summary: str, details: dict[str, Any] | None = None) -> None:
    """Record a user-visible incident for the root relay route.

    Example: late reply for an app request, publish errors, repeated timeouts.
    """
    st = str(status or "").strip() or "incident"
    with _LOCK:
        _record_channel_incident(
            "route",
            previous_status=_ROUTE.status.value,
            status=st,
            summary=str(summary or ""),
            details=details,
        )


def observe_route_e2e(*, details: dict[str, Any]) -> None:
    """Update route E2E observations without emitting a readiness transition."""
    if not isinstance(details, dict) or not details:
        return
    with _LOCK:
        try:
            _ROUTE.details.update(dict(details))
        except Exception:
            _ROUTE.details = dict(details)
        _ROUTE.updated_at = time.time()
        _ROUTE.observed = True


def set_integration_readiness(
    name: str,
    *,
    status: ReadinessStatus,
    summary: str = "",
    observed: bool = True,
    details: dict[str, Any] | None = None,
) -> None:
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("integration name is required")
    with _LOCK:
        signal = _INTEGRATIONS.setdefault(key, RuntimeSignal())
        _set_signal(signal, status=status, summary=summary, observed=observed, details=details)


def runtime_signal_snapshot() -> dict[str, Any]:
    with _LOCK:
        return {
            "root_control": _ROOT_CONTROL.to_dict(),
            "route": _ROUTE.to_dict(),
            "integrations": {name: signal.to_dict() for name, signal in sorted(_INTEGRATIONS.items())},
        }


def effective_channel_view(
    channel_id: str,
    *,
    tree_item: dict[str, Any],
    diag_item: dict[str, Any],
    transport_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = str(tree_item.get("status") or diag_item.get("status") or ReadinessStatus.UNKNOWN.value)
    stability = diag_item.get("stability") if isinstance(diag_item.get("stability"), dict) else {}
    effective_state = str(stability.get("state") or "unknown")
    assessment = transport_assessment if isinstance(transport_assessment, dict) else {}
    transport_state = str(assessment.get("state") or "").strip().lower()
    if channel_id in {"root_control", "route"} and transport_state in {"down", "unstable", "flapping"}:
        if status == ReadinessStatus.READY.value:
            status = ReadinessStatus.DEGRADED.value
        elif status == ReadinessStatus.UNKNOWN.value and transport_state == "down":
            status = ReadinessStatus.DOWN.value
        if effective_state in {"stable", "unknown"} or transport_state == "down":
            effective_state = transport_state
    return {
        "status": status,
        "state": effective_state,
        "stability": stability,
    }


def _history_count(entries: list[dict[str, Any]], *, within_s: float, now_ts: float, ready: bool | None = None) -> int:
    total = 0
    threshold = now_ts - max(0.0, float(within_s))
    for item in entries:
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts < threshold:
            continue
        status = str(item.get("status") or "")
        if ready is None:
            total += 1
        elif ready and status == ReadinessStatus.READY.value:
            total += 1
        elif ready is False and status != ReadinessStatus.READY.value:
            total += 1
    return total


def _classify_channel_incident(channel: str, entry: dict[str, Any]) -> str | None:
    if not isinstance(entry, dict):
        return None
    status = str(entry.get("status") or "").strip().lower()
    summary = str(entry.get("summary") or "").strip().lower()
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}

    if channel == "root_control":
        if status == "reconnect":
            return "reconnect"
        kind = str(details.get("kind") or "").strip().lower()
        if kind:
            return f"transport_{kind}"
        if status in {ReadinessStatus.DOWN.value, ReadinessStatus.DEGRADED.value}:
            return f"state_{status}"
        if "transport" in summary or "session" in summary:
            return "transport_incident"
        return None

    if channel == "route":
        if status in {
            "late_reply",
            "publish_fail",
            "no_upstream",
            "forced_close_no_upstream",
        }:
            return status
        route_t = str(details.get("t") or "").strip().lower()
        if route_t in {"frame", "chunk", "http", "open", "close"}:
            return f"{route_t}_incident"
        if status in {ReadinessStatus.DOWN.value, ReadinessStatus.DEGRADED.value}:
            cause = str(details.get("cause") or "").strip().lower()
            if cause:
                return f"derived_{cause}"
            return f"state_{status}"
        return status or None

    return status or None


def _incident_class_counts(
    channel: str,
    entries: list[dict[str, Any]],
    *,
    within_s: float,
    now_ts: float,
) -> dict[str, int]:
    threshold = now_ts - max(0.0, float(within_s))
    counts: dict[str, int] = {}
    for item in entries:
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts < threshold:
            continue
        cls = _classify_channel_incident(channel, item)
        if not cls:
            continue
        counts[cls] = int(counts.get(cls) or 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _recent_incident_samples(
    channel: str,
    entries: list[dict[str, Any]],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in entries:
        cls = _classify_channel_incident(channel, item)
        if not cls:
            continue
        samples.append(
            {
                "ts": item.get("ts"),
                "status": item.get("status"),
                "class": cls,
                "summary": item.get("summary"),
                "details": item.get("details") if isinstance(item.get("details"), dict) else {},
            }
        )
    return samples[-max(1, int(limit)) :]


def _last_transition_at(entries: list[dict[str, Any]], *, ready: bool | None = None) -> float | None:
    for item in reversed(entries):
        status = str(item.get("status") or "")
        if ready is None:
            return float(item.get("ts") or 0.0) or None
        if ready and status == ReadinessStatus.READY.value:
            return float(item.get("ts") or 0.0) or None
        if ready is False and status != ReadinessStatus.READY.value:
            return float(item.get("ts") or 0.0) or None
    return None


def _round_age(now_ts: float, ts: float | None) -> float | None:
    if not isinstance(ts, (int, float)) or float(ts) <= 0.0:
        return None
    return round(max(0.0, now_ts - float(ts)), 3)


def _channel_stability_assessment(
    *,
    status: str,
    non_ready_5m: int,
    non_ready_15m: int,
    transitions_5m: int,
) -> dict[str, Any]:
    score = 100
    if status == ReadinessStatus.DOWN.value:
        score -= 45
    elif status == ReadinessStatus.DEGRADED.value:
        score -= 25
    elif status not in {ReadinessStatus.READY.value, ReadinessStatus.UNKNOWN.value}:
        score -= 10

    score -= min(30, non_ready_5m * 15)
    score -= min(15, non_ready_15m * 5)
    score -= min(10, max(0, transitions_5m - 1) * 2)
    score = max(0, min(100, score))

    if status == ReadinessStatus.DOWN.value:
        state = "down"
        reason = "channel is currently down"
    elif non_ready_5m >= 2 or transitions_5m >= 4:
        state = "flapping"
        reason = f"{non_ready_5m} non-ready transitions in the last 5 minutes"
    elif status == ReadinessStatus.DEGRADED.value:
        state = "degraded"
        reason = "channel is currently degraded"
    elif non_ready_5m >= 1:
        state = "unstable"
        reason = f"{non_ready_5m} non-ready incidents in the last 5 minutes"
    elif non_ready_15m >= 3:
        state = "unstable"
        reason = f"{non_ready_15m} non-ready transitions in the last 15 minutes"
    elif status == ReadinessStatus.READY.value:
        state = "stable"
        reason = "channel is ready and no recent flap threshold is exceeded"
    else:
        state = "unknown"
        reason = "channel has not been observed enough yet"

    return {"state": state, "score": score, "reason": reason}


def channel_diagnostics_snapshot() -> dict[str, Any]:
    now_ts = time.time()
    with _LOCK:
        signals = {
            "root_control": _ROOT_CONTROL,
            "route": _ROUTE,
        }
        diagnostics: dict[str, Any] = {}
        for name, signal in signals.items():
            history_entries = list(_CHANNEL_HISTORY.get(name) or [])
            current_status = signal.status.value
            last_ready_at = _last_transition_at(history_entries, ready=True)
            if last_ready_at is None and current_status == ReadinessStatus.READY.value:
                last_ready_at = signal.updated_at or None
            last_non_ready_at = _last_transition_at(history_entries, ready=False)
            if last_non_ready_at is None and current_status not in {
                ReadinessStatus.READY.value,
                ReadinessStatus.UNKNOWN.value,
                ReadinessStatus.NOT_APPLICABLE.value,
            }:
                last_non_ready_at = signal.updated_at or None
            last_transition_at = _last_transition_at(history_entries, ready=None) or signal.updated_at or None
            non_ready_5m = _history_count(history_entries, within_s=300.0, now_ts=now_ts, ready=False)
            non_ready_15m = _history_count(history_entries, within_s=900.0, now_ts=now_ts, ready=False)
            ready_5m = _history_count(history_entries, within_s=300.0, now_ts=now_ts, ready=True)
            transitions_5m = _history_count(history_entries, within_s=300.0, now_ts=now_ts, ready=None)
            incident_classes_5m = _incident_class_counts(name, history_entries, within_s=300.0, now_ts=now_ts)
            incident_classes_15m = _incident_class_counts(name, history_entries, within_s=900.0, now_ts=now_ts)
            recent_incident_samples = _recent_incident_samples(name, history_entries, limit=6)
            last_incident_class = recent_incident_samples[-1]["class"] if recent_incident_samples else None
            stability = _channel_stability_assessment(
                status=current_status,
                non_ready_5m=non_ready_5m,
                non_ready_15m=non_ready_15m,
                transitions_5m=transitions_5m,
            )
            diagnostics[name] = {
                "status": current_status,
                "summary": signal.summary,
                "updated_at": signal.updated_at or None,
                "status_age_s": _round_age(now_ts, signal.updated_at or None),
                "last_transition_at": last_transition_at,
                "last_transition_ago_s": _round_age(now_ts, last_transition_at),
                "last_ready_at": last_ready_at,
                "last_ready_ago_s": _round_age(now_ts, last_ready_at),
                "last_non_ready_at": last_non_ready_at,
                "last_non_ready_ago_s": _round_age(now_ts, last_non_ready_at),
                "recent_non_ready_transitions_5m": non_ready_5m,
                "recent_non_ready_transitions_15m": non_ready_15m,
                "recent_ready_transitions_5m": ready_5m,
                "recent_transitions_5m": transitions_5m,
                "incident_classes_5m": incident_classes_5m,
                "incident_classes_15m": incident_classes_15m,
                "last_incident_class": last_incident_class,
                "total_non_ready_transitions": sum(
                    1 for item in history_entries if str(item.get("status") or "") != ReadinessStatus.READY.value
                ),
                "total_ready_transitions": sum(
                    1 for item in history_entries if str(item.get("status") or "") == ReadinessStatus.READY.value
                ),
                "stable_for_s": _round_age(now_ts, last_ready_at) if current_status == ReadinessStatus.READY.value else None,
                "non_ready_for_s": _round_age(now_ts, last_non_ready_at)
                if current_status not in {ReadinessStatus.READY.value, ReadinessStatus.UNKNOWN.value, ReadinessStatus.NOT_APPLICABLE.value}
                else None,
                "stability": stability,
                "recent_incident_samples": recent_incident_samples,
                "recent_history": history_entries[-8:],
            }
        return diagnostics


def _transport_task_done(record: dict[str, Any], key: str) -> bool:
    task = record.get(key)
    return isinstance(task, dict) and bool(task.get("done"))


def _transport_diag_incident_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not isinstance(record, dict):
        return reasons
    source = str(record.get("source") or "").strip().lower()
    if source and source not in {"periodic"}:
        reasons.append(f"source:{source}")
    if record.get("err"):
        reasons.append("error")
    if record.get("nc_connected") is False or record.get("nc_closed") is True:
        reasons.append("transport_disconnected")
    ws_closed = record.get("ws_closed")
    ws_close_code = record.get("ws_close_code")
    if ws_closed is True or ws_close_code not in {None, "", 1000, "1000"}:
        reasons.append("ws_closed")
    if _transport_task_done(record, "reading_task"):
        reasons.append("reading_task_terminated")
    if _transport_task_done(record, "flusher_task"):
        reasons.append("flusher_task_terminated")
    if _transport_task_done(record, "ping_interval_task"):
        reasons.append("ping_interval_task_terminated")
    # If the reader is gone while the client still claims to be connected,
    # treat this as a stale-but-broken session snapshot.
    if (
        "reading_task_terminated" in reasons
        and record.get("nc_connected") is True
        and "transport_disconnected" not in reasons
    ):
        reasons.append("connected_without_reader")
    return reasons


def assess_transport_diagnostics(
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    recent_5m_threshold = now - 300.0
    recent_15m_threshold = now - 900.0
    recent_records_5m = 0
    recent_records_15m = 0
    recent_incidents_5m = 0
    recent_incidents_15m = 0
    recent_hard_incidents_5m = 0
    recent_hard_incidents_15m = 0
    recent_error_records_5m = 0
    recent_error_records_15m = 0
    recent_tags_5m: set[str] = set()
    recent_tags_15m: set[str] = set()
    recent_incident_samples: list[dict[str, Any]] = []
    last_incident_at: float | None = None
    last_incident_reasons: list[str] = []
    last_incident_summary = ""

    hard_incident_markers = {
        "error",
        "transport_disconnected",
        "ws_closed",
        "reading_task_terminated",
        "flusher_task_terminated",
        "ping_interval_task_terminated",
        "connected_without_reader",
        "source:error_cb",
        "source:watchdog",
        "source:eof",
        "source:disconnected",
    }

    for item in records or ():
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts <= 0.0 or ts < recent_15m_threshold:
            continue
        recent_records_15m += 1
        if ts >= recent_5m_threshold:
            recent_records_5m += 1

        tag = str(item.get("ws_tag") or item.get("conn_tag") or "").strip()
        if tag:
            recent_tags_15m.add(tag)
            if ts >= recent_5m_threshold:
                recent_tags_5m.add(tag)

        reasons = _transport_diag_incident_reasons(item)
        if not reasons:
            continue

        is_hard = any(marker in hard_incident_markers for marker in reasons)
        recent_incidents_15m += 1
        if ts >= recent_5m_threshold:
            recent_incidents_5m += 1
        if item.get("err"):
            recent_error_records_15m += 1
            if ts >= recent_5m_threshold:
                recent_error_records_5m += 1
        if is_hard:
            recent_hard_incidents_15m += 1
            if ts >= recent_5m_threshold:
                recent_hard_incidents_5m += 1

        recent_incident_samples.append(
            {
                "ts": ts,
                "source": item.get("source"),
                "ws_tag": tag or None,
                "reasons": reasons,
                "err": item.get("err"),
            }
        )
        if last_incident_at is None or ts >= last_incident_at:
            last_incident_at = ts
            last_incident_reasons = list(reasons)
            last_incident_summary = str(item.get("err") or item.get("source") or "").strip()

    recent_tag_changes_5m = max(0, len(recent_tags_5m) - 1)
    recent_tag_changes_15m = max(0, len(recent_tags_15m) - 1)
    last_incident_ago_s = _round_age(now, last_incident_at)
    state = "unknown"
    reason = "no recent transport diagnostics records"

    latest_is_hard = bool(last_incident_reasons) and any(
        marker in hard_incident_markers for marker in last_incident_reasons
    )
    if recent_records_15m > 0:
        state = "stable"
        reason = "recent transport diagnostics show no incident markers"
    if latest_is_hard and isinstance(last_incident_ago_s, (int, float)) and last_incident_ago_s <= 30.0:
        state = "down"
        reason = "latest transport diagnostics show a fresh disconnect/reader failure"
    elif (
        recent_hard_incidents_5m >= 2
        or recent_incidents_5m >= 3
        or recent_tag_changes_5m >= 2
        or recent_hard_incidents_15m >= 3
        or recent_tag_changes_15m >= 3
    ):
        state = "flapping"
        reason = "multiple recent transport incidents or reconnects detected"
    elif (
        recent_hard_incidents_5m >= 1
        or recent_incidents_5m >= 1
        or recent_tag_changes_5m >= 1
        or recent_hard_incidents_15m >= 2
        or recent_tag_changes_15m >= 2
        or recent_incidents_15m >= 2
    ):
        state = "unstable"
        reason = "recent transport incident or reconnect detected"

    return {
        "state": state,
        "reason": reason,
        "recent_records_5m": recent_records_5m,
        "recent_records_15m": recent_records_15m,
        "recent_incidents_5m": recent_incidents_5m,
        "recent_incidents_15m": recent_incidents_15m,
        "recent_hard_incidents_5m": recent_hard_incidents_5m,
        "recent_hard_incidents_15m": recent_hard_incidents_15m,
        "recent_ws_tags_5m": sorted(recent_tags_5m),
        "recent_ws_tags_15m": sorted(recent_tags_15m),
        "recent_tag_changes_5m": recent_tag_changes_5m,
        "recent_tag_changes_15m": recent_tag_changes_15m,
        "recent_error_records_5m": recent_error_records_5m,
        "recent_error_records_15m": recent_error_records_15m,
        "last_incident_at": last_incident_at,
        "last_incident_ago_s": last_incident_ago_s,
        "last_incident_reasons": list(last_incident_reasons),
        "last_incident_summary": last_incident_summary,
        "recent_incident_samples": recent_incident_samples[-6:],
    }


def _node(
    status: ReadinessStatus,
    summary: str,
    *,
    observed: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status.value,
        "summary": summary,
        "observed": observed,
        "details": dict(details or {}),
    }


def _apply_incident_degradation(
    node: dict[str, Any],
    *,
    channel_name: str,
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    current_status = str(node.get("status") or "")
    if current_status != ReadinessStatus.READY.value:
        return node
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    stability = diag.get("stability") if isinstance(diag.get("stability"), dict) else {}
    stability_state = str(stability.get("state") or "")
    if stability_state not in {"unstable", "flapping"}:
        return node
    degraded = dict(node)
    degraded["status"] = ReadinessStatus.DEGRADED.value
    degraded["summary"] = f"{channel_name} is degraded due to recent transport incidents"
    details = dict(node.get("details") or {})
    details.update(
        {
            "derived_from": "channel_incidents",
            "incident_state": stability_state,
            "incident_reason": str(stability.get("reason") or ""),
            "recent_non_ready_transitions_5m": diag.get("recent_non_ready_transitions_5m"),
            "recent_transitions_5m": diag.get("recent_transitions_5m"),
        }
    )
    degraded["details"] = details
    return degraded


def _is_ready(node: dict[str, Any]) -> bool:
    return str(node.get("status") or "") == ReadinessStatus.READY.value


def _derived_integration_node(name: str, root_control: dict[str, Any], observed_signal: dict[str, Any]) -> dict[str, Any]:
    sig_status = str(observed_signal.get("status") or ReadinessStatus.UNKNOWN.value)
    if sig_status != ReadinessStatus.UNKNOWN.value:
        if (
            sig_status == ReadinessStatus.READY.value
            and str(root_control.get("status") or "") != ReadinessStatus.READY.value
        ):
            node = dict(observed_signal)
            node["status"] = ReadinessStatus.DEGRADED.value
            node["summary"] = f"{name} integration probe last succeeded, but root authority is currently unavailable"
            details = dict(observed_signal.get("details") or {})
            details.update(
                {
                    "derived_from": "root_control",
                    "cause": "root_control_not_ready",
                    "last_observed_status": sig_status,
                }
            )
            node["details"] = details
            return node
        return observed_signal
    if str(root_control.get("status") or "") == ReadinessStatus.READY.value:
        return _node(
            ReadinessStatus.DEGRADED,
            f"{name} integration has no dedicated probe yet; derived from root control readiness",
            observed=False,
            details={"derived_from": "root_control"},
        )
    return _node(
        ReadinessStatus.DOWN,
        f"{name} integration is unavailable while root control is not ready",
        observed=False,
        details={"derived_from": "root_control"},
    )


def build_readiness_tree(
    *,
    role: str,
    local_ready: bool,
    node_state: str,
    draining: bool,
    connected_to_hub: bool | None,
    channel_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signals = runtime_signal_snapshot()
    diagnostics = channel_diagnostics if isinstance(channel_diagnostics, dict) else channel_diagnostics_snapshot()
    role_norm = str(role or "").strip().lower()

    local_core = _node(
        ReadinessStatus.READY if local_ready else ReadinessStatus.DOWN,
        "local runtime is healthy" if local_ready else "local runtime is not ready",
        observed=True,
        details={"node_state": node_state, "draining": bool(draining), "accepting_new_work": not bool(draining)},
    )

    if role_norm == "hub":
        root_control = _apply_incident_degradation(
            signals["root_control"],
            channel_name="hub-root control",
            diagnostics=diagnostics.get("root_control"),
        )
        route_signal = signals["route"]
        route_status = str(route_signal.get("status") or ReadinessStatus.UNKNOWN.value)
        if route_status == ReadinessStatus.UNKNOWN.value:
            if str(root_control.get("status") or "") == ReadinessStatus.READY.value:
                route = _node(
                    ReadinessStatus.DEGRADED,
                    "hub route path not observed yet",
                    observed=False,
                    details={"derived_from": "root_control"},
                )
            else:
                route = _node(
                    ReadinessStatus.DOWN,
                    "hub route path is unavailable while root control is not ready",
                    observed=False,
                    details={"derived_from": "root_control"},
                )
        else:
            route = _apply_incident_degradation(
                route_signal,
                channel_name="root relay route",
                diagnostics=diagnostics.get("route"),
            )
            if (
                str(route.get("status") or "") == ReadinessStatus.READY.value
                and str(root_control.get("status") or "") == ReadinessStatus.DEGRADED.value
            ):
                route = _node(
                    ReadinessStatus.DEGRADED,
                    "root relay route is degraded because hub-root control is degraded by recent incidents",
                    observed=True,
                    details={"derived_from": "root_control_incidents"},
                )

        sync = _node(
            ReadinessStatus.READY if local_ready else ReadinessStatus.DOWN,
            "hub-local sync services are available" if local_ready else "hub-local sync services are unavailable",
            observed=False,
            details={"derived_from": "local_core"},
        )
        integrations = {
            name: _derived_integration_node(name, root_control, sig)
            for name, sig in signals["integrations"].items()
        }
    else:
        root_control = _node(
            ReadinessStatus.NOT_APPLICABLE,
            "member/browser does not own a direct root control session",
            observed=False,
        )
        route = _node(
            ReadinessStatus.READY if connected_to_hub is True else ReadinessStatus.DOWN if connected_to_hub is False else ReadinessStatus.UNKNOWN,
            "member link to hub is connected"
            if connected_to_hub is True
            else "member link to hub is disconnected"
            if connected_to_hub is False
            else "member link state is unknown",
            observed=True if connected_to_hub is not None else False,
        )
        sync = _node(
            ReadinessStatus.READY if connected_to_hub is True else ReadinessStatus.DOWN if connected_to_hub is False else ReadinessStatus.UNKNOWN,
            "member sync path is available through the active hub link"
            if connected_to_hub is True
            else "member sync path is unavailable because the hub link is down"
            if connected_to_hub is False
            else "member sync path state is unknown",
            observed=False if connected_to_hub is not None else False,
            details={"derived_from": "connected_to_hub"} if connected_to_hub is not None else {},
        )
        integrations = {
            name: _node(
                ReadinessStatus.NOT_APPLICABLE,
                "integration readiness is evaluated on the hub/root side",
                observed=False,
            )
            for name in sorted(signals["integrations"])
        }

    media = _node(
        ReadinessStatus.UNKNOWN,
        "media plane is not part of the first-stage readiness hardening",
        observed=False,
    )

    return {
        "hub_local_core": local_core,
        "root_control": root_control,
        "route": route,
        "sync": sync,
        "integration": integrations,
        "media": media,
    }


def _matrix_entry(*, allowed: bool, reason: str, required_ready: list[str]) -> dict[str, Any]:
    return {
        "allowed": bool(allowed),
        "reason": reason,
        "required_ready": list(required_ready),
    }


def build_degraded_matrix(*, role: str, readiness_tree: dict[str, Any]) -> dict[str, Any]:
    role_norm = str(role or "").strip().lower()
    local_core = readiness_tree["hub_local_core"]
    root_control = readiness_tree["root_control"]
    route = readiness_tree["route"]
    integrations = readiness_tree["integration"]

    local_ok = _is_ready(local_core)
    root_ok = _is_ready(root_control)
    route_ok = _is_ready(route)
    tg_ok = _is_ready(integrations.get("telegram", {}))
    gh_ok = _is_ready(integrations.get("github", {}))
    llm_ok = _is_ready(integrations.get("llm", {}))

    base = {
        "execute_local_scenarios": _matrix_entry(
            allowed=local_ok,
            reason="local scenario execution depends only on hub local core readiness",
            required_ready=["hub_local_core"],
        ),
        "existing_local_member_sessions": _matrix_entry(
            allowed=local_ok,
            reason="existing local sessions may continue while the local core remains healthy",
            required_ready=["hub_local_core"],
        ),
    }

    if role_norm == "hub":
        base.update(
            {
                "new_root_backed_member_admission": _matrix_entry(
                    allowed=local_ok and root_ok,
                    reason="new root-backed admissions require fresh root control authority",
                    required_ready=["hub_local_core", "root_control"],
                ),
                "root_routed_browser_proxy": _matrix_entry(
                    allowed=local_ok and root_ok and route_ok,
                    reason="root-routed browser proxy requires local core, root control, and route readiness",
                    required_ready=["hub_local_core", "root_control", "route"],
                ),
                "telegram_action_completion": _matrix_entry(
                    allowed=local_ok and root_ok and tg_ok,
                    reason="Telegram completion requires local core, root control, and Telegram integration readiness",
                    required_ready=["hub_local_core", "root_control", "integration.telegram"],
                ),
                "github_action_completion": _matrix_entry(
                    allowed=local_ok and root_ok and gh_ok,
                    reason="GitHub completion requires local core, root control, and GitHub integration readiness",
                    required_ready=["hub_local_core", "root_control", "integration.github"],
                ),
                "llm_action_completion": _matrix_entry(
                    allowed=local_ok and root_ok and llm_ok,
                    reason="Root-backed LLM completion requires local core, root control, and LLM integration readiness",
                    required_ready=["hub_local_core", "root_control", "integration.llm"],
                ),
                "core_update_coordination_via_root": _matrix_entry(
                    allowed=local_ok and root_ok,
                    reason="Core update coordination depends on local core and root control readiness",
                    required_ready=["hub_local_core", "root_control"],
                ),
            }
        )
    else:
        base.update(
            {
                "new_root_backed_member_admission": _matrix_entry(
                    allowed=False,
                    reason="member/browser role does not own root-backed admissions",
                    required_ready=[],
                ),
                "root_routed_browser_proxy": _matrix_entry(
                    allowed=local_ok and route_ok,
                    reason="member/browser route availability depends on local core and the current hub path",
                    required_ready=["hub_local_core", "route"],
                ),
                "telegram_action_completion": _matrix_entry(
                    allowed=False,
                    reason="integration completion is evaluated on the hub/root side",
                    required_ready=[],
                ),
                "github_action_completion": _matrix_entry(
                    allowed=False,
                    reason="integration completion is evaluated on the hub/root side",
                    required_ready=[],
                ),
                "llm_action_completion": _matrix_entry(
                    allowed=False,
                    reason="integration completion is evaluated on the hub/root side",
                    required_ready=[],
                ),
                "core_update_coordination_via_root": _matrix_entry(
                    allowed=False,
                    reason="member/browser role does not coordinate core updates via root",
                    required_ready=[],
                ),
            }
        )

    return base


def channel_overview_snapshot(
    *,
    readiness_tree: dict[str, Any],
    channel_diagnostics: dict[str, Any],
    transport_strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy = transport_strategy if isinstance(transport_strategy, dict) else {}
    assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}

    root_tree = readiness_tree.get("root_control") if isinstance(readiness_tree.get("root_control"), dict) else {}
    root_diag = channel_diagnostics.get("root_control") if isinstance(channel_diagnostics.get("root_control"), dict) else {}
    route_tree = readiness_tree.get("route") if isinstance(readiness_tree.get("route"), dict) else {}
    route_diag = channel_diagnostics.get("route") if isinstance(channel_diagnostics.get("route"), dict) else {}
    sync_tree = readiness_tree.get("sync") if isinstance(readiness_tree.get("sync"), dict) else {}
    sync_diag: dict[str, Any] = {}

    hub_root = effective_channel_view(
        "root_control",
        tree_item=root_tree,
        diag_item=root_diag,
        transport_assessment=assessment,
    )
    hub_root_browser = effective_channel_view(
        "route",
        tree_item=route_tree,
        diag_item=route_diag,
        transport_assessment=assessment,
    )
    browser_hub_sync = effective_channel_view(
        "sync",
        tree_item=sync_tree,
        diag_item=sync_diag,
        transport_assessment={},
    )

    return {
        "hub_root": {
            "channel_id": "root_control",
            "title": "Hub -> Root control",
            "effective_status": hub_root.get("status"),
            "effective_state": hub_root.get("state"),
            "readiness": root_tree,
            "diagnostics": root_diag,
        },
        "hub_root_browser": {
            "channel_id": "route",
            "title": "Hub -> Root -> Browser relay",
            "effective_status": hub_root_browser.get("status"),
            "effective_state": hub_root_browser.get("state"),
            "readiness": route_tree,
            "diagnostics": route_diag,
        },
        "browser_hub_sync": {
            "channel_id": "sync",
            "title": "Browser -> Hub sync",
            "effective_status": browser_hub_sync.get("status"),
            "effective_state": browser_hub_sync.get("state"),
            "readiness": sync_tree,
            "diagnostics": sync_diag,
        },
    }


def hub_root_protocol_model_snapshot() -> dict[str, Any]:
    return {
        "traffic_classes": {
            name: hub_root_protocol_class_policy(name)
            for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES
        },
        "stale_authority_thresholds_s": {
            name: int(hub_root_protocol_class_policy(name).get("stale_authority_after_s") or 0)
            for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES
        },
        "tracked_streams": [
            {
                "flow_id": "hub_root.integration.github_core_update",
                "stream_id_pattern": "hub-integration:github-core-update:<hub_id>",
                "delivery_class": "must_not_lose",
                "message_type": "state_report",
                "ack_required": True,
                "dedupe_scope": "cursor_and_message_id",
            }
        ],
    }


def _hub_root_protocol_assessment(protocol: dict[str, Any]) -> dict[str, Any]:
    traffic_classes = protocol.get("traffic_classes") if isinstance(protocol.get("traffic_classes"), dict) else {}
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    integration_outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control = traffic_classes.get("control") if isinstance(traffic_classes.get("control"), dict) else {}
    route = traffic_classes.get("route") if isinstance(traffic_classes.get("route"), dict) else {}
    telegram = integration_outboxes.get("telegram") if isinstance(integration_outboxes.get("telegram"), dict) else {}

    reasons: list[str] = []
    state = "nominal"
    if int(control.get("active_subscriptions") or 0) <= 0:
        state = "degraded"
        reasons.append("control_subscription_missing")
    if int(control.get("handler_errors") or 0) > 0:
        state = "degraded"
        reasons.append("control_handler_errors")
    control_qsize = control.get("last_qsize")
    control_limit = ((control.get("policy") or {}) if isinstance(control.get("policy"), dict) else {}).get("pending_msgs_limit")
    if isinstance(control_qsize, int) and isinstance(control_limit, int) and control_limit > 0 and control_qsize >= control_limit:
        state = "degraded"
        reasons.append("control_queue_at_limit")

    route_backlog = int(route_runtime.get("pending_events") or 0)
    route_qsize = route.get("last_qsize")
    route_limit = ((route.get("policy") or {}) if isinstance(route.get("policy"), dict) else {}).get("pending_msgs_limit")
    if route_backlog > 0:
        if state == "nominal":
            state = "pressure"
        reasons.append("route_backlog")
    if isinstance(route_qsize, int) and isinstance(route_limit, int) and route_limit > 0 and route_qsize >= route_limit:
        if state == "nominal":
            state = "pressure"
        reasons.append("route_queue_at_limit")

    telegram_size = int(telegram.get("size") or 0)
    telegram_max = telegram.get("max_size")
    if telegram_size > 0:
        if state == "nominal":
            state = "pressure"
        reasons.append("integration_buffering")
    if isinstance(telegram_max, int) and telegram_max > 0 and telegram_size >= telegram_max:
        if state == "nominal":
            state = "pressure"
        reasons.append("integration_outbox_full")

    for stream_id, entry in streams.items():
        if not isinstance(entry, dict):
            continue
        pending = entry.get("pending")
        if not isinstance(pending, dict):
            continue
        pending_age_s = pending.get("age_s")
        traffic = str(entry.get("traffic_class") or "integration").strip().lower()
        cls = traffic_classes.get(traffic) if isinstance(traffic_classes.get(traffic), dict) else {}
        policy = cls.get("policy") if isinstance(cls.get("policy"), dict) else {}
        stale_after_s = int(policy.get("stale_authority_after_s") or 0)
        flow_id = str(entry.get("flow_id") or stream_id).strip()
        if isinstance(pending_age_s, (int, float)) and stale_after_s > 0 and float(pending_age_s) >= float(stale_after_s):
            state = "degraded"
            reasons.append(f"pending_ack_stale:{flow_id}")
        elif isinstance(pending_age_s, (int, float)) and float(pending_age_s) > 0.0:
            if state == "nominal":
                state = "pressure"
            reasons.append(f"pending_ack:{flow_id}")

    if not reasons:
        reasons.append("no_active_protocol_pressure")
    return {"state": state, "reason": "; ".join(reasons)}


def hub_root_protocol_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        runtime = {
            "traffic_classes": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("traffic_classes") or {})),
            "subscriptions": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("subscriptions") or {})),
            "route_runtime": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("route_runtime") or {})),
            "integration_outboxes": json.loads(json.dumps(_HUB_ROOT_PROTOCOL_RUNTIME.get("integration_outboxes") or {})),
            "streams": {},
            "updated_at": _HUB_ROOT_PROTOCOL_RUNTIME.get("updated_at"),
        }
    try:
        stream_state = protocol_streams_snapshot(now_ts=now)
        runtime["streams"] = (
            stream_state.get("streams")
            if isinstance(stream_state.get("streams"), dict)
            else {}
        )
        if not runtime.get("updated_at") and stream_state.get("updated_at"):
            runtime["updated_at"] = stream_state.get("updated_at")
    except Exception:
        runtime["streams"] = {}
    traffic_classes = runtime.get("traffic_classes") if isinstance(runtime.get("traffic_classes"), dict) else {}
    for name in _HUB_ROOT_PROTOCOL_TRAFFIC_CLASSES:
        cls = traffic_classes.get(name) if isinstance(traffic_classes.get(name), dict) else {}
        if not cls:
            cls = _new_protocol_traffic_class_state(name)
            traffic_classes[name] = cls
        cls["policy"] = hub_root_protocol_class_policy(name)
        cls["last_dispatch_ago_s"] = _round_age(now, cls.get("last_dispatch_at"))
        cls["last_publish_ago_s"] = _round_age(now, cls.get("last_publish_at"))
        cls["last_error_ago_s"] = _round_age(now, cls.get("last_error_at"))
    subscriptions = runtime.get("subscriptions") if isinstance(runtime.get("subscriptions"), dict) else {}
    for entry in subscriptions.values():
        if not isinstance(entry, dict):
            continue
        entry["last_dispatch_ago_s"] = _round_age(now, entry.get("last_dispatch_at"))
        entry["last_error_ago_s"] = _round_age(now, entry.get("last_error_at"))
        entry["updated_ago_s"] = _round_age(now, entry.get("updated_at"))
    outboxes = runtime.get("integration_outboxes") if isinstance(runtime.get("integration_outboxes"), dict) else {}
    for entry in outboxes.values():
        if not isinstance(entry, dict):
            continue
        entry["updated_ago_s"] = _round_age(now, entry.get("updated_at"))
        entry["last_error_ago_s"] = _round_age(now, entry.get("last_error_at"))
    streams = runtime.get("streams") if isinstance(runtime.get("streams"), dict) else {}
    pending_acks = 0
    for entry in streams.values():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("pending"), dict):
            pending_acks += 1
    runtime["pending_ack_streams"] = pending_acks
    runtime["updated_ago_s"] = _round_age(now, runtime.get("updated_at"))
    runtime["assessment"] = _hub_root_protocol_assessment(runtime)
    return runtime


def reliability_model_snapshot() -> dict[str, Any]:
    return {
        "message_taxonomy": [item.value for item in MessageTaxonomy],
        "delivery_classes": [item.value for item in DeliveryClass],
        "channel_types": [item.value for item in ChannelType],
        "authorities": [item.value for item in Authority],
        "authority_boundaries": AUTHORITY_BOUNDARIES,
        "flow_inventory": [item.to_dict() for item in HUB_ROOT_FLOW_SPECS],
        "hub_root_protocol": hub_root_protocol_model_snapshot(),
    }


def sidecar_runtime_snapshot() -> dict[str, Any]:
    try:
        from adaos.services.realtime_sidecar import (
            realtime_sidecar_diag_path,
            realtime_sidecar_enabled,
            realtime_sidecar_local_url,
        )
    except Exception:
        return {"enabled": False, "status": "unavailable", "summary": "sidecar runtime module is unavailable"}

    enabled = bool(realtime_sidecar_enabled())
    diag_path = realtime_sidecar_diag_path()
    record = _read_last_jsonl_record(diag_path)
    now_ts = time.time()

    status = "disabled"
    summary = "realtime sidecar is disabled"
    if enabled:
        status = "unknown"
        summary = "realtime sidecar is enabled but has no diagnostics yet"
    if isinstance(record, dict):
        last_error = str(record.get("last_error") or "").strip()
        remote_connected_ago_s = record.get("remote_connected_ago_s")
        local_connected_ago_s = record.get("local_connected_ago_s")
        ts = record.get("ts")
        diag_age_s = None
        if isinstance(ts, (int, float)):
            diag_age_s = round(max(0.0, now_ts - float(ts)), 3)
        if last_error:
            status = "degraded"
            summary = f"sidecar reports transport error: {last_error}"
        elif isinstance(remote_connected_ago_s, (int, float)):
            status = "ready"
            summary = "sidecar remote session is connected"
        elif isinstance(local_connected_ago_s, (int, float)):
            status = "degraded"
            summary = "sidecar local listener is active but remote session is not connected"
        else:
            status = "unknown" if enabled else "disabled"
            summary = "sidecar diagnostics do not show an active session"
        return {
            "enabled": enabled,
            "status": status,
            "summary": summary,
            "local_url": realtime_sidecar_local_url(),
            "diag_path": str(diag_path),
            "diag_age_s": diag_age_s,
            "last_diag": record,
        }

    return {
        "enabled": enabled,
        "status": status,
        "summary": summary,
        "local_url": realtime_sidecar_local_url(),
        "diag_path": str(diag_path),
        "diag_age_s": None,
        "last_diag": None,
    }


def reliability_snapshot(
    *,
    node_id: str,
    subnet_id: str,
    role: str,
    local_ready: bool,
    node_state: str,
    draining: bool,
    route_mode: str | None,
    connected_to_hub: bool | None,
) -> dict[str, Any]:
    channel_diagnostics = channel_diagnostics_snapshot()
    transport_strategy = hub_root_transport_strategy_snapshot()
    readiness_tree = build_readiness_tree(
        role=role,
        local_ready=local_ready,
        node_state=node_state,
        draining=draining,
        connected_to_hub=connected_to_hub,
        channel_diagnostics=channel_diagnostics,
    )
    degraded_matrix = build_degraded_matrix(role=role, readiness_tree=readiness_tree)
    channel_overview = channel_overview_snapshot(
        readiness_tree=readiness_tree,
        channel_diagnostics=channel_diagnostics,
        transport_strategy=transport_strategy,
    )
    hub_root_protocol = hub_root_protocol_snapshot()
    sidecar_runtime = sidecar_runtime_snapshot()
    return {
        "ok": True,
        "node": {
            "node_id": node_id,
            "subnet_id": subnet_id,
            "role": role,
            "ready": bool(local_ready and not draining),
            "node_state": node_state,
            "draining": bool(draining),
            "route_mode": route_mode,
            "connected_to_hub": connected_to_hub,
        },
        "model": reliability_model_snapshot(),
        "runtime": {
            "signals": runtime_signal_snapshot(),
            "readiness_tree": readiness_tree,
            "degraded_matrix": degraded_matrix,
            "channel_diagnostics": channel_diagnostics,
            "channel_overview": channel_overview,
            "hub_root_transport_strategy": transport_strategy,
            "hub_root_protocol": hub_root_protocol,
            "sidecar_runtime": sidecar_runtime,
        },
    }
