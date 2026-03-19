from __future__ import annotations

from adaos.services.runtime_lifecycle import (
    is_accepting_new_work,
    request_drain,
    reset_runtime_lifecycle,
    runtime_lifecycle_snapshot,
)


def test_runtime_lifecycle_defaults_ready() -> None:
    reset_runtime_lifecycle()
    snapshot = runtime_lifecycle_snapshot()
    assert snapshot["node_state"] == "ready"
    assert snapshot["draining"] is False
    assert is_accepting_new_work() is True


def test_runtime_lifecycle_switches_to_draining() -> None:
    reset_runtime_lifecycle()
    request_drain(reason="test")
    snapshot = runtime_lifecycle_snapshot()
    assert snapshot["node_state"] == "draining"
    assert snapshot["reason"] == "test"
    assert snapshot["accepting_new_work"] is False
