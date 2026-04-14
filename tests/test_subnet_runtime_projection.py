from __future__ import annotations

from adaos.services.registry.subnet_runtime_projection import (
    subnet_runtime_projection_freshness,
    subnet_runtime_projection_policy,
)


def test_subnet_runtime_projection_policy_uses_default_thresholds() -> None:
    policy = subnet_runtime_projection_policy()

    assert policy == {
        "aging_after_s": 30.0,
        "stale_after_s": 90.0,
    }


def test_subnet_runtime_projection_freshness_tracks_pending_aging_and_stale_states() -> None:
    pending = subnet_runtime_projection_freshness({}, online=True, now=100.0)
    fresh = subnet_runtime_projection_freshness({"captured_at": 80.0}, online=True, now=100.0)
    aging = subnet_runtime_projection_freshness({"captured_at": 69.0}, online=True, now=100.0)
    stale = subnet_runtime_projection_freshness({"captured_at": 9.0}, online=True, now=100.0)
    offline = subnet_runtime_projection_freshness({"captured_at": 99.0}, online=False, now=100.0)

    assert pending["state"] == "pending"
    assert pending["reason"] == "snapshot_missing"
    assert fresh["state"] == "fresh"
    assert fresh["age_s"] == 20.0
    assert aging["state"] == "aging"
    assert stale["state"] == "stale"
    assert offline["state"] == "stale"
    assert offline["reason"] == "node_offline"
