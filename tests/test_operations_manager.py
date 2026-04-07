from __future__ import annotations

import pytest

pytest.importorskip("y_py")

from adaos.services.operations import get_operation_manager
from adaos.services.yjs.doc import get_ydoc


def test_operation_manager_projects_active_operations_to_yjs() -> None:
    manager = get_operation_manager()
    operation = manager.create_operation(
        kind="skill.install",
        target_kind="skill",
        target_id="demo_skill",
        webspace_id="default",
        scope=["global", "skill.install", "skill:demo_skill"],
        message="Accepted skill install",
    )

    manager.update_operation(
        operation["operation_id"] if isinstance(operation, dict) else operation.operation_id,
        status="running",
        progress=25,
        message="Installing",
        current_step="skill.install",
    )

    snapshot = manager.snapshot(webspace_id="default")
    assert snapshot["active"]
    current = next(item for item in snapshot["active_items"] if item["target_id"] == "demo_skill")
    assert current["target_id"] == "demo_skill"
    assert current["status"] == "running"

    with get_ydoc("default") as ydoc:
        runtime_map = ydoc.get_map("runtime")
        operations = runtime_map.get("operations")
        assert isinstance(operations, dict)
        assert current["operation_id"] in (operations.get("by_id") or {})


def test_operation_manager_records_notifications_on_completion() -> None:
    manager = get_operation_manager()
    operation = manager.create_operation(
        kind="scenario.install",
        target_kind="scenario",
        target_id="welcome",
        webspace_id="default",
        scope=["global", "scenario.install", "scenario:welcome"],
    )
    op_id = operation["operation_id"] if isinstance(operation, dict) else operation.operation_id
    manager.update_operation(
        op_id,
        status="succeeded",
        progress=100,
        message="Installed scenario welcome",
        result={"target_id": "welcome"},
        finished=True,
    )

    snapshot = manager.snapshot(webspace_id="default")
    assert snapshot["notifications"]
    assert snapshot["notifications"][-1]["operation_id"] == op_id
