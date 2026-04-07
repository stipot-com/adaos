from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from adaos.services.system_model import (
    CANONICAL_KIND_REGISTRY,
    CANONICAL_RELATION_REGISTRY,
    CanonicalKind,
    CanonicalObject,
    RelationKind,
    canonical_ref,
    CanonicalStatus,
    canonical_inventory_projection,
    canonical_object_from_browser_session,
    canonical_object_from_capacity_snapshot,
    canonical_object_from_device_endpoint,
    canonical_object_from_integration_quota,
    canonical_object_from_io_capacity_entry,
    canonical_object_from_node_status,
    canonical_projection_from_reliability_snapshot,
    canonical_object_from_skill_status,
    canonical_object_from_user_profile,
    canonical_object_from_workspace_manifest,
    normalize_kind,
    normalize_relation_kind,
    normalize_connectivity_status,
    normalize_operational_status,
)


class _NodeStatusModel(BaseModel):
    node_id: str
    subnet_id: str
    role: str
    node_names: list[str]
    primary_node_name: str
    ready: bool
    node_state: str
    draining: bool
    route_mode: str | None = None
    connected_to_hub: bool | None = None


def test_normalize_operational_status_maps_common_runtime_terms() -> None:
    assert normalize_operational_status("ready") == CanonicalStatus.ONLINE
    assert normalize_operational_status("down") == CanonicalStatus.OFFLINE
    assert normalize_operational_status("degraded") == CanonicalStatus.DEGRADED
    assert normalize_operational_status("draining") == CanonicalStatus.WARNING
    assert normalize_operational_status("not_applicable") == CanonicalStatus.UNKNOWN


def test_kind_and_relation_registry_normalize_and_build_refs() -> None:
    assert CANONICAL_KIND_REGISTRY["device"] == CanonicalKind.DEVICE
    assert CANONICAL_RELATION_REGISTRY["hosted_on"] == RelationKind.HOSTED_ON
    assert normalize_kind("BROWSER_SESSION") == "browser_session"
    assert normalize_relation_kind(RelationKind.WORKSPACE) == "workspace"
    assert canonical_ref(CanonicalKind.QUOTA, "telegram-outbox") == "quota:telegram-outbox"


def test_normalize_connectivity_status_handles_bool_and_transport_tokens() -> None:
    assert normalize_connectivity_status(True).value == "reachable"
    assert normalize_connectivity_status(False).value == "unreachable"
    assert normalize_connectivity_status("ws").value == "reachable"
    assert normalize_connectivity_status("open").value == "reachable"
    assert normalize_connectivity_status("none").value == "unreachable"


def test_canonical_object_from_node_status_accepts_pydantic_payload() -> None:
    payload = _NodeStatusModel(
        node_id="member-1",
        subnet_id="subnet-a",
        role="member",
        node_names=["Kitchen member"],
        primary_node_name="Kitchen member",
        ready=True,
        node_state="ready",
        draining=False,
        route_mode="ws",
        connected_to_hub=True,
    )

    obj = canonical_object_from_node_status(payload).to_dict()

    assert obj["id"] == "member:member-1"
    assert obj["kind"] == "member"
    assert obj["status"] == "online"
    assert obj["relations"]["subnet"] == ["subnet:subnet-a"]
    assert obj["health"]["connectivity"] == "reachable"


def test_canonical_object_from_node_status_marks_draining_as_warning() -> None:
    obj = canonical_object_from_node_status(
        {
            "node_id": "hub-1",
            "subnet_id": "main",
            "role": "hub",
            "primary_node_name": "Hub Alpha",
            "ready": False,
            "node_state": "ready",
            "draining": True,
            "route_mode": "hub",
        }
    ).to_dict()

    assert obj["id"] == "hub:hub-1"
    assert obj["status"] == "warning"
    assert obj["runtime"]["draining"] is True


def test_canonical_object_from_skill_status_tracks_slot_and_version_drift() -> None:
    obj = canonical_object_from_skill_status(
        {
            "name": "weather_skill",
            "version": "1.0.0",
            "slot": "slot-a",
            "remote_version": "1.1.0",
            "update_available": True,
        }
    ).to_dict()

    assert obj["id"] == "skill:weather_skill"
    assert obj["status"] == "warning"
    assert obj["runtime"]["installation_status"] == "active"
    assert obj["versioning"]["drift"] is True


def test_canonical_object_from_workspace_manifest_uses_effective_properties() -> None:
    manifest = SimpleNamespace(
        workspace_id="desk",
        title="DEV: desk",
        effective_kind="dev",
        effective_home_scenario="web_desktop",
        effective_source_mode="dev",
        owner_scope="profile:ops",
        profile_scope="role:developer",
    )

    obj = canonical_object_from_workspace_manifest(manifest).to_dict()

    assert obj["id"] == "workspace:desk"
    assert obj["title"] == "DEV: desk"
    assert obj["relations"]["home_scenario"] == ["scenario:web_desktop"]
    assert obj["governance"]["owner_id"] == "profile:ops"


def test_canonical_object_from_workspace_manifest_includes_binding_and_overlay_state() -> None:
    manifest = SimpleNamespace(
        workspace_id="kitchen",
        title="Kitchen",
        effective_kind="workspace",
        effective_home_scenario="home",
        effective_source_mode="workspace",
        owner_scope="profile:owner",
        profile_scope="role:operator",
        device_binding="tablet-kitchen",
        has_ui_overlay=True,
        has_installed_overlay=True,
        has_pinned_widgets_overlay=False,
    )

    obj = canonical_object_from_workspace_manifest(manifest).to_dict()

    assert obj["relations"]["device_binding"] == ["device:tablet-kitchen"]
    assert obj["runtime"]["overlay"]["has_ui_overlay"] is True
    assert obj["actual_state"]["device_binding"] == "tablet-kitchen"


def test_canonical_object_from_user_profile_uses_preferred_name() -> None:
    obj = canonical_object_from_user_profile(
        SimpleNamespace(user_id="u-1", settings={"preferred_name": "Ada", "locale": "ru-RU"})
    ).to_dict()

    assert obj["id"] == "profile:u-1"
    assert obj["title"] == "Ada"
    assert obj["actual_state"]["settings"]["locale"] == "ru-RU"


def test_canonical_object_from_browser_session_tracks_workspace_and_channels() -> None:
    obj = canonical_object_from_browser_session(
        {
            "device_id": "browser-a",
            "webspace_id": "desk",
            "connection_state": "connected",
            "events_channel_state": "open",
            "yjs_channel_state": "open",
            "incoming_video_tracks": 1,
        }
    ).to_dict()

    assert obj["id"] == "browser:browser-a"
    assert obj["kind"] == "browser_session"
    assert obj["relations"]["workspace"] == ["workspace:desk"]
    assert obj["health"]["connectivity"] == "reachable"
    assert obj["runtime"]["incoming_video_tracks"] == 1


def test_canonical_object_from_device_endpoint_merges_workspace_and_session_links() -> None:
    obj = canonical_object_from_device_endpoint(
        {
            "device_id": "tablet-kitchen",
            "device_kind": "browser",
            "workspace_ids": ["kitchen"],
            "session_ids": ["browser:tablet-kitchen"],
            "online": True,
            "source": "merged",
        }
    ).to_dict()

    assert obj["id"] == "device:tablet-kitchen"
    assert obj["kind"] == "device"
    assert obj["relations"]["workspace"] == ["workspace:kitchen"]
    assert obj["relations"]["connected_to"] == ["browser:tablet-kitchen"]
    assert obj["health"]["connectivity"] == "reachable"


def test_canonical_object_from_capacity_snapshot_summarizes_local_inventory() -> None:
    obj = canonical_object_from_capacity_snapshot(
        {
            "io": [{"io_type": "stdout"}, {"io_type": "say"}],
            "skills": [{"name": "weather", "active": True}, {"name": "music", "active": False}],
            "scenarios": [{"name": "home", "active": True}],
        },
        node_id="node-7",
    ).to_dict()

    assert obj["id"] == "capacity:node-7"
    assert obj["resources"]["io_total"] == 2
    assert obj["resources"]["active_skill_total"] == 1
    assert obj["runtime"]["skills"] == ["weather", "music"]


def test_canonical_object_from_io_capacity_entry_tracks_availability_tokens() -> None:
    obj = canonical_object_from_io_capacity_entry(
        {
            "io_type": "telegram",
            "priority": 60,
            "capabilities": ["text", "state:unavailable", "mode:webhook", "reason:token_missing"],
        },
        node_id="node-7",
    ).to_dict()

    assert obj["id"] == "io:node-7:telegram"
    assert obj["status"] == "offline"
    assert obj["runtime"]["mode"] == "webhook"
    assert obj["runtime"]["reason"] == "token_missing"


def test_canonical_object_from_integration_quota_exposes_usage_and_pressure() -> None:
    obj = canonical_object_from_integration_quota(
        {
            "name": "telegram",
            "size": 12,
            "max_size": 10,
            "durable_store": False,
            "publish_fail": 2,
            "publish_ok": 1,
            "last_error": "network timeout",
            "updated_ago_s": 3.1,
        },
        node_id="hub-1",
        root_id="root:eu",
    ).to_dict()

    assert obj["id"] == "quota:telegram-outbox"
    assert obj["kind"] == "quota"
    assert obj["status"] == "warning"
    assert obj["relations"]["connected_to"] == ["root:eu"]
    assert obj["resources"]["used"] == 12


def test_canonical_projection_from_reliability_snapshot_builds_runtime_components() -> None:
    projection = canonical_projection_from_reliability_snapshot(
        {
            "node": {
                "node_id": "hub-1",
                "subnet_id": "main",
                "role": "hub",
                "node_names": ["Hub Alpha"],
                "ready": True,
                "node_state": "ready",
                "draining": False,
                "route_mode": "hub",
            },
            "runtime": {
                "readiness_tree": {
                    "hub_local_core": {"status": "ready"},
                    "root_control": {"status": "ready", "summary": "root control is healthy"},
                    "route": {"status": "ready", "summary": "route tunnels are healthy"},
                    "sync": {"status": "ready"},
                    "media": {"status": "unknown"},
                },
                "degraded_matrix": {
                    "root_routed_browser_proxy": {"allowed": False},
                    "execute_local_scenarios": {"allowed": True},
                },
                "channel_diagnostics": {
                    "root_control": {"stability": {"state": "stable", "score": 98}, "recent_transitions_5m": 1},
                    "route": {"stability": {"state": "stable", "score": 93}, "recent_transitions_5m": 1},
                },
                "hub_root_zone": {
                    "configured_zone_id": "eu",
                    "active_zone_id": "eu",
                    "selected_server": "wss://api.inimatic.com/nats",
                },
                "sidecar_runtime": {
                    "enabled": True,
                    "status": "ready",
                    "summary": "sidecar remote session is connected",
                    "phase": "nats_transport_sidecar",
                    "local_listener_state": "ready",
                    "remote_session_state": "ready",
                    "transport_ready": True,
                    "control_ready": "ready",
                    "route_ready": "not_owned",
                    "sync_ready": "not_owned",
                    "media_ready": "not_owned",
                    "process": {"pid": 41},
                    "transport_provenance": {"selected_server": "wss://api.inimatic.com/nats"},
                },
                "sync_runtime": {
                    "available": True,
                    "scope": "hub_local_only",
                    "selected_webspace_id": "desk",
                    "assessment": {"state": "nominal", "reason": "bounded sync runtime observed"},
                    "transport": {"server_ready": True},
                    "action_overrides": {
                        "backup": {"enabled": True, "reason": "persist current state", "source_of_truth": "current_runtime"},
                        "reset": {"enabled": True, "reason": "hard reset", "source_of_truth": "scenario"},
                    },
                    "recovery_guidance": {"recommended_action": "backup"},
                    "recovery_playbook": {"default_action": "reload"},
                    "webspace_guidance": {"recommended_action": "go_home"},
                    "selected_webspace": {"webspace_id": "desk", "home_scenario": "web_desktop"},
                    "webspace_total": 2,
                    "active_webspace_total": 1,
                    "compacted_webspace_total": 1,
                    "update_log_total": 12,
                    "replay_window_total": 4,
                },
                "media_runtime": {
                    "available": True,
                    "scope": "hub_media",
                    "assessment": {
                        "state": "bounded_relay_available",
                        "reason": "media plane supports direct-local authority and bounded root relay authority",
                    },
                    "transport": {"direct_local_ready": True, "root_routed_ready": True, "broadcast_ready": False},
                    "counts": {"file_total": 2, "total_bytes": 1024, "live_peer_total": 0, "live_connected_peers": 0},
                    "recommended_path": "direct_local_http",
                },
                "hub_root_protocol": {
                    "integration_outboxes": {
                        "telegram": {"name": "telegram", "size": 1, "max_size": 10, "durable_store": True},
                        "llm": {"name": "llm", "size": 0, "max_size": 5, "durable_store": True},
                    }
                },
            },
        }
    ).to_dict()

    assert projection["id"] == "projection:hub:hub-1/reliability"
    assert projection["subject"]["health"]["root_control"] == "ready"
    assert projection["context"]["blocked_capabilities"] == ["root_routed_browser_proxy"]

    objects = {item["id"]: item for item in projection["objects"]}
    assert objects["root:eu"]["kind"] == "root"
    assert objects["quota:telegram-outbox"]["kind"] == "quota"
    assert objects["connection:hub:hub-1/root-control"]["kind"] == "connection"
    assert objects["runtime:hub:hub-1/yjs-sync"]["relations"]["workspace"] == ["workspace:desk"]
    assert objects["runtime:hub:hub-1/media-plane"]["resources"]["file_total"] == 2


def test_canonical_projection_from_reliability_snapshot_maps_actions_and_incidents() -> None:
    projection = canonical_projection_from_reliability_snapshot(
        {
            "node": {
                "node_id": "hub-2",
                "subnet_id": "main",
                "role": "hub",
                "ready": True,
                "node_state": "ready",
                "draining": False,
            },
            "runtime": {
                "readiness_tree": {
                    "root_control": {"status": "degraded", "summary": "root control is unstable"},
                    "route": {"status": "down", "summary": "route is unavailable"},
                },
                "degraded_matrix": {"root_routed_browser_proxy": {"allowed": False}},
                "channel_diagnostics": {
                    "root_control": {"stability": {"state": "flapping", "score": 62}},
                    "route": {"stability": {"state": "down", "score": 15}},
                },
                "hub_root_zone": {},
                "sidecar_runtime": {
                    "enabled": True,
                    "status": "degraded",
                    "summary": "sidecar diagnostics are stale",
                    "remote_session_state": "stale",
                    "control_ready": "degraded",
                },
                "sync_runtime": {
                    "available": True,
                    "selected_webspace_id": "default",
                    "assessment": {"state": "pressure", "reason": "bounded replay window near limit"},
                    "transport": {"server_ready": False},
                    "action_overrides": {"restore": {"enabled": False, "reason": "snapshot missing"}},
                },
                "media_runtime": {
                    "available": False,
                    "assessment": {"state": "unavailable", "reason": "media runtime module is unavailable"},
                    "transport": {"direct_local_ready": False},
                },
                "hub_root_protocol": {
                    "integration_outboxes": {
                        "telegram": {"name": "telegram", "size": 5, "durable_store": False, "publish_fail": 1, "publish_ok": 0}
                    }
                },
            },
        }
    ).to_dict()

    objects = {item["id"]: item for item in projection["objects"]}
    sync_actions = {item["id"]: item for item in objects["runtime:hub:hub-2/yjs-sync"]["actions"]}

    assert sync_actions["restore"]["risk"] == "medium"
    assert objects["connection:hub:hub-2/route"]["status"] == "offline"
    assert objects["quota:telegram-outbox"]["status"] in {"warning", "degraded"}
    assert any(item["object_id"] == "connection:hub:hub-2/route" for item in projection["incidents"])
    assert any(item["object_id"] == "runtime:hub:hub-2/sidecar" for item in projection["incidents"])


def test_canonical_inventory_projection_counts_objects_and_incidents() -> None:
    subject = CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status=CanonicalStatus.ONLINE)
    objects = [
        CanonicalObject(id="workspace:desk", kind="workspace", title="Desk", status=CanonicalStatus.ONLINE),
        CanonicalObject(id="browser:b1", kind="browser_session", title="Browser 1", status=CanonicalStatus.WARNING),
        CanonicalObject(id="io:node:say", kind="io_endpoint", title="say", status=CanonicalStatus.OFFLINE),
    ]

    projection = canonical_inventory_projection(subject, objects).to_dict()

    assert projection["id"] == "projection:hub:alpha/inventory"
    assert projection["context"]["kind_totals"]["browser_session"] == 1
    assert projection["context"]["incident_total"] == 2
    assert len(projection["incidents"]) == 2
