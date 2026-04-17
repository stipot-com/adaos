from __future__ import annotations

import y_py as Y

from adaos.services.scenario import webspace_runtime as webspace_runtime_module


def _collect_container_ids(value, out: set[int]) -> None:
    if isinstance(value, dict):
        out.add(id(value))
        for item in value.values():
            _collect_container_ids(item, out)
        return
    if isinstance(value, list):
        out.add(id(value))
        for item in value:
            _collect_container_ids(item, out)
        return
    if isinstance(value, tuple):
        out.add(id(value))
        for item in value:
            _collect_container_ids(item, out)


def test_set_map_value_if_changed_promotes_dict_branch_to_attached_y_map() -> None:
    ydoc = Y.YDoc()
    with ydoc.begin_transaction() as txn:
        data_map = ydoc.get_map("data")
        data_map.set(
            txn,
            "desktop",
            {
                "pageSchema": {"id": "old-page", "widgets": [{"id": "w1"}]},
                "topbar": [{"id": "t1"}],
            },
        )

    with ydoc.begin_transaction() as txn:
        changed, mode = webspace_runtime_module._set_map_value_if_changed(
            ydoc.get_map("data"),
            txn,
            "desktop",
            {
                "pageSchema": {"id": "new-page", "widgets": [{"id": "w1"}]},
                "topbar": [{"id": "t1"}],
            },
        )

    assert changed is True
    assert mode == "diff"

    desktop = ydoc.get_map("data").get("desktop")
    assert isinstance(desktop, Y.YMap)
    assert desktop.get("topbar") == [{"id": "t1"}]

    page_schema = desktop.get("pageSchema")
    assert isinstance(page_schema, Y.YMap)
    assert page_schema.get("id") == "new-page"
    assert page_schema.get("widgets") == [{"id": "w1"}]

    with ydoc.begin_transaction() as txn:
        changed, mode = webspace_runtime_module._set_map_value_if_changed(
            ydoc.get_map("data"),
            txn,
            "desktop",
            {
                "pageSchema": {"id": "new-page", "widgets": [{"id": "w1"}]},
                "topbar": [{"id": "t1"}],
            },
        )

    assert changed is False
    assert mode == "diff"


def test_set_map_value_if_changed_diff_deletes_missing_nested_keys() -> None:
    ydoc = Y.YDoc()
    with ydoc.begin_transaction() as txn:
        data_map = ydoc.get_map("data")
        changed, mode = webspace_runtime_module._set_map_value_if_changed(
            data_map,
            txn,
            "routing",
            {
                "current": {"path": "/home"},
                "history": ["/home"],
            },
        )
    assert changed is True
    assert mode == "diff"

    with ydoc.begin_transaction() as txn:
        changed, mode = webspace_runtime_module._set_map_value_if_changed(
            ydoc.get_map("data"),
            txn,
            "routing",
            {
                "current": {"path": "/settings"},
            },
        )

    assert changed is True
    assert mode == "diff"

    routing = ydoc.get_map("data").get("routing")
    assert isinstance(routing, Y.YMap)
    assert routing.get("history") is None
    current = routing.get("current")
    assert isinstance(current, Y.YMap)
    assert current.get("path") == "/settings"


def test_set_map_value_if_changed_unchanged_diff_avoids_cloning_current_containers(monkeypatch) -> None:
    ydoc = Y.YDoc()
    payload = {
        "pageSchema": {
            "id": "desktop",
            "widgets": [
                {"id": "weather"},
                {"id": "infrascope"},
            ],
        },
        "topbar": [
            {"id": "home"},
            {"id": "settings"},
        ],
    }

    with ydoc.begin_transaction() as txn:
        changed, mode = webspace_runtime_module._set_map_value_if_changed(
            ydoc.get_map("data"),
            txn,
            "desktop",
            payload,
        )

    assert changed is True
    assert mode == "diff"

    payload_container_ids: set[int] = set()
    _collect_container_ids(payload, payload_container_ids)
    original_clone = webspace_runtime_module._clone_json_like

    def _guarded_clone(value):
        if isinstance(value, (dict, list, tuple)) and id(value) not in payload_container_ids:
            raise AssertionError(f"unexpected clone of current container: {type(value).__name__}")
        return original_clone(value)

    monkeypatch.setattr(webspace_runtime_module, "_clone_json_like", _guarded_clone)

    with ydoc.begin_transaction() as txn:
        changed, mode = webspace_runtime_module._set_map_value_if_changed(
            ydoc.get_map("data"),
            txn,
            "desktop",
            payload,
        )

    assert changed is False
    assert mode == "diff"
