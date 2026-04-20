from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import types
import uuid

import pytest

sys.modules.setdefault("nats", types.SimpleNamespace())
fake_y_py = types.SimpleNamespace(
    YDoc=type("YDoc", (), {}),
    apply_update=lambda *args, **kwargs: None,
)
sys.modules.setdefault("y_py", fake_y_py)
fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
fake_ystore_module.BaseYStore = object
fake_ystore_module.YDocNotFound = RuntimeError
fake_ypy_websocket = types.ModuleType("ypy_websocket")
fake_ypy_websocket.ystore = fake_ystore_module
sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)

from adaos.services.node_config import NodeConfig, RootSettings
from adaos.services.root.client import RootHttpError
from adaos.services.root.service import RootDeveloperService, RootServiceError


class _DummyBus:
    def publish(self, event) -> None:
        return None


class _DummyPaths:
    def __init__(self, base: Path) -> None:
        self._base = base

    def base_dir(self) -> Path:
        return self._base


def _workspace_tmp_dir() -> Path:
    path = Path("artifacts") / "test_tmp" / f"root-service-zone-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _install_dummy_ctx(monkeypatch: pytest.MonkeyPatch, base_dir: Path) -> None:
    ctx = SimpleNamespace(bus=_DummyBus(), paths=_DummyPaths(base_dir))
    monkeypatch.setattr("adaos.services.root.service.get_ctx", lambda: ctx)
    monkeypatch.setattr("adaos.services.node_config.get_ctx", lambda: ctx)


def test_root_service_client_uses_stored_effective_root_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dummy_ctx(monkeypatch, _workspace_tmp_dir())
    monkeypatch.setenv("ADAOS_ZONE_ID", "ru")
    cfg = NodeConfig(
        node_id="node-1",
        subnet_id="subnet-1",
        role="hub",
        root_settings=RootSettings(base_url="https://ru.api.inimatic.com"),
    )

    service = RootDeveloperService(config_loader=lambda: cfg, config_saver=lambda _cfg: None)

    assert service._client(cfg).base_url == "https://ru.api.inimatic.com"


def test_root_service_client_keeps_explicit_non_default_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dummy_ctx(monkeypatch, _workspace_tmp_dir())
    monkeypatch.setenv("ADAOS_ZONE_ID", "ru")
    cfg = NodeConfig(
        node_id="node-1",
        subnet_id="subnet-1",
        role="hub",
        root_settings=RootSettings(base_url="https://custom-root.example"),
    )

    service = RootDeveloperService(config_loader=lambda: cfg, config_saver=lambda _cfg: None)

    assert service._client(cfg).base_url == "https://custom-root.example"


def test_root_init_reports_zone_aware_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dummy_ctx(monkeypatch, _workspace_tmp_dir())
    monkeypatch.setenv("ADAOS_ZONE_ID", "ru")
    cfg = NodeConfig(
        node_id="node-1",
        subnet_id="subnet-1",
        role="hub",
        root_settings=RootSettings(base_url="https://ru.api.inimatic.com"),
    )
    service = RootDeveloperService(config_loader=lambda: cfg, config_saver=lambda _cfg: None)
    monkeypatch.setattr(
        service,
        "_register_hub",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RootHttpError(
                "POST /v1/bootstrap_token failed: _ssl.c:999: The handshake operation timed out",
                status_code=0,
            )
        ),
    )

    with pytest.raises(RootServiceError) as exc_info:
        service.init(root_token="dev-root-token")

    message = str(exc_info.value)
    assert "https://ru.api.inimatic.com" in message
    assert "ADAOS_ZONE_ID=ru" in message
