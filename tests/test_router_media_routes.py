from __future__ import annotations

import sys
from types import SimpleNamespace

from adaos.services.router.media_routes import resolve_media_route_intent


def test_resolve_media_route_intent_prefers_member_browser_direct_when_admitted() -> None:
    route = resolve_media_route_intent(
        need="live_stream",
        direct_local_ready=False,
        root_routed_ready=True,
        hub_webrtc_ready=True,
        producer_preference="member",
        preferred_member_id="member-1",
        candidate_member_ids=["member-1"],
        member_browser_direct_possible=True,
        member_browser_direct_admitted=True,
        candidate_member_total=1,
        browser_session_total=1,
    )

    assert route["active_route"] == "member_browser_direct"
    assert route["delivery_topology"] == "member_browser_direct"
    assert route["producer_authority"] == "member"
    assert route["preferred_member_id"] == "member-1"
    assert route["producer_target"]["member_id"] == "member-1"
    assert route["member_browser_direct"]["candidate_members"] == ["member-1"]
    assert route["member_browser_direct"]["ready"] is True


def test_resolve_media_route_intent_uses_first_candidate_member_when_preference_is_omitted() -> None:
    route = resolve_media_route_intent(
        need="live_stream",
        direct_local_ready=False,
        root_routed_ready=True,
        hub_webrtc_ready=False,
        producer_preference="member",
        candidate_member_ids=["member-2", "member-3"],
        member_browser_direct_possible=True,
        member_browser_direct_admitted=True,
        candidate_member_total=2,
        browser_session_total=1,
    )

    assert route["active_route"] == "member_browser_direct"
    assert route["preferred_member_id"] == "member-2"
    assert route["producer_target"]["member_id"] == "member-2"


def test_resolve_media_route_intent_falls_back_to_hub_webrtc_when_member_route_not_admitted() -> None:
    route = resolve_media_route_intent(
        need="live_stream",
        direct_local_ready=False,
        root_routed_ready=True,
        hub_webrtc_ready=True,
        producer_preference="member",
        candidate_member_ids=["member-1"],
        member_browser_direct_possible=True,
        member_browser_direct_admitted=False,
        candidate_member_total=1,
        browser_session_total=1,
    )

    assert route["active_route"] == "hub_webrtc_loopback"
    assert route["producer_authority"] == "hub"
    assert route["member_browser_direct"]["possible"] is True
    assert route["member_browser_direct"]["admitted"] is False
    assert route["preferred_member_id"] == "member-1"
    assert route["member_browser_direct"]["candidate_members"] == ["member-1"]
    assert route["degradation_reason"] == "member_browser_direct_not_admitted"


def test_media_runtime_snapshot_exposes_member_browser_direct_foundation(monkeypatch, tmp_path) -> None:
    import adaos.services.media_library as media_library
    import adaos.services.media_capability as media_capability

    monkeypatch.setattr(media_library, "media_video_dir", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.webrtc.peer",
        SimpleNamespace(
            webrtc_peer_snapshot=lambda: {
                "peer_total": 1,
                "connected_peers": 1,
                "incoming_audio_tracks": 1,
                "incoming_video_tracks": 0,
                "loopback_audio_tracks": 1,
                "loopback_video_tracks": 0,
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        SimpleNamespace(
            active_browser_session_snapshot=lambda: {
                "peers": [
                    {
                        "device_id": "browser-1",
                        "connection_state": "connected",
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(
        media_capability,
        "_directory_nodes",
        lambda: [
            {
                "node_id": "member-2",
                "roles": ["member"],
                "online": True,
                "node_state": "ready",
                "capacity": {
                    "io": [
                        {
                            "io_type": "webrtc_media",
                            "capabilities": [
                                "webrtc:av",
                                "producer:member",
                                "topology:member_browser_direct",
                                "media:live_stream",
                                "state:available",
                                "mode:webrtc",
                            ],
                            "priority": 60,
                        }
                    ]
                },
            }
        ],
    )
    monkeypatch.setattr(media_capability, "_live_member_links", lambda: [])

    runtime = media_library.media_runtime_snapshot(items=[])

    assert runtime["route_intent"]["route_intent"] == "scenario_response_media"
    assert runtime["member_browser_direct"]["possible"] is True
    assert runtime["member_browser_direct"]["admitted"] is False
    assert runtime["member_browser_direct"]["preferred_member_id"] == "member-2"
    assert runtime["member_browser_direct"]["candidate_members"] == ["member-2"]
    assert runtime["preferred_member_id"] == "member-2"
    assert runtime["paths"]["member_browser_webrtc"]["ready"] is False
    assert runtime["route_profiles"]["live_stream"]["active_route"] == "hub_webrtc_loopback"


def test_media_runtime_snapshot_uses_live_member_snapshot_capacity_as_fallback(monkeypatch, tmp_path) -> None:
    import adaos.services.media_library as media_library
    import adaos.services.media_capability as media_capability

    monkeypatch.setattr(media_library, "media_video_dir", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.webrtc.peer",
        SimpleNamespace(
            webrtc_peer_snapshot=lambda: {
                "peer_total": 0,
                "connected_peers": 0,
                "incoming_audio_tracks": 0,
                "incoming_video_tracks": 0,
                "loopback_audio_tracks": 0,
                "loopback_video_tracks": 0,
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        SimpleNamespace(
            active_browser_session_snapshot=lambda: {
                "peers": [
                    {
                        "device_id": "browser-1",
                        "connection_state": "connected",
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(media_capability, "_directory_nodes", lambda: [])
    monkeypatch.setattr(
        media_capability,
        "_live_member_links",
        lambda: [
            {
                "node_id": "member-live",
                "connected": True,
                "roles": ["member"],
                "node_snapshot": {
                    "role": "member",
                    "node_state": "ready",
                    "capacity": {
                        "io": [
                            {
                                "io_type": "webrtc_media",
                                "capabilities": [
                                    "webrtc:av",
                                    "producer:member",
                                    "topology:member_browser_direct",
                                    "media:scenario_response_media",
                                    "state:available",
                                ],
                                "priority": 55,
                            }
                        ]
                    },
                },
            }
        ],
    )

    runtime = media_library.media_runtime_snapshot(items=[])

    assert runtime["member_browser_direct"]["candidate_member_total"] == 1
    assert runtime["member_browser_direct"]["preferred_member_id"] == "member-live"
