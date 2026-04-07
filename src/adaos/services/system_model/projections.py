from __future__ import annotations

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
                "transport": payload.get("transport"),
                "recovery_guidance": payload.get("recovery_guidance"),
                "recovery_playbook": payload.get("recovery_playbook"),
                "webspace_guidance": payload.get("webspace_guidance"),
            }
        ),
        actual_state=compact_mapping(
            {
                "selected_webspace_id": selected_webspace_id,
                "selected_webspace": selected_webspace,
            }
        ),
        actions=_actions_from_yjs_overrides(coerce_mapping(payload.get("action_overrides")), webspace_id=selected_webspace_id),
    )


def _media_object(subject: CanonicalObject, runtime: dict[str, Any]) -> CanonicalObject:
    payload = coerce_mapping(runtime.get("media_runtime"))
    assessment = coerce_mapping(payload.get("assessment"))
    transport = coerce_mapping(payload.get("transport"))
    counts = coerce_mapping(payload.get("counts"))
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
    "canonical_inventory_projection",
    "canonical_neighborhood_projection",
    "canonical_projection_from_reliability_snapshot",
]
