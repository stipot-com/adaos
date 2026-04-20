from __future__ import annotations

from dataclasses import asdict
from typing import Any

from adaos.services.system_model.mappers import (
    canonical_object_from_integration_quota,
    canonical_object_from_node_status,
    canonical_object_from_protocol_traffic_budget,
    coerce_mapping,
)
from adaos.services.system_model.model import (
    CanonicalActionDescriptor,
    CanonicalKind,
    CanonicalObject,
    CanonicalProjection,
    CanonicalStatus,
    RelationKind,
    canonical_ref,
    compact_mapping,
    normalize_connectivity_status,
    normalize_operational_status,
)


def _token(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value or "").strip().lower()


def _runtime_status(*values: Any) -> CanonicalStatus:
    for value in values:
        mapped = normalize_operational_status(value)
        if mapped != CanonicalStatus.UNKNOWN:
            token = _token(value)
            if token in {"warning", "warn", "pending", "stale", "outdated", "pressure", "aging"}:
                return CanonicalStatus.DEGRADED
            return mapped
        token = _token(value)
        if token in {"nominal", "fresh", "stable", "available", "connected", "relay_and_webrtc_media_available", "bounded_relay_available"}:
            return CanonicalStatus.ONLINE
        if token in {"degraded", "unstable", "flapping", "pressure", "aging", "stale"}:
            return CanonicalStatus.DEGRADED
        if token in {"down", "missing"}:
            return CanonicalStatus.OFFLINE
        if token in {"disabled", "not_applicable", "not_owned", "idle", "unavailable"}:
            return CanonicalStatus.UNKNOWN
    return CanonicalStatus.UNKNOWN


def _connectivity_for_state(*values: Any):
    for value in values:
        token = _token(value)
        if token in {"ready", "fresh", "nominal", "stable", "connected", "available", "true"}:
            return normalize_connectivity_status(True)
        if token in {"down", "offline", "missing", "disconnected", "false"}:
            return normalize_connectivity_status(False)
    return normalize_connectivity_status(None)


def _canonical_status_token(*values: Any) -> str | None:
    status = _runtime_status(*values)
    if status == CanonicalStatus.UNKNOWN:
        return None
    return status.value


def _risk_for_action(action_id: str) -> str:
    token = _token(action_id)
    if token in {"backup", "go_home", "restart_sidecar", "reconnect_root"}:
        return "low"
    if token in {"reload", "restore", "set_home_current"}:
        return "medium"
    if token in {"reset"}:
        return "high"
    return "medium"


def _actions_from_yjs_overrides(
    overrides: dict[str, Any] | None,
    *,
    webspace_id: str | None,
) -> list[CanonicalActionDescriptor]:
    items: list[CanonicalActionDescriptor] = []
    data = overrides if isinstance(overrides, dict) else {}
    for action_id, payload in data.items():
        entry = payload if isinstance(payload, dict) else {}
        items.append(
            CanonicalActionDescriptor(
                id=str(action_id),
                title=str(action_id).replace("_", " "),
                risk=_risk_for_action(str(action_id)),
                metadata=compact_mapping(
                    {
                        "enabled": bool(entry.get("enabled")),
                        "reason": entry.get("reason"),
                        "source_of_truth": entry.get("source_of_truth"),
                        "scenario_id": entry.get("scenario_id"),
                        "webspace_id": webspace_id,
                    }
                ),
            )
        )
    return items


def _incident_from_object(obj: CanonicalObject) -> dict[str, Any] | None:
    if obj.status not in {CanonicalStatus.OFFLINE, CanonicalStatus.DEGRADED, CanonicalStatus.WARNING}:
        return None
    severity = "critical" if obj.status == CanonicalStatus.OFFLINE else "high" if obj.status == CanonicalStatus.DEGRADED else "medium"
    return compact_mapping(
        {
            "id": f"incident:{obj.id}",
            "object_id": obj.id,
            "severity": severity,
            "status": obj.status.value if hasattr(obj.status, "value") else str(obj.status),
            "title": obj.title,
            "summary": obj.summary,
        }
    )


def _node_like_object(obj: CanonicalObject) -> bool:
    kind = str(obj.kind or "").strip()
    return kind in {
        CanonicalKind.NODE.value,
        CanonicalKind.HUB.value,
        CanonicalKind.MEMBER.value,
    }


def _subnet_planning_nodes(subject: CanonicalObject, objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obj in [subject, *objects]:
        if not _node_like_object(obj):
            continue
        obj_id = str(obj.id or "").strip()
        if not obj_id or obj_id in seen:
            continue
        seen.add(obj_id)
        runtime = coerce_mapping(getattr(obj, "runtime", {}))
        health = coerce_mapping(getattr(obj, "health", {}))
        actual_state = coerce_mapping(getattr(obj, "actual_state", {}))
        build = coerce_mapping(runtime.get("build"))
        update_status = coerce_mapping(runtime.get("update_status"))
        freshness = coerce_mapping(runtime.get("runtime_projection_freshness"))
        node_names = [
            str(item or "").strip()
            for item in list(runtime.get("node_names") or [])
            if str(item or "").strip()
        ]
        items.append(
            compact_mapping(
                {
                    "id": obj_id,
                    "kind": obj.kind,
                    "title": obj.title,
                    "status": obj.status.value if hasattr(obj.status, "value") else str(obj.status),
                    "online": actual_state.get("online"),
                    "connectivity": health.get("connectivity"),
                    "route_mode": runtime.get("route_mode") or health.get("route_mode"),
                    "ready": runtime.get("ready"),
                    "node_state": runtime.get("node_state"),
                    "node_names": node_names,
                    "freshness": freshness,
                    "runtime_version": build.get("runtime_version") or build.get("version"),
                    "runtime_git_short_commit": build.get("runtime_git_short_commit"),
                    "update_state": update_status.get("state"),
                    "update_phase": update_status.get("phase"),
                    "connected_to_hub": runtime.get("connected_to_hub"),
                }
            )
        )
    return items


def _subnet_planning_summary(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    if not nodes:
        return {}
    status_totals: dict[str, int] = {}
    freshness_totals: dict[str, int] = {}
    route_mode_totals: dict[str, int] = {}
    update_state_totals: dict[str, int] = {}
    online_total = 0
    ready_total = 0
    stale_node_ids: list[str] = []
    pending_node_ids: list[str] = []
    offline_node_ids: list[str] = []
    for item in nodes:
        status = str(item.get("status") or "").strip()
        if status:
            status_totals[status] = int(status_totals.get(status) or 0) + 1
        route_mode = str(item.get("route_mode") or "").strip()
        if route_mode:
            route_mode_totals[route_mode] = int(route_mode_totals.get(route_mode) or 0) + 1
        update_state = str(item.get("update_state") or "").strip()
        if update_state:
            update_state_totals[update_state] = int(update_state_totals.get(update_state) or 0) + 1
        if item.get("online") is True:
            online_total += 1
        elif item.get("online") is False:
            offline_node_ids.append(str(item.get("id") or ""))
        if item.get("ready") is True:
            ready_total += 1
        freshness = coerce_mapping(item.get("freshness"))
        freshness_state = str(freshness.get("state") or "").strip()
        if freshness_state:
            freshness_totals[freshness_state] = int(freshness_totals.get(freshness_state) or 0) + 1
            if freshness_state == "stale":
                stale_node_ids.append(str(item.get("id") or ""))
            elif freshness_state == "pending":
                pending_node_ids.append(str(item.get("id") or ""))
    return compact_mapping(
        {
            "node_total": len(nodes),
            "online_total": online_total,
            "ready_total": ready_total,
            "status_totals": status_totals,
            "freshness_totals": freshness_totals,
            "route_mode_totals": route_mode_totals,
            "update_state_totals": update_state_totals,
            "stale_node_ids": [item for item in stale_node_ids if item],
            "pending_node_ids": [item for item in pending_node_ids if item],
            "offline_node_ids": [item for item in offline_node_ids if item],
        }
    )


def _flatten_relation_refs(relations: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    data = relations if isinstance(relations, dict) else {}
    for value in data.values():
        items = value if isinstance(value, list) else [value]
        for item in items:
            token = str(item or "").strip()
            if token and token not in out:
                out.append(token)
    return out


def _collect_incidents(subject: CanonicalObject, objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    for item in [subject, *objects]:
        incidents.extend(list(item.incidents or []))
        incident = _incident_from_object(item)
        if incident and not any(existing.get("id") == incident.get("id") for existing in incidents):
            incidents.append(incident)
    return incidents


def _status_token(value: Any) -> str:
    if isinstance(value, CanonicalStatus):
        return value.value
    token = _token(value)
    return token or CanonicalStatus.UNKNOWN.value


def _status_rank(value: Any) -> int:
    token = _status_token(value)
    order = {
        CanonicalStatus.OFFLINE.value: 5,
        CanonicalStatus.DEGRADED.value: 4,
        CanonicalStatus.WARNING.value: 3,
        CanonicalStatus.UNKNOWN.value: 2,
        CanonicalStatus.ONLINE.value: 1,
    }
    return int(order.get(token) or 0)


def _worst_status(items: list[CanonicalObject]) -> str:
    if not items:
        return CanonicalStatus.UNKNOWN.value
    ordered = sorted((_status_token(item.status) for item in items), key=_status_rank, reverse=True)
    return ordered[0] if ordered else CanonicalStatus.UNKNOWN.value


def _representative_object_id(items: list[CanonicalObject]) -> str | None:
    if not items:
        return None
    ranked = sorted(items, key=lambda item: (_status_rank(item.status), item.title), reverse=True)
    token = str(ranked[0].id or "").strip() if ranked else ""
    return token or None


def _kind_totals(items: list[CanonicalObject]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in items:
        kind = str(item.kind or "unknown").strip() or "unknown"
        totals[kind] = int(totals.get(kind) or 0) + 1
    return totals


def _incident_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    severity = str(item.get("severity") or "").strip().lower()
    severity_rank = {
        "critical": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
    }
    return (-int(severity_rank.get(severity) or 0), str(item.get("title") or ""))


def _health_strip_items(subject: CanonicalObject, objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    catalog = [
        ("roots", "Roots", "cloud-outline", {CanonicalKind.ROOT.value}),
        ("hubs", "Hubs", "server-outline", {CanonicalKind.HUB.value}),
        ("members", "Members", "git-branch-outline", {CanonicalKind.MEMBER.value}),
        ("browsers", "Browsers", "globe-outline", {CanonicalKind.BROWSER_SESSION.value}),
        ("devices", "Devices", "phone-portrait-outline", {CanonicalKind.DEVICE.value}),
        ("runtimes", "Runtimes", "pulse-outline", {CanonicalKind.RUNTIME.value}),
        ("quotas", "Quotas", "speedometer-outline", {CanonicalKind.QUOTA.value}),
        ("skills", "Skills", "extension-puzzle-outline", {CanonicalKind.SKILL.value}),
        ("scenarios", "Scenarios", "layers-outline", {CanonicalKind.SCENARIO.value}),
    ]
    items: list[dict[str, Any]] = []
    universe = [subject, *objects]
    for bucket_id, title, icon, kinds in catalog:
        members = [item for item in universe if str(item.kind or "").strip() in kinds]
        if not members:
            continue
        online_total = sum(1 for item in members if _status_token(item.status) == CanonicalStatus.ONLINE.value)
        issue_titles = [item.title for item in members if _status_rank(item.status) >= _status_rank(CanonicalStatus.WARNING.value)]
        status = _worst_status(members)
        subtitle = f"{online_total}/{len(members)} online"
        summary = f"{len(members)} object(s)"
        if issue_titles:
            summary = f"Issues: {', '.join(issue_titles[:3])}"
        items.append(
            compact_mapping(
                {
                    "id": f"health:{bucket_id}",
                    "object_id": _representative_object_id(members),
                    "kind": bucket_id,
                    "title": title,
                    "subtitle": subtitle,
                    "status": status,
                    "summary": summary,
                    "icon": icon,
                    "details": [item.to_dict() for item in members],
                }
            )
        )
    return items


def _quota_summary_items(objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for obj in objects:
        if str(obj.kind or "").strip() != CanonicalKind.QUOTA.value:
            continue
        resources = coerce_mapping(obj.resources)
        used = resources.get("used")
        limit = resources.get("limit")
        queue_size = resources.get("queue_size")
        queue_limit = resources.get("queue_limit")
        pending_bytes = resources.get("pending_bytes")
        pending_bytes_limit = resources.get("pending_bytes_limit")
        parts: list[str] = []
        if used is not None or limit is not None:
            parts.append(f"used {used or 0}/{limit or '?'}")
        if queue_size is not None or queue_limit is not None:
            parts.append(f"queue {queue_size or 0}/{queue_limit or '?'}")
        if pending_bytes is not None or pending_bytes_limit is not None:
            parts.append(f"bytes {pending_bytes or 0}/{pending_bytes_limit or '?'}")
        items.append(
            compact_mapping(
                {
                    "id": f"quota-summary:{obj.id}",
                    "object_id": obj.id,
                    "kind": obj.kind,
                    "title": obj.title,
                    "subtitle": ", ".join(parts) if parts else obj.summary,
                    "status": _status_token(obj.status),
                    "summary": obj.summary,
                    "details": obj.to_dict(),
                }
            )
        )
    return sorted(items, key=lambda item: (_status_rank(item.get("status")), str(item.get("title") or "")), reverse=True)


def _runtime_summary_items(objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for obj in objects:
        if str(obj.kind or "").strip() != CanonicalKind.RUNTIME.value:
            continue
        runtime = coerce_mapping(obj.runtime)
        assessment = coerce_mapping(runtime.get("assessment"))
        phase = str(runtime.get("phase") or runtime.get("scope") or "").strip()
        selected_webspace_id = str(coerce_mapping(obj.actual_state).get("selected_webspace_id") or "").strip()
        subtitle_bits = [bit for bit in [phase, selected_webspace_id] if bit]
        items.append(
            compact_mapping(
                {
                    "id": f"runtime-summary:{obj.id}",
                    "object_id": obj.id,
                    "kind": obj.kind,
                    "title": obj.title,
                    "subtitle": " | ".join(subtitle_bits) if subtitle_bits else obj.summary,
                    "status": _status_token(obj.status),
                    "summary": str(assessment.get("reason") or obj.summary or ""),
                    "details": obj.to_dict(),
                }
            )
        )
    return sorted(items, key=lambda item: (_status_rank(item.get("status")), str(item.get("title") or "")), reverse=True)


def _recent_change_items(subject: CanonicalObject, objects: list[CanonicalObject], *, limit: int = 12) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obj in [subject, *objects]:
        versioning = coerce_mapping(obj.versioning)
        if bool(versioning.get("drift")):
            change_id = f"change:{obj.id}:drift"
            if change_id not in seen:
                seen.add(change_id)
                items.append(
                    compact_mapping(
                        {
                            "id": change_id,
                            "object_id": obj.id,
                            "category": "drift",
                            "title": f"{obj.title} drift",
                            "subtitle": f"desired {versioning.get('desired') or '-'} | actual {versioning.get('actual') or '-'}",
                            "status": _status_token(obj.status),
                            "summary": obj.summary,
                            "details": obj.to_dict(),
                        }
                    )
                )

        runtime = coerce_mapping(obj.runtime)
        transitions = runtime.get("recent_non_ready_transitions_5m")
        if transitions is None:
            transitions = runtime.get("recent_transitions_5m")
        try:
            transition_total = int(transitions or 0)
        except Exception:
            transition_total = 0
        if transition_total > 0:
            change_id = f"change:{obj.id}:transitions"
            if change_id not in seen:
                seen.add(change_id)
                items.append(
                    compact_mapping(
                        {
                            "id": change_id,
                            "object_id": obj.id,
                            "category": "transition",
                            "title": f"{obj.title} transitions",
                            "subtitle": f"{transition_total} transition(s) in the last 5m",
                            "status": _status_token(obj.status),
                            "summary": obj.summary,
                            "details": obj.to_dict(),
                        }
                    )
                )

        gap = _state_gap(obj)
        if gap:
            change_id = f"change:{obj.id}:state-gap"
            if change_id not in seen:
                seen.add(change_id)
                items.append(
                    compact_mapping(
                        {
                            "id": change_id,
                            "object_id": obj.id,
                            "category": "state_gap",
                            "title": f"{obj.title} state gap",
                            "subtitle": ", ".join(sorted(gap.keys())[:4]),
                            "status": _status_token(obj.status),
                            "summary": obj.summary,
                            "details": gap,
                        }
                    )
                )
    ordered = sorted(
        items,
        key=lambda item: (_status_rank(item.get("status")), str(item.get("category") or ""), str(item.get("title") or "")),
        reverse=True,
    )
    return ordered[:limit]


def _narrative_projection(subject: CanonicalObject, objects: list[CanonicalObject], incidents: list[dict[str, Any]]) -> dict[str, Any]:
    degraded = [item.title for item in objects if item.status in {CanonicalStatus.OFFLINE, CanonicalStatus.DEGRADED, CanonicalStatus.WARNING}]
    current_issue = incidents[0]["summary"] if incidents else subject.summary
    operator_focus = degraded[:3] or [subject.title]
    return compact_mapping(
        {
            "summary": subject.summary or f"{subject.title} ({subject.kind})",
            "current_issue": current_issue,
            "operator_focus": operator_focus,
            "risk_summary": f"{len(incidents)} active incident(s)" if incidents else "no active incidents",
        }
    )


def _action_projection(subject: CanonicalObject, objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for owner in [subject, *objects]:
        for action in list(owner.actions or []):
            token = f"{owner.id}:{action.id}"
            if token in seen:
                continue
            seen.add(token)
            payload = compact_mapping(asdict(action))
            payload["object_id"] = owner.id
            actions.append(payload)
    return actions


def _topology_edges(subject: CanonicalObject, objects: list[CanonicalObject]) -> list[dict[str, Any]]:
    included = {item.id for item in [subject, *objects]}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for owner in [subject, *objects]:
        for relation, refs in (owner.relations or {}).items():
            for ref in refs:
                target = str(ref or "").strip()
                if not target or target not in included:
                    continue
                key = (owner.id, str(relation), target)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"source": owner.id, "relation": str(relation), "target": target})
    return edges


def _state_gap(subject: CanonicalObject) -> dict[str, Any]:
    desired = subject.desired_state if isinstance(subject.desired_state, dict) else {}
    actual = subject.actual_state if isinstance(subject.actual_state, dict) else {}
    delta: dict[str, Any] = {}
    for key in sorted(set(desired) | set(actual)):
        if desired.get(key) != actual.get(key):
            delta[key] = {"desired": desired.get(key), "actual": actual.get(key)}
    return compact_mapping(delta)


def _reliability_focus_context(runtime: dict[str, Any]) -> dict[str, Any]:
    readiness_tree = coerce_mapping(runtime.get("readiness_tree"))
    degraded_matrix = coerce_mapping(runtime.get("degraded_matrix"))
    zone = coerce_mapping(runtime.get("hub_root_zone"))
    blocked_capabilities = sorted(
        key
        for key, item in degraded_matrix.items()
        if isinstance(item, dict) and item.get("allowed") is False
    )
    return compact_mapping(
        {
            "readiness": {
                "hub_local_core": _canonical_status_token(coerce_mapping(readiness_tree.get("hub_local_core")).get("status")),
                "root_control": _canonical_status_token(coerce_mapping(readiness_tree.get("root_control")).get("status")),
                "route": _canonical_status_token(coerce_mapping(readiness_tree.get("route")).get("status")),
                "sync": _canonical_status_token(coerce_mapping(readiness_tree.get("sync")).get("status")),
                "media": _canonical_status_token(coerce_mapping(readiness_tree.get("media")).get("status")),
            },
            "blocked_capabilities": blocked_capabilities,
            "hub_root_zone": zone,
        }
    )


def _root_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    readiness_tree = coerce_mapping(runtime.get("readiness_tree"))
    zone = coerce_mapping(runtime.get("hub_root_zone"))
    readiness = coerce_mapping(readiness_tree.get("root_control"))
    zone_id = str(zone.get("active_zone_id") or zone.get("configured_zone_id") or "default").strip() or "default"
    selected_server = str(zone.get("selected_server") or "").strip() or None
    return CanonicalObject(
        id=canonical_ref(CanonicalKind.ROOT, zone_id) or f"root:{zone_id}",
        kind=CanonicalKind.ROOT.value,
        title=f"Root {zone_id}",
        summary="Root control-plane authority for the current node",
        status=_runtime_status(readiness.get("status")),
        health=compact_mapping(
            {
                "connectivity": _connectivity_for_state(readiness.get("status")),
            }
        ),
        relations={RelationKind.CONNECTED_TO.value: [subject.id]},
        runtime=compact_mapping(
            {
                "root_control": readiness,
                "selected_server": selected_server,
            }
        ),
        actual_state=compact_mapping(
            {
                "configured_zone_id": zone.get("configured_zone_id"),
                "active_zone_id": zone.get("active_zone_id"),
                "selected_server": selected_server,
            }
        ),
    )


def _integration_quota_objects(subject: CanonicalObject, runtime: dict[str, Any]) -> list[CanonicalObject]:
    hub_root_protocol = coerce_mapping(runtime.get("hub_root_protocol"))
    zone = coerce_mapping(runtime.get("hub_root_zone"))
    outboxes = coerce_mapping(hub_root_protocol.get("integration_outboxes"))
    node_id = str(subject.id.partition(":")[2] or subject.id).strip() or subject.id
    root_token = str(zone.get("active_zone_id") or zone.get("configured_zone_id") or "default").strip() or "default"
    root_id = canonical_ref(CanonicalKind.ROOT, root_token) or f"root:{root_token}"
    objects: list[CanonicalObject] = []
    for name, entry in sorted(outboxes.items()):
        if not isinstance(entry, dict):
            continue
        payload = dict(entry)
        payload.setdefault("name", str(name))
        objects.append(canonical_object_from_integration_quota(payload, node_id=node_id, root_id=root_id))
    return objects


def _traffic_budget_objects(subject: CanonicalObject, runtime: dict[str, Any]) -> list[CanonicalObject]:
    hub_root_protocol = coerce_mapping(runtime.get("hub_root_protocol"))
    zone = coerce_mapping(runtime.get("hub_root_zone"))
    traffic_classes = coerce_mapping(hub_root_protocol.get("traffic_classes"))
    node_id = str(subject.id.partition(":")[2] or subject.id).strip() or subject.id
    root_token = str(zone.get("active_zone_id") or zone.get("configured_zone_id") or "default").strip() or "default"
    root_id = canonical_ref(CanonicalKind.ROOT, root_token) or f"root:{root_token}"
    objects: list[CanonicalObject] = []
    for name, entry in sorted(traffic_classes.items()):
        if not isinstance(entry, dict):
            continue
        payload = dict(entry)
        payload.setdefault("traffic_class", str(name))
        objects.append(canonical_object_from_protocol_traffic_budget(payload, node_id=node_id, root_id=root_id))
    return objects


def _root_control_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    readiness_tree = coerce_mapping(runtime.get("readiness_tree"))
    channel_diagnostics = coerce_mapping(runtime.get("channel_diagnostics"))
    zone = coerce_mapping(runtime.get("hub_root_zone"))
    readiness = coerce_mapping(readiness_tree.get("root_control"))
    diagnostics = coerce_mapping(channel_diagnostics.get("root_control"))
    stability = coerce_mapping(diagnostics.get("stability"))
    status = _runtime_status(readiness.get("status"), stability.get("state"))
    summary = str(readiness.get("summary") or "").strip() or "Control-plane path between node and root"
    root_token = str(zone.get("active_zone_id") or zone.get("configured_zone_id") or "default").strip() or "default"
    return CanonicalObject(
        id=f"connection:{subject.id}/root-control",
        kind=CanonicalKind.CONNECTION.value,
        title="Root control channel",
        summary=summary,
        status=status,
        health=compact_mapping(
            {
                "connectivity": _connectivity_for_state(readiness.get("status"), stability.get("state")),
                "stability": stability.get("state"),
                "stability_score": stability.get("score"),
            }
        ),
        relations=compact_mapping(
            {
                RelationKind.HOSTED_ON.value: [subject.id],
                RelationKind.CONNECTED_TO.value: [canonical_ref(CanonicalKind.ROOT, root_token) or f"root:{root_token}"],
            }
        ),
        runtime=compact_mapping(
            {
                "readiness": readiness,
                "recent_transitions_5m": diagnostics.get("recent_transitions_5m"),
                "recent_non_ready_transitions_5m": diagnostics.get("recent_non_ready_transitions_5m"),
            }
        ),
        actual_state=compact_mapping(
            {
                "selected_server": zone.get("selected_server"),
                "configured_zone_id": zone.get("configured_zone_id"),
                "active_zone_id": zone.get("active_zone_id"),
            }
        ),
    )


def _route_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    readiness_tree = coerce_mapping(runtime.get("readiness_tree"))
    channel_diagnostics = coerce_mapping(runtime.get("channel_diagnostics"))
    channel_overview = coerce_mapping(runtime.get("channel_overview"))
    readiness = coerce_mapping(readiness_tree.get("route"))
    diagnostics = coerce_mapping(channel_diagnostics.get("route"))
    stability = coerce_mapping(diagnostics.get("stability"))
    status = _runtime_status(readiness.get("status"), stability.get("state"))
    summary = str(readiness.get("summary") or "").strip() or "Runtime route channel"
    return CanonicalObject(
        id=f"connection:{subject.id}/route",
        kind=CanonicalKind.CONNECTION.value,
        title="Route channel",
        summary=summary,
        status=status,
        health=compact_mapping(
            {
                "connectivity": _connectivity_for_state(readiness.get("status"), stability.get("state")),
                "stability": stability.get("state"),
                "stability_score": stability.get("score"),
            }
        ),
        relations={RelationKind.HOSTED_ON.value: [subject.id]},
        runtime=compact_mapping(
            {
                "readiness": readiness,
                "diagnostics": diagnostics,
                "overview": channel_overview.get("route"),
            }
        ),
    )


def _sidecar_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    payload = coerce_mapping(runtime.get("sidecar_runtime"))
    enabled = bool(payload.get("enabled"))
    status = _runtime_status(payload.get("status"), "disabled" if not enabled else None)
    actions = [
        CanonicalActionDescriptor(
            id="restart_sidecar",
            title="restart sidecar",
            risk="low",
            metadata={"api_path": "/api/node/sidecar/restart"},
        )
    ]
    if str(subject.kind or "").strip().lower() == "hub":
        actions.append(
            CanonicalActionDescriptor(
                id="reconnect_root",
                title="reconnect root",
                risk="low",
                metadata={"api_path": "/api/node/hub-root/reconnect"},
            )
        )
    return CanonicalObject(
        id=f"runtime:{subject.id}/sidecar",
        kind=CanonicalKind.RUNTIME.value,
        title="Realtime sidecar",
        summary=str(payload.get("summary") or "Sidecar transport runtime"),
        status=status,
        health=compact_mapping(
            {
                "connectivity": _connectivity_for_state(payload.get("remote_session_state"), payload.get("control_ready")),
                "availability": _canonical_status_token(payload.get("status")),
            }
        ),
        relations={RelationKind.HOSTED_ON.value: [subject.id]},
        runtime=compact_mapping(
            {
                "enabled": enabled,
                "phase": payload.get("phase"),
                "transport_owner": payload.get("transport_owner"),
                "lifecycle_manager": payload.get("lifecycle_manager"),
                "local_listener_state": payload.get("local_listener_state"),
                "remote_session_state": payload.get("remote_session_state"),
                "transport_ready": payload.get("transport_ready"),
                "control_ready": payload.get("control_ready"),
                "route_ready": payload.get("route_ready"),
                "sync_ready": payload.get("sync_ready"),
                "media_ready": payload.get("media_ready"),
                "process": payload.get("process"),
            }
        ),
        actual_state=compact_mapping(
            {
                "local_url": payload.get("local_url"),
                "diag_path": payload.get("diag_path"),
                "diag_age_s": payload.get("diag_age_s"),
                "scope": payload.get("scope"),
                "continuity_contract": payload.get("continuity_contract"),
                "progress": payload.get("progress"),
                "route_tunnel_contract": payload.get("route_tunnel_contract"),
                "transport_provenance": payload.get("transport_provenance"),
            }
        ),
        actions=actions,
    )


def _sync_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    payload = coerce_mapping(runtime.get("sync_runtime"))
    assessment = coerce_mapping(payload.get("assessment"))
    selected_webspace = coerce_mapping(payload.get("selected_webspace"))
    selected_webspace_id = str(payload.get("selected_webspace_id") or selected_webspace.get("webspace_id") or "").strip() or None
    relations = {RelationKind.HOSTED_ON.value: [subject.id]}
    if selected_webspace_id:
        relations[RelationKind.WORKSPACE.value] = [
            canonical_ref(CanonicalKind.WORKSPACE, selected_webspace_id) or f"workspace:{selected_webspace_id}"
        ]
    return CanonicalObject(
        id=f"runtime:{subject.id}/yjs-sync",
        kind=CanonicalKind.RUNTIME.value,
        title="Yjs sync runtime",
        summary=str(assessment.get("reason") or "Yjs bounded replay and recovery state"),
        status=_runtime_status(assessment.get("state")),
        health=compact_mapping(
            {
                "availability": _canonical_status_token(assessment.get("state")),
                "connectivity": _connectivity_for_state(coerce_mapping(payload.get("transport")).get("server_ready")),
            }
        ),
        relations=relations,
        resources=compact_mapping(
            {
                "webspace_total": payload.get("webspace_total"),
                "active_webspace_total": payload.get("active_webspace_total"),
                "compacted_webspace_total": payload.get("compacted_webspace_total"),
                "update_log_total": payload.get("update_log_total"),
                "replay_window_total": payload.get("replay_window_total"),
            }
        ),
        runtime=compact_mapping(
            {
                "available": payload.get("available"),
                "scope": payload.get("scope"),
                "assessment": assessment,
                "channel_contract": payload.get("channel_contract"),
                "transport": payload.get("transport"),
                "ownership_boundaries": payload.get("ownership_boundaries"),
                "update_guard": payload.get("update_guard"),
                "recovery_guidance": payload.get("recovery_guidance"),
                "recovery_playbook": payload.get("recovery_playbook"),
                "webspace_guidance": payload.get("webspace_guidance"),
            }
        ),
        actual_state=compact_mapping(
            {
                "selected_webspace_id": selected_webspace_id,
                "selected_webspace": selected_webspace,
                "channel_contract": coerce_mapping(payload.get("channel_contract")),
                "transport_ownership": coerce_mapping(payload.get("transport")),
                "ownership_boundaries": coerce_mapping(payload.get("ownership_boundaries")),
            }
        ),
        actions=_actions_from_yjs_overrides(coerce_mapping(payload.get("action_overrides")), webspace_id=selected_webspace_id),
    )


def _media_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    payload = coerce_mapping(runtime.get("media_runtime"))
    assessment = coerce_mapping(payload.get("assessment"))
    transport = coerce_mapping(payload.get("transport"))
    counts = coerce_mapping(payload.get("counts"))
    route_intent = coerce_mapping(payload.get("route_intent"))
    attempt = coerce_mapping(payload.get("attempt") or route_intent.get("attempt"))
    monitoring = coerce_mapping(payload.get("monitoring") or route_intent.get("monitoring"))
    member_browser_direct = coerce_mapping(payload.get("member_browser_direct"))
    return CanonicalObject(
        id=f"runtime:{subject.id}/media-plane",
        kind=CanonicalKind.RUNTIME.value,
        title="Media plane",
        summary=str(assessment.get("reason") or "Local and root-routed media runtime"),
        status=_runtime_status(assessment.get("state")),
        health=compact_mapping(
            {
                "availability": _canonical_status_token(assessment.get("state")),
                "connectivity": _connectivity_for_state(
                    transport.get("direct_local_ready"),
                    transport.get("root_routed_ready"),
                    transport.get("broadcast_ready"),
                ),
            }
        ),
        relations={RelationKind.HOSTED_ON.value: [subject.id]},
        resources=compact_mapping(
            {
                "file_total": counts.get("file_total"),
                "total_bytes": counts.get("total_bytes"),
                "live_peer_total": counts.get("live_peer_total"),
                "live_connected_peers": counts.get("live_connected_peers"),
            }
        ),
        runtime=compact_mapping(
            {
                "available": payload.get("available"),
                "scope": payload.get("scope"),
                "assessment": assessment,
                "transport": transport,
                "recommended_path": payload.get("recommended_path"),
                "route_intent": route_intent,
                "active_route": payload.get("active_route") or route_intent.get("active_route"),
                "preferred_route": payload.get("preferred_route") or route_intent.get("preferred_route"),
                "preferred_member_id": payload.get("preferred_member_id") or route_intent.get("preferred_member_id"),
                "producer_authority": payload.get("producer_authority"),
                "producer_target": payload.get("producer_target"),
                "delivery_topology": payload.get("delivery_topology"),
                "selection_reason": payload.get("selection_reason"),
                "degradation_reason": payload.get("degradation_reason"),
                "fallback_chain": payload.get("fallback_chain") or route_intent.get("fallback_chain"),
                "attempt": attempt,
                "monitoring": monitoring,
                "member_browser_direct": member_browser_direct,
                "update_guard": payload.get("update_guard"),
            }
        ),
    )


def canonical_neighborhood_projection(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    title: str = "Local control-plane neighborhood",
    summary: str | None = None,
) -> CanonicalProjection:
    kind_totals: dict[str, int] = {}
    peer_node_ids: list[str] = []
    online_peer_total = 0
    incidents: list[dict[str, Any]] = []
    subnet_planning_nodes = _subnet_planning_nodes(subject, objects)
    subnet_runtime_summary = _subnet_planning_summary(subnet_planning_nodes)
    for obj in objects:
        kind = str(obj.kind or "unknown").strip() or "unknown"
        kind_totals[kind] = int(kind_totals.get(kind) or 0) + 1
        if kind in {CanonicalKind.NODE.value, CanonicalKind.HUB.value, CanonicalKind.MEMBER.value}:
            peer_node_ids.append(obj.id)
            if obj.status == CanonicalStatus.ONLINE:
                online_peer_total += 1
        incident = _incident_from_object(obj)
        if incident:
            incidents.append(incident)

    effective_summary = summary or (
        f"{subject.title} neighborhood with {len(peer_node_ids)} peer nodes and {len(objects)} related objects"
    )
    context = compact_mapping(
        {
            "kind_totals": kind_totals,
            "peer_total": len(peer_node_ids),
            "online_peer_total": online_peer_total,
            "incident_total": len(incidents),
            "peer_node_ids": peer_node_ids,
            "subnet_runtime_summary": subnet_runtime_summary,
        }
    )
    return CanonicalProjection(
        id=f"projection:{subject.id}/neighborhood",
        kind="neighborhood",
        title=title,
        subject=subject,
        summary=effective_summary,
        objects=objects,
        incidents=incidents,
        context=context,
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "peer_node_ids": peer_node_ids,
                    "object_ids": [obj.id for obj in objects],
                    "incident_total": len(incidents),
                    "subnet_runtime_summary": subnet_runtime_summary,
                }
            }
        ),
    )


def canonical_inventory_projection(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    title: str = "Local control-plane inventory",
    summary: str | None = None,
) -> CanonicalProjection:
    kind_totals: dict[str, int] = {}
    incidents: list[dict[str, Any]] = []
    for obj in objects:
        kind = str(obj.kind or "unknown").strip() or "unknown"
        kind_totals[kind] = int(kind_totals.get(kind) or 0) + 1
        incident = _incident_from_object(obj)
        if incident:
            incidents.append(incident)

    ordered_kinds = ", ".join(f"{kind}:{kind_totals[kind]}" for kind in sorted(kind_totals))
    effective_summary = summary or (f"{subject.title} inventory with {len(objects)} objects" + (f" ({ordered_kinds})" if ordered_kinds else ""))
    context = compact_mapping(
        {
            "kind_totals": kind_totals,
            "incident_total": len(incidents),
        }
    )
    llm_repr = compact_mapping(
        {
            "subject_id": subject.id,
            "object_ids": [obj.id for obj in objects],
            "kind_totals": kind_totals,
            "incident_total": len(incidents),
        }
    )
    return CanonicalProjection(
        id=f"projection:{subject.id}/inventory",
        kind="inventory",
        title=title,
        subject=subject,
        summary=effective_summary,
        objects=objects,
        incidents=incidents,
        context=context,
        representations={"llm": llm_repr},
    )


def canonical_overview_projection(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    title: str = "Infrascope overview",
    summary: str | None = None,
) -> CanonicalProjection:
    universe = [subject, *objects]
    incidents = sorted(_collect_incidents(subject, objects), key=_incident_sort_key)
    health_strip = _health_strip_items(subject, objects)
    quota_summary = _quota_summary_items(objects)
    active_runtimes = _runtime_summary_items(objects)
    recent_changes = _recent_change_items(subject, objects)
    kind_totals = _kind_totals(universe)
    overall_status = _worst_status(universe)
    effective_summary = summary or (
        f"{subject.title} overview with {len(universe)} objects, {len(incidents)} incidents, and {len(recent_changes)} recent changes"
    )
    summary_tile = compact_mapping(
        {
            "label": "Control plane",
            "value": overall_status,
            "subtitle": f"{subject.title} | {len(universe)} objects",
            "description": f"{len(incidents)} incidents | {len(quota_summary)} quotas | {len(active_runtimes)} runtimes",
        }
    )
    return CanonicalProjection(
        id=f"projection:{subject.id}/overview",
        kind="overview",
        title=title,
        subject=subject,
        summary=effective_summary,
        objects=objects,
        incidents=incidents,
        context=compact_mapping(
            {
                "summary_tile": summary_tile,
                "overall_status": overall_status,
                "kind_totals": kind_totals,
                "health_strip": health_strip,
                "active_incidents": incidents,
                "quota_summary": quota_summary,
                "active_runtimes": active_runtimes,
                "recent_changes": recent_changes,
            }
        ),
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "overall_status": overall_status,
                    "incident_total": len(incidents),
                    "kind_totals": kind_totals,
                },
                "operator": {
                    "summary_tile": summary_tile,
                    "health_strip": health_strip,
                    "active_incidents": incidents,
                    "quota_summary": quota_summary,
                    "active_runtimes": active_runtimes,
                    "recent_changes": recent_changes,
                },
            }
        ),
    )


def canonical_object_projection(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    title: str | None = None,
    summary: str | None = None,
) -> CanonicalProjection:
    incidents = _collect_incidents(subject, objects)
    narrative = _narrative_projection(subject, objects, incidents)
    actions = _action_projection(subject, objects)
    effective_title = title or f"{subject.title} object"
    effective_summary = summary or str(narrative.get("summary") or subject.summary or subject.title)
    return CanonicalProjection(
        id=f"projection:{subject.id}/object",
        kind="object",
        title=effective_title,
        subject=subject,
        summary=effective_summary,
        objects=objects,
        incidents=incidents,
        context=compact_mapping(
            {
                "narrative": narrative,
                "action_total": len(actions),
                "incident_total": len(incidents),
            }
        ),
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "narrative": narrative,
                    "allowed_action_ids": [item.get("id") for item in actions],
                },
                "operator": {
                    "actions": actions,
                },
            }
        ),
    )


def canonical_object_inspector(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    task_goal: str | None = None,
    title: str | None = None,
    summary: str | None = None,
) -> CanonicalProjection:
    incidents = sorted(_collect_incidents(subject, objects), key=_incident_sort_key)
    narrative = _narrative_projection(subject, objects, incidents)
    actions = _action_projection(subject, objects)
    edges = _topology_edges(subject, objects)
    recent_changes = _recent_change_items(subject, objects, limit=8)
    task_packet = canonical_task_packet(subject, objects, task_goal=task_goal).to_dict()
    task_packet_context = coerce_mapping(task_packet.get("context"))
    subnet_planning = coerce_mapping(task_packet_context.get("subnet_planning"))
    inspector_payload = compact_mapping(
        {
            "label": subject.kind,
            "value": _status_token(subject.status),
            "subtitle": subject.title,
            "description": narrative.get("current_issue") or subject.summary,
            "object": subject.to_dict(),
            "incidents": incidents,
            "actions": actions,
            "recent_changes": recent_changes,
            "topology": {"edges": edges},
            "task_packet": task_packet,
            "subnet_planning": subnet_planning,
        }
    )
    effective_title = title or f"{subject.title} inspector"
    effective_summary = summary or str(narrative.get("summary") or subject.summary or subject.title)
    return CanonicalProjection(
        id=f"projection:{subject.id}/inspector",
        kind="inspector",
        title=effective_title,
        subject=subject,
        summary=effective_summary,
        objects=objects,
        incidents=incidents,
        context=compact_mapping(
            {
                "narrative": narrative,
                "actions": actions,
                "recent_changes": recent_changes,
                "topology": {"edges": edges},
                "task_packet": task_packet,
                "subnet_planning": subnet_planning,
                "inspector": inspector_payload,
            }
        ),
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "allowed_action_ids": [item.get("id") for item in actions],
                    "incident_total": len(incidents),
                },
                "operator": inspector_payload,
            }
        ),
    )


def canonical_topology_projection(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    title: str | None = None,
    summary: str | None = None,
) -> CanonicalProjection:
    incidents = _collect_incidents(subject, objects)
    edges = _topology_edges(subject, objects)
    effective_title = title or f"{subject.title} topology"
    effective_summary = summary or f"{subject.title} topology with {len(objects) + 1} nodes and {len(edges)} edges"
    return CanonicalProjection(
        id=f"projection:{subject.id}/topology",
        kind="topology",
        title=effective_title,
        subject=subject,
        summary=effective_summary,
        objects=objects,
        incidents=incidents,
        context=compact_mapping(
            {
                "node_ids": [subject.id, *[item.id for item in objects]],
                "edge_total": len(edges),
                "view_modes": ["physical", "connectivity", "runtime", "resource", "capability"],
            }
        ),
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "edges": edges,
                },
                "operator": {
                    "edges": edges,
                },
            }
        ),
    )


def canonical_task_packet(
    subject: CanonicalObject,
    objects: list[CanonicalObject],
    *,
    task_goal: str | None = None,
    title: str | None = None,
) -> CanonicalProjection:
    incidents = _collect_incidents(subject, objects)
    narrative = _narrative_projection(subject, objects, incidents)
    actions = _action_projection(subject, objects)
    goal = str(task_goal or "").strip() or "diagnose current state"
    policy_context = compact_mapping(
        {
            "tenant_id": subject.governance.tenant_id,
            "owner_id": subject.governance.owner_id,
            "visibility": list(subject.governance.visibility or []),
            "roles_allowed": list(subject.governance.roles_allowed or []),
        }
    )
    constraints = compact_mapping(
        {
            "roles_allowed": list(subject.governance.roles_allowed or []),
            "visibility": list(subject.governance.visibility or []),
        }
    )
    gap = _state_gap(subject)
    subnet_planning_nodes = _subnet_planning_nodes(subject, objects)
    subnet_planning = compact_mapping(
        {
            "summary": _subnet_planning_summary(subnet_planning_nodes),
            "nodes": subnet_planning_nodes,
        }
    )
    return CanonicalProjection(
        id=f"projection:{subject.id}/task-packet",
        kind="task_packet",
        title=title or f"{subject.title} task packet",
        subject=subject,
        summary=f"Task packet for {subject.title}: {goal}",
        objects=objects,
        incidents=incidents,
        context=compact_mapping(
            {
                "task_goal": goal,
                "local_object": subject.to_dict(),
                "neighborhood": [item.to_dict() for item in objects],
                "policy_context": policy_context,
                "allowed_actions": actions,
                "relevant_incidents": incidents,
                "recent_changes": [],
                "desired_state": subject.desired_state,
                "actual_state": subject.actual_state,
                "gap": gap,
                "constraints": constraints,
                "narrative": narrative,
                "subnet_planning": subnet_planning,
            }
        ),
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "task_goal": goal,
                    "policy_context": policy_context,
                    "allowed_action_ids": [item.get("id") for item in actions],
                    "incident_total": len(incidents),
                    "subnet_planning_summary": subnet_planning.get("summary"),
                }
            }
        ),
    )


def canonical_projection_from_reliability_snapshot(payload: Any) -> CanonicalProjection:
    data = coerce_mapping(payload)
    node_data = coerce_mapping(data.get("node"))
    runtime = coerce_mapping(data.get("runtime"))
    subject = canonical_object_from_node_status(node_data)

    focus_context = _reliability_focus_context(runtime)
    subject.health.update(focus_context.get("readiness") if isinstance(focus_context.get("readiness"), dict) else {})
    subject.runtime.update(
        compact_mapping(
            {
                "blocked_capabilities": focus_context.get("blocked_capabilities"),
                "hub_root_zone": focus_context.get("hub_root_zone"),
            }
        )
    )
    llm_repr = coerce_mapping(subject.representations.get("llm"))
    llm_repr["reliability_focus"] = focus_context
    subject.representations["llm"] = compact_mapping(llm_repr)

    objects = [
        _root_object(subject, runtime),
        _root_control_object(subject, runtime),
        _route_object(subject, runtime),
        _sidecar_object(subject, runtime),
        _sync_object(subject, runtime),
        _media_object(subject, runtime),
        *_traffic_budget_objects(subject, runtime),
        *_integration_quota_objects(subject, runtime),
    ]
    incidents = [item for item in (_incident_from_object(obj) for obj in objects) if item]
    subject.incidents = incidents
    blocked_capabilities = focus_context.get("blocked_capabilities") if isinstance(focus_context.get("blocked_capabilities"), list) else []
    subject.summary = (
        f"{subject.summary}; blocked capabilities: {', '.join(blocked_capabilities)}"
        if blocked_capabilities
        else subject.summary
    )

    return CanonicalProjection(
        id=f"projection:{subject.id}/reliability",
        kind="reliability",
        title=f"{subject.title} reliability",
        subject=subject,
        summary="Canonical control-plane projection over the node reliability runtime",
        objects=objects,
        incidents=incidents,
        context=focus_context,
        representations=compact_mapping(
            {
                "llm": {
                    "subject_id": subject.id,
                    "component_ids": [obj.id for obj in objects],
                    "blocked_capabilities": blocked_capabilities,
                    "incident_total": len(incidents),
                }
            }
        ),
    )


__all__ = [
    "canonical_overview_projection",
    "canonical_object_inspector",
    "canonical_object_projection",
    "canonical_inventory_projection",
    "canonical_neighborhood_projection",
    "canonical_task_packet",
    "canonical_topology_projection",
    "canonical_projection_from_reliability_snapshot",
]
