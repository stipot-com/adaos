from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from adaos.services.system_model import (
    CanonicalStatus,
    canonical_object_from_node_status,
    canonical_projection_from_reliability_snapshot,
    canonical_object_from_skill_status,
    canonical_object_from_user_profile,
    canonical_object_from_workspace_manifest,
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


def test_normalize_connectivity_status_handles_bool_and_transport_tokens() -> None:
    assert normalize_connectivity_status(True).value == "reachable"
    assert normalize_connectivity_status(False).value == "unreachable"
    assert normalize_connectivity_status("ws").value == "reachable"
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


def test_canonical_object_from_user_profile_uses_preferred_name() -> None:
    obj = canonical_object_from_user_profile(
        SimpleNamespace(user_id="u-1", settings={"preferred_name": "Ada", "locale": "ru-RU"})
    ).to_dict()

    assert obj["id"] == "profile:u-1"
    assert obj["title"] == "Ada"
    assert obj["actual_state"]["settings"]["locale"] == "ru-RU"


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
            },
        }
    ).to_dict()

    assert projection["id"] == "projection:hub:hub-1/reliability"
    assert projection["subject"]["health"]["root_control"] == "ready"
    assert projection["context"]["blocked_capabilities"] == ["root_routed_browser_proxy"]

    objects = {item["id"]: item for item in projection["objects"]}
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
            },
        }
    ).to_dict()

    objects = {item["id"]: item for item in projection["objects"]}
    sync_actions = {item["id"]: item for item in objects["runtime:hub:hub-2/yjs-sync"]["actions"]}

    assert sync_actions["restore"]["risk"] == "medium"
    assert objects["connection:hub:hub-2/route"]["status"] == "offline"
    assert any(item["object_id"] == "connection:hub:hub-2/route" for item in projection["incidents"])
    assert any(item["object_id"] == "runtime:hub:hub-2/sidecar" for item in projection["incidents"])
