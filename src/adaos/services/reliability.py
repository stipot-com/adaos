from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
        current_paths=("nats:route.to_hub.*",),
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
        current_paths=("nats:route.to_hub.*", "nats:route.to_browser.*"),
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
_ROOT_CONTROL = RuntimeSignal()
_ROUTE = RuntimeSignal()
_INTEGRATIONS: dict[str, RuntimeSignal] = {name: RuntimeSignal() for name in _INTEGRATION_NAMES}


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


def reset_reliability_runtime_state() -> None:
    with _LOCK:
        _set_signal(_ROOT_CONTROL, status=ReadinessStatus.UNKNOWN)
        _set_signal(_ROUTE, status=ReadinessStatus.UNKNOWN)
        for name in _INTEGRATION_NAMES:
            _set_signal(_INTEGRATIONS[name], status=ReadinessStatus.UNKNOWN)


def mark_root_control_up(*, summary: str = "hub-root control session established", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        _set_signal(
            _ROOT_CONTROL,
            status=ReadinessStatus.READY,
            summary=summary,
            observed=True,
            details=details,
        )


def mark_root_control_down(*, summary: str = "hub-root control session unavailable", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        _set_signal(
            _ROOT_CONTROL,
            status=ReadinessStatus.DOWN,
            summary=summary,
            observed=True,
            details=details,
        )
        if _ROUTE.status == ReadinessStatus.READY:
            _set_signal(
                _ROUTE,
                status=ReadinessStatus.DEGRADED,
                summary="route path lost authority while root control is down",
                observed=True,
                details={"cause": "root_control_down"},
            )


def mark_route_ready(*, summary: str = "hub route relay subscription installed", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        _set_signal(
            _ROUTE,
            status=ReadinessStatus.READY,
            summary=summary,
            observed=True,
            details=details,
        )


def mark_route_degraded(*, summary: str = "hub route relay degraded", details: dict[str, Any] | None = None) -> None:
    with _LOCK:
        _set_signal(
            _ROUTE,
            status=ReadinessStatus.DEGRADED,
            summary=summary,
            observed=True,
            details=details,
        )


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


def _is_ready(node: dict[str, Any]) -> bool:
    return str(node.get("status") or "") == ReadinessStatus.READY.value


def _derived_integration_node(name: str, root_control: dict[str, Any], observed_signal: dict[str, Any]) -> dict[str, Any]:
    sig_status = str(observed_signal.get("status") or ReadinessStatus.UNKNOWN.value)
    if sig_status != ReadinessStatus.UNKNOWN.value:
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
) -> dict[str, Any]:
    signals = runtime_signal_snapshot()
    role_norm = str(role or "").strip().lower()

    local_core = _node(
        ReadinessStatus.READY if local_ready else ReadinessStatus.DOWN,
        "local runtime is healthy" if local_ready else "local runtime is not ready",
        observed=True,
        details={"node_state": node_state, "draining": bool(draining), "accepting_new_work": not bool(draining)},
    )

    if role_norm == "hub":
        root_control = signals["root_control"]
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
            route = route_signal

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


def reliability_model_snapshot() -> dict[str, Any]:
    return {
        "message_taxonomy": [item.value for item in MessageTaxonomy],
        "delivery_classes": [item.value for item in DeliveryClass],
        "channel_types": [item.value for item in ChannelType],
        "authorities": [item.value for item in Authority],
        "authority_boundaries": AUTHORITY_BOUNDARIES,
        "flow_inventory": [item.to_dict() for item in HUB_ROOT_FLOW_SPECS],
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
    readiness_tree = build_readiness_tree(
        role=role,
        local_ready=local_ready,
        node_state=node_state,
        draining=draining,
        connected_to_hub=connected_to_hub,
    )
    degraded_matrix = build_degraded_matrix(role=role, readiness_tree=readiness_tree)
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
        },
    }
