from __future__ import annotations

from typing import Any

from adaos.services.capacity import (
    io_capacity_capabilities,
    io_capacity_first_token_value,
    io_capacity_token_values,
    is_io_capacity_entry_available,
)


WEBRTC_MEDIA_IO_TYPE = "webrtc_media"
MEMBER_BROWSER_DIRECT_TOPOLOGY = "member_browser_direct"
MEMBER_BROWSER_DIRECT_TOKEN = f"topology:{MEMBER_BROWSER_DIRECT_TOPOLOGY}"


def local_webrtc_media_capabilities(*, role: str | None) -> list[str]:
    role_norm = str(role or "").strip().lower()
    caps = [
        "webrtc:av",
        "media:live_stream",
        "media:scenario_response_media",
        "signal:hub-mediated",
    ]
    if role_norm == "member":
        caps.extend(
            [
                "producer:member",
                MEMBER_BROWSER_DIRECT_TOKEN,
            ]
        )
    elif role_norm == "hub":
        caps.extend(
            [
                "producer:hub",
                "topology:hub_webrtc_loopback",
            ]
        )
    return caps


def parse_webrtc_media_capacity_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    io_type = str(entry.get("io_type") or entry.get("type") or "").strip().lower()
    capabilities = io_capacity_capabilities(entry)
    producer = io_capacity_first_token_value(entry, "producer")
    topology = io_capacity_first_token_value(entry, "topology")
    return {
        "io_type": io_type or None,
        "capabilities": list(capabilities),
        "priority": int(entry.get("priority") or 50),
        "id_hint": str(entry.get("id_hint") or "").strip() or None,
        "available": is_io_capacity_entry_available(entry, default=True),
        "mode": io_capacity_first_token_value(entry, "mode"),
        "reason": io_capacity_first_token_value(entry, "reason"),
        "producer": producer,
        "topology": topology,
        "media_needs": io_capacity_token_values(entry, "media"),
        "signals": io_capacity_token_values(entry, "signal"),
        "member_browser_direct": bool(
            io_type == WEBRTC_MEDIA_IO_TYPE
            and "webrtc:av" in capabilities
            and producer == "member"
            and topology == MEMBER_BROWSER_DIRECT_TOPOLOGY
        ),
    }


def select_member_browser_direct_capacity_entry(capacity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(capacity, dict):
        return None
    io_items = capacity.get("io") if isinstance(capacity.get("io"), list) else []
    best_entry: dict[str, Any] | None = None
    best_priority = -1
    for item in io_items:
        if not isinstance(item, dict):
            continue
        parsed = parse_webrtc_media_capacity_entry(item)
        if not bool(parsed.get("member_browser_direct")):
            continue
        if not bool(parsed.get("available")):
            continue
        priority = int(parsed.get("priority") or 0)
        if priority <= best_priority:
            continue
        best_entry = dict(item)
        best_priority = priority
    return best_entry


def _directory_nodes() -> list[dict[str, Any]]:
    try:
        from adaos.services.registry.subnet_directory import get_directory

        raw = get_directory().list_known_nodes()
    except Exception:
        raw = []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _live_member_links() -> list[dict[str, Any]]:
    try:
        from adaos.services.subnet.link_manager import hub_link_manager_snapshot

        raw = hub_link_manager_snapshot()
    except Exception:
        raw = {}
    items = raw.get("members") if isinstance(raw.get("members"), list) else []
    return [dict(item) for item in items if isinstance(item, dict)]


def _normalized_roles(
    *,
    directory_item: dict[str, Any],
    live_item: dict[str, Any],
    node_snapshot: dict[str, Any],
) -> list[str]:
    roles: list[str] = []
    sources = []
    snapshot_role = str(node_snapshot.get("role") or "").strip()
    if snapshot_role:
        sources.append([snapshot_role])
    sources.append(live_item.get("roles") if isinstance(live_item.get("roles"), list) else [])
    sources.append(directory_item.get("roles") if isinstance(directory_item.get("roles"), list) else [])
    for raw in sources:
        for item in raw:
            token = str(item or "").strip().lower()
            if token and token not in roles:
                roles.append(token)
    return roles


def list_member_browser_media_candidates() -> list[dict[str, Any]]:
    directory_by_id: dict[str, dict[str, Any]] = {}
    for item in _directory_nodes():
        node_id = str(item.get("node_id") or "").strip()
        if node_id:
            directory_by_id[node_id] = item

    live_by_id: dict[str, dict[str, Any]] = {}
    for item in _live_member_links():
        node_id = str(item.get("node_id") or "").strip()
        if node_id:
            live_by_id[node_id] = item

    candidates: list[dict[str, Any]] = []
    for node_id in sorted(set(directory_by_id) | set(live_by_id)):
        directory_item = directory_by_id.get(node_id, {})
        live_item = live_by_id.get(node_id, {})
        node_snapshot = live_item.get("node_snapshot") if isinstance(live_item.get("node_snapshot"), dict) else {}
        roles = _normalized_roles(
            directory_item=directory_item,
            live_item=live_item,
            node_snapshot=node_snapshot,
        )
        if "member" not in roles:
            continue

        online = bool(directory_item.get("online")) or bool(live_item.get("connected"))
        node_state = str(node_snapshot.get("node_state") or directory_item.get("node_state") or "").strip().lower()
        if not online or (node_state and node_state != "ready"):
            continue

        snapshot_capacity = node_snapshot.get("capacity") if isinstance(node_snapshot.get("capacity"), dict) else {}
        directory_capacity = directory_item.get("capacity") if isinstance(directory_item.get("capacity"), dict) else {}
        capacity = snapshot_capacity if snapshot_capacity else directory_capacity
        source = (
            "live_snapshot"
            if snapshot_capacity and not directory_capacity
            else "merged"
            if snapshot_capacity
            else "subnet_directory"
        )
        entry = select_member_browser_direct_capacity_entry(capacity)
        if not isinstance(entry, dict):
            continue
        parsed = parse_webrtc_media_capacity_entry(entry)
        candidates.append(
            {
                "member_id": node_id,
                "node_id": node_id,
                "roles": roles,
                "hostname": str(directory_item.get("hostname") or "").strip() or None,
                "base_url": str(directory_item.get("base_url") or "").strip() or None,
                "online": online,
                "node_state": node_state or None,
                "source": source,
                "priority": int(parsed.get("priority") or 50),
                "mode": parsed.get("mode"),
                "media_needs": list(parsed.get("media_needs") or []),
                "capabilities": list(parsed.get("capabilities") or []),
                "capacity_entry": dict(entry),
                "updated_at": (
                    entry.get("updated_at")
                    or node_snapshot.get("captured_at")
                    or directory_item.get("updated_at")
                    or directory_item.get("last_seen")
                ),
            }
        )

    candidates.sort(
        key=lambda item: (
            -int(item.get("priority") or 0),
            -float(item.get("updated_at") or 0.0),
            str(item.get("member_id") or ""),
        )
    )
    return candidates


def member_browser_direct_foundation(
    *,
    browser_session_total: int,
    connected_browser_session_total: int | None = None,
    admitted: bool = False,
) -> dict[str, Any]:
    browser_total = max(0, int(browser_session_total or 0))
    connected_total = (
        browser_total
        if connected_browser_session_total is None
        else max(0, int(connected_browser_session_total or 0))
    )
    candidates = list_member_browser_media_candidates()
    candidate_members = [
        str(item.get("member_id") or "").strip()
        for item in candidates
        if str(item.get("member_id") or "").strip()
    ]
    preferred_member_id = candidate_members[0] if candidate_members else None
    preferred_candidate = candidates[0] if candidates else {}
    possible = bool(candidate_members) and browser_total > 0
    ready = possible and bool(admitted)
    if ready:
        reason = "member_browser_direct_ready"
    elif not candidate_members and browser_total <= 0:
        reason = "member_browser_direct_missing_browser_or_member_candidate"
    elif not candidate_members:
        reason = "member_browser_direct_missing_member_candidate"
    elif browser_total <= 0:
        reason = "member_browser_direct_missing_browser_session"
    else:
        reason = "member_browser_direct_policy_not_admitted_yet"
    return {
        "possible": possible,
        "admitted": bool(admitted),
        "ready": ready,
        "reason": reason,
        "candidate_member_total": len(candidate_members),
        "candidate_members": list(candidate_members),
        "preferred_member_id": preferred_member_id,
        "preferred_candidate_source": str(preferred_candidate.get("source") or "").strip() or None,
        "browser_session_total": browser_total,
        "connected_browser_session_total": connected_total,
    }


__all__ = [
    "MEMBER_BROWSER_DIRECT_TOPOLOGY",
    "MEMBER_BROWSER_DIRECT_TOKEN",
    "WEBRTC_MEDIA_IO_TYPE",
    "list_member_browser_media_candidates",
    "local_webrtc_media_capabilities",
    "member_browser_direct_foundation",
    "parse_webrtc_media_capacity_entry",
    "select_member_browser_direct_capacity_entry",
]
