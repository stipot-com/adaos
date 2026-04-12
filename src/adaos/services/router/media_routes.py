from __future__ import annotations

from typing import Any


_ROUTE_LABELS: dict[str, str] = {
    "local_http": "direct local hub HTTP",
    "root_media_relay": "bounded root media relay",
    "hub_webrtc_loopback": "browser-hub direct WebRTC media",
    "member_browser_direct": "browser-member direct WebRTC media",
}


def _normalize_need(need: str | None) -> str:
    token = str(need or "").strip().lower()
    if token in {"upload", "playback", "live_stream", "scenario_response_media"}:
        return token
    return "scenario_response_media"


def _normalize_producer_preference(token: str | None) -> str:
    value = str(token or "").strip().lower()
    if value in {"hub", "member", "router_selected"}:
        return value
    return "hub"


def _topology_state(
    *,
    topology_id: str,
    available: bool,
    producer_authority: str,
    ready_reason: str,
    unavailable_reason: str,
) -> dict[str, Any]:
    return {
        "topology_id": topology_id,
        "label": _ROUTE_LABELS.get(topology_id, topology_id),
        "available": bool(available),
        "producer_authority": str(producer_authority or "none"),
        "reason": ready_reason if available else unavailable_reason,
    }


def resolve_media_route_intent(
    *,
    need: str | None,
    target_webspace_id: str | None = None,
    producer_preference: str | None = None,
    preferred_member_id: str | None = None,
    candidate_member_ids: list[str] | None = None,
    direct_local_ready: bool,
    root_routed_ready: bool,
    hub_webrtc_ready: bool,
    member_browser_direct_possible: bool = False,
    member_browser_direct_admitted: bool = False,
    member_browser_direct_reason: str | None = None,
    candidate_member_total: int = 0,
    browser_session_total: int = 0,
    observed_failure: str | None = None,
) -> dict[str, Any]:
    need_norm = _normalize_need(need)
    producer_pref = _normalize_producer_preference(producer_preference)
    target_ws = str(target_webspace_id or "").strip() or None
    preferred_member = str(preferred_member_id or "").strip() or None
    candidate_members = [
        str(item or "").strip()
        for item in list(candidate_member_ids or [])
        if str(item or "").strip()
    ]
    if not preferred_member and candidate_members:
        preferred_member = candidate_members[0]
    member_direct_possible = bool(member_browser_direct_possible)
    member_direct_admitted = bool(member_browser_direct_admitted)
    member_direct_ready = (
        member_direct_possible
        and member_direct_admitted
        and int(candidate_member_total) > 0
        and int(browser_session_total) > 0
    )
    member_direct_reason = (
        str(member_browser_direct_reason or "").strip()
        or (
            "member_browser_direct_ready"
            if member_direct_ready
            else "member_browser_direct_not_possible"
            if not member_direct_possible
            else "member_browser_direct_not_admitted"
            if not member_direct_admitted
            else "member_browser_direct_missing_live_participants"
        )
    )

    abilities = {
        "local_http": _topology_state(
            topology_id="local_http",
            available=bool(direct_local_ready),
            producer_authority="hub",
            ready_reason="local_hub_api_authority_available",
            unavailable_reason="local_hub_api_authority_unavailable",
        ),
        "root_media_relay": _topology_state(
            topology_id="root_media_relay",
            available=bool(root_routed_ready),
            producer_authority="shared",
            ready_reason="root_media_relay_available",
            unavailable_reason="root_media_relay_unavailable",
        ),
        "hub_webrtc_loopback": _topology_state(
            topology_id="hub_webrtc_loopback",
            available=bool(hub_webrtc_ready),
            producer_authority="hub",
            ready_reason="hub_webrtc_media_available",
            unavailable_reason="hub_webrtc_media_unavailable",
        ),
        "member_browser_direct": _topology_state(
            topology_id="member_browser_direct",
            available=member_direct_ready,
            producer_authority="member",
            ready_reason="member_browser_direct_ready",
            unavailable_reason=member_direct_reason,
        ),
    }

    fallback_chain: list[str]
    if need_norm == "upload":
        fallback_chain = ["local_http", "root_media_relay"]
    elif need_norm == "playback":
        fallback_chain = ["local_http", "root_media_relay"]
    elif need_norm == "live_stream":
        fallback_chain = (
            ["member_browser_direct", "hub_webrtc_loopback", "root_media_relay"]
            if producer_pref == "member"
            else ["hub_webrtc_loopback", "member_browser_direct", "root_media_relay"]
        )
    else:
        fallback_chain = (
            ["member_browser_direct", "local_http", "root_media_relay", "hub_webrtc_loopback"]
            if producer_pref == "member"
            else ["local_http", "root_media_relay", "hub_webrtc_loopback", "member_browser_direct"]
        )

    preferred_topology = fallback_chain[0] if fallback_chain else None
    selected_topology = next(
        (topology_id for topology_id in fallback_chain if bool(abilities.get(topology_id, {}).get("available"))),
        None,
    )
    selected_state = abilities.get(selected_topology or "") if selected_topology else {}

    if selected_topology == "member_browser_direct":
        producer_target: dict[str, Any] | None = {
            "kind": "member",
            "member_id": preferred_member,
            "webspace_id": target_ws,
        }
    elif selected_topology:
        producer_target = {
            "kind": "hub",
            "webspace_id": target_ws,
        }
    else:
        producer_target = None

    degradation_reason: str | None = None
    if selected_topology is None:
        degradation_reason = "no_media_route_is_currently_available"
    elif preferred_topology and selected_topology != preferred_topology:
        preferred_state = abilities.get(preferred_topology, {})
        degradation_reason = str(preferred_state.get("reason") or f"{preferred_topology}_unavailable")

    selection_reason = (
        str(selected_state.get("reason") or "").strip()
        or degradation_reason
        or "no_media_route_selected"
    )
    active_route = selected_topology
    producer_authority = (
        str(selected_state.get("producer_authority") or "none")
        if selected_topology
        else "none"
    )

    return {
        "route_intent": need_norm,
        "target_webspace_id": target_ws,
        "producer_preference": producer_pref,
        "preferred_member_id": preferred_member,
        "preferred_route": preferred_topology,
        "active_route": active_route,
        "delivery_topology": active_route,
        "producer_authority": producer_authority,
        "producer_target": producer_target,
        "selection_reason": selection_reason,
        "degradation_reason": degradation_reason,
        "fallback_chain": list(fallback_chain),
        "capabilities": {
            "candidate_routes": list(fallback_chain),
            "ability": abilities,
        },
        "member_browser_direct": {
            "possible": member_direct_possible,
            "admitted": member_direct_admitted,
            "ready": member_direct_ready,
            "reason": member_direct_reason,
            "candidate_member_total": int(candidate_member_total),
            "candidate_members": list(candidate_members),
            "preferred_member_id": preferred_member,
            "browser_session_total": int(browser_session_total),
        },
        "monitoring": {
            "watch_signals": [
                "local_http_ready",
                "root_media_relay_ready",
                "hub_webrtc_ready",
                "member_browser_direct_admitted",
                "browser_session_total",
                "candidate_member_total",
            ],
            "observed_failure": str(observed_failure or "").strip() or None,
        },
    }


__all__ = ["resolve_media_route_intent"]
