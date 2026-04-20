from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from uuid import uuid4


if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg


def _load_subnet_env_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "subnet_env" / "handlers" / "main.py"
    module_name = f"test_subnet_env_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _patch_snapshot_dependencies(monkeypatch, mod, dotenv_path: Path) -> None:
    monkeypatch.setattr(mod, "_dotenv_path", lambda: dotenv_path)
    monkeypatch.setattr(mod, "_ensure_skill_data_projections", lambda: None)
    monkeypatch.setattr(mod, "_project_snapshot", lambda snapshot, webspace_id=None: None)
    monkeypatch.setattr(
        mod,
        "_node_payload",
        lambda: {
            "node_id": "node-1",
            "subnet_id": "subnet-1",
            "role": "hub",
            "zone_id": "ru-msk",
            "node_names": ["Hub"],
            "primary_node_name": "Hub",
            "runtime": {"route_mode": "local", "connected_to_hub": True},
        },
    )
    monkeypatch.setattr(
        mod,
        "get_reliability_projection",
        lambda: {
            "subject": {
                "status": "online",
                "health": {"connectivity": "ok", "runtime_freshness": "fresh"},
            }
        },
    )


def test_subnet_env_snapshot_includes_zone_drift_and_effective_views(tmp_path, monkeypatch):
    mod = _load_subnet_env_module()
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "ENV_TYPE=prod",
                "GIT_USER=alice",
                "GIT_EMAIL=alice@example.com",
                "ADAOS_SUBNET_YJS_REPLICATION=1",
                "ADAOS_LOG_LEVEL=DEBUG",
                "ADAOS_ZONE_ID=ru-msk",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _patch_snapshot_dependencies(monkeypatch, mod, dotenv_path)
    monkeypatch.setenv("ENV_TYPE", "dev")

    snapshot = mod.get_snapshot(webspace_id="ws-1")

    assert snapshot["summary"]["value"] == "dev"
    assert "zone=ru-msk" in snapshot["summary"]["label"]
    assert snapshot["state"]["drift_count"] == 1
    assert any(item["title"] == "ADAOS_ZONE_ID" for item in snapshot["effective_env"])
    assert any(item["title"] == "ENV_TYPE" and "process env" in item["subtitle"] for item in snapshot["effective_env"])
    assert snapshot["notices"][0]["id"] == "notice:drift"


def test_subnet_env_set_env_value_validates_and_clears_git_email(tmp_path, monkeypatch):
    mod = _load_subnet_env_module()
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("GIT_EMAIL=old@example.com\n", encoding="utf-8")
    _patch_snapshot_dependencies(monkeypatch, mod, dotenv_path)

    invalid = mod.set_env_value(key="GIT_EMAIL", value="broken-email")

    assert invalid["ok"] is False
    assert invalid["error"] == "invalid_value"
    assert "@" in invalid["message"]

    cleared = mod.set_env_value(key="GIT_EMAIL", value="")

    assert cleared["ok"] is True
    assert "GIT_EMAIL" not in dotenv_path.read_text(encoding="utf-8")


def test_subnet_env_apply_action_toggles_diagnostic_flag(tmp_path, monkeypatch):
    mod = _load_subnet_env_module()
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("HUB_NATS_TRACE=0\n", encoding="utf-8")
    _patch_snapshot_dependencies(monkeypatch, mod, dotenv_path)
    monkeypatch.delenv("HUB_NATS_TRACE", raising=False)

    result = mod.apply_action("toggle::HUB_NATS_TRACE", webspace_id="ws-2")

    assert result["ok"] is True
    assert result["value"] == "1"
    assert "HUB_NATS_TRACE=1" in dotenv_path.read_text(encoding="utf-8")
    assert result["env"]["effective"]["HUB_NATS_TRACE"] == "1"
