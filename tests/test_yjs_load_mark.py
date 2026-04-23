from __future__ import annotations

from adaos.services.yjs import load_mark as load_mark_module


def _reset_load_mark_state() -> None:
    load_mark_module._WEBSPACE_STATE.clear()
    load_mark_module._ACTIVE_STREAM_SUBSCRIPTIONS.clear()
    load_mark_module._LAST_STREAM_PUBLISH_AT.clear()
    load_mark_module._OWNER_ALERTS.clear()


def test_load_mark_tracks_owner_write_volume_and_rate() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=600,
        root_names=["data"],
        now_ts=10.0,
        source="sdk.web.yjs",
        owner="skill:weather_skill",
    )
    load_mark_module.record_write_update(
        "default",
        total_bytes=400,
        root_names=["data"],
        now_ts=10.2,
        source="sdk.web.yjs",
        owner="skill:weather_skill",
    )

    snapshot = load_mark_module.yjs_load_mark_snapshot(webspace_id="default", now_ts=11.0)
    selected = snapshot["selected_webspace"]
    root_item = selected["roots"]["data"]
    owner_item = selected["owners"]["_by_owner/skill_weather_skill"]

    assert root_item["recent_writes"] == 2
    assert root_item["write_total"] == 2
    assert root_item["avg_wps"] > 0.0
    assert root_item["peak_wps"] >= 1.0
    assert owner_item["recent_writes"] == 2
    assert owner_item["write_total"] == 2
    assert owner_item["recent_bytes"] == 1000
    assert selected["active_owner_total"] == 1


def test_load_mark_surfaces_unknown_owner_for_unattributed_flow() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=256,
        now_ts=20.0,
        source="mystery_writer",
    )

    snapshot = load_mark_module.yjs_load_mark_snapshot(webspace_id="default", now_ts=21.0)
    selected = snapshot["selected_webspace"]

    assert "_by_owner/unknown" in selected["owners"]
    assert selected["owners"]["_by_owner/unknown"]["recent_writes"] == 1
    assert selected["roots"]["_by_initiator/mystery_writer"]["recent_bytes"] == 256


def test_load_mark_stream_payload_includes_owner_and_root_rows() -> None:
    _reset_load_mark_state()

    load_mark_module.record_write_update(
        "default",
        total_bytes=512,
        root_names=["data"],
        now_ts=30.0,
        source="sdk.web.yjs",
        owner="skill:adaos_connect",
    )

    rows = load_mark_module._stream_payload_items_locked("default", now_ts=31.0)

    owner_rows = [row for row in rows if row.get("kind") == "owner"]
    root_rows = [row for row in rows if row.get("kind") == "root"]

    assert owner_rows
    assert root_rows
    assert owner_rows[0]["display"] == "_by_owner/skill_adaos_connect"
    assert root_rows[0]["display"] == "data"
