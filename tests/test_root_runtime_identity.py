from __future__ import annotations

from types import SimpleNamespace

from adaos.services.root import control_lifecycle_sync, core_update_sync


def test_control_lifecycle_report_surfaces_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-a-a-12345678")
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "candidate")
    monkeypatch.setattr(
        control_lifecycle_sync,
        "runtime_lifecycle_snapshot",
        lambda: {
            "node_state": "running",
            "reason": "booted",
            "draining": False,
            "accepting_new_work": True,
        },
    )
    monkeypatch.setattr(
        control_lifecycle_sync,
        "runtime_signal_snapshot",
        lambda: {
            "root_control": {"status": "up", "summary": "ready"},
            "route": {"status": "up", "summary": "ready"},
        },
    )
    monkeypatch.setattr(
        control_lifecycle_sync,
        "channel_diagnostics_snapshot",
        lambda: {
            "root_control": {"stability": {"state": "stable"}, "last_incident_class": ""},
            "route": {"stability": {"state": "stable"}, "last_incident_class": ""},
        },
    )
    monkeypatch.setattr(
        control_lifecycle_sync,
        "hub_root_transport_strategy_snapshot",
        lambda: {
            "requested_transport": "ws",
            "effective_transport": "ws",
            "selected_server": "wss://api.inimatic.com/nats",
            "last_event": "connected",
            "assessment": {"state": "healthy"},
        },
    )
    monkeypatch.setattr(
        control_lifecycle_sync,
        "active_slot_manifest",
        lambda: {"slot": "A", "git_commit": "abcdef1234567890", "target_rev": "rev2026"},
    )

    conf = SimpleNamespace(subnet_id="sn-test", node_id="node-1", role="hub")

    report = control_lifecycle_sync.build_control_lifecycle_report(conf)
    stream_id = control_lifecycle_sync._control_lifecycle_stream_id(conf)
    authority_epoch = control_lifecycle_sync._control_lifecycle_authority_epoch(conf)

    assert report["runtime_instance_id"] == "rt-a-a-12345678"
    assert report["transition_role"] == "candidate"
    assert report["runtime"]["runtime_instance_id"] == "rt-a-a-12345678"
    assert report["runtime"]["transition_role"] == "candidate"
    assert stream_id == "hub-control:lifecycle:sn-test:rt-a-a-12345678"
    assert "role:candidate" in authority_epoch
    assert "instance:rt-a-a-12345678" in authority_epoch


def test_core_update_report_surfaces_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-b-a-abcdef12")
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "active")
    monkeypatch.setattr(core_update_sync, "read_status", lambda: {"state": "idle"})
    monkeypatch.setattr(core_update_sync, "slot_status", lambda: {"active_slot": "B", "slots": {}})
    monkeypatch.setattr(
        core_update_sync,
        "active_slot_manifest",
        lambda: {"slot": "B", "git_commit": "fedcba9876543210", "target_rev": "rev2026"},
    )

    conf = SimpleNamespace(subnet_id="sn-test", node_id="node-1", role="hub")

    report = core_update_sync.build_core_update_report(conf)
    stream_id = core_update_sync._core_update_stream_id(conf)
    authority_epoch = core_update_sync._core_update_authority_epoch(conf)

    assert report["runtime_instance_id"] == "rt-b-a-abcdef12"
    assert report["transition_role"] == "active"
    assert report["runtime"]["runtime_instance_id"] == "rt-b-a-abcdef12"
    assert report["runtime"]["transition_role"] == "active"
    assert stream_id == "hub-integration:github-core-update:sn-test:rt-b-a-abcdef12"
    assert "role:active" in authority_epoch
    assert "instance:rt-b-a-abcdef12" in authority_epoch


def test_core_update_reconcile_skips_dev_api_serve(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_LAUNCH_MODE", "api_serve")
    monkeypatch.delenv("ADAOS_API_SERVE_ALLOW_CORE_UPDATE", raising=False)
    monkeypatch.setattr(
        core_update_sync,
        "_root_client",
        lambda _conf: (_ for _ in ()).throw(AssertionError("root client must not be used")),
    )

    conf = SimpleNamespace(subnet_id="sn-test", node_id="node-1", role="hub")

    result = core_update_sync.reconcile_hub_core_update(conf)

    assert result == {
        "ok": True,
        "skipped": True,
        "reason": "dev_api_serve_core_update_sync_disabled",
    }
