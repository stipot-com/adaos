from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from adaos.services.system_model import (
    CanonicalStatus,
    canonical_object_from_node_status,
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
