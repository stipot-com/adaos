from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


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


def _load_adaos_connect_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "adaos_connect" / "handlers" / "main.py"
    module_name = f"test_adaos_connect_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _fake_ctx(*, app_base: str = "https://app.inimatic.com", api_base: str = "https://api.inimatic.com"):
    return SimpleNamespace(
        settings=SimpleNamespace(
            app_base=app_base,
            api_base=api_base,
            subnet_id="sn_test",
            default_hub="sn_test",
        )
    )


def _fake_cfg(*, zone_id: str | None = "ru", base_url: str = "https://api.inimatic.com"):
    return SimpleNamespace(
        zone_id=zone_id,
        subnet_id="sn_test",
        root_state=None,
        root_settings=SimpleNamespace(base_url=base_url, ca_cert="keys/ca.cert"),
        subnet_settings=SimpleNamespace(hub=SimpleNamespace(cert="keys/hub_cert.pem", key="keys/hub_private.pem")),
    )


def test_browser_current_uses_zone_and_new_app_domain(monkeypatch, tmp_path: Path):
    mod = _load_adaos_connect_module()
    calls: list[dict[str, object]] = []
    expires_at = 1_800_000_000

    monkeypatch.setattr(mod, "get_ctx", lambda: _fake_ctx())
    monkeypatch.setattr(mod, "load_config", lambda ctx=None: _fake_cfg())
    monkeypatch.setattr(mod, "_expand_path", lambda value, fallback: tmp_path / str(value or fallback))

    class _FakeRootHttpClient:
        def __init__(self, *, base_url, verify=True, cert=None, **kwargs):
            self.base_url = base_url
            self.verify = verify
            self.cert = cert

        def request(self, method, path, *, json=None, headers=None, **kwargs):
            calls.append(
                {
                    "base_url": self.base_url,
                    "cert": self.cert,
                    "method": method,
                    "path": path,
                    "json": json,
                    "headers": headers,
                }
            )
            return {"pair_code": "PAIR1234", "zone_id": "ru", "expires_at": expires_at}

    monkeypatch.setattr(mod, "RootHttpClient", _FakeRootHttpClient)

    context = mod._resolve_context()
    current = mod._browser_current(context, request_id="ws-1:1")

    assert calls == [
        {
            "base_url": "https://ru.api.inimatic.com",
            "cert": None,
            "method": "POST",
            "path": "/v1/browser/pair/create",
            "json": {"ttl": 600, "zone_id": "ru"},
            "headers": None,
        }
    ]
    assert current["status"] == "ready"
    assert current["app_base_url"] == "https://myinimatic.web.app"
    assert current["link"] == "https://myinimatic.web.app/?pair_code=PAIR1234&zone=ru"
    assert current["qr_text"] == current["link"]
    assert current["expires_at_epoch"] == expires_at
    assert current["expires_at"] == mod._format_expiry_iso(expires_at)
    assert current["expires_at_display"] == mod._format_expiry_display(expires_at)
    assert "Valid until" in current["summary"]


def test_node_current_falls_back_to_root_token_and_embeds_zone_bootstrap_args(monkeypatch, tmp_path: Path):
    mod = _load_adaos_connect_module()
    calls: list[dict[str, object]] = []
    expires_at_utc = "2030-01-02T03:04:05Z"

    cert_path = tmp_path / "keys" / "hub_cert.pem"
    key_path = tmp_path / "keys" / "hub_private.pem"
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text("cert", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")

    def _expand(value, fallback):
        token = str(value or fallback)
        if token.endswith("hub_cert.pem"):
            return cert_path
        if token.endswith("hub_private.pem"):
            return key_path
        return tmp_path / token

    monkeypatch.setattr(mod, "get_ctx", lambda: _fake_ctx())
    monkeypatch.setattr(mod, "load_config", lambda ctx=None: _fake_cfg())
    monkeypatch.setattr(mod, "_expand_path", _expand)
    monkeypatch.setenv("ROOT_TOKEN", "root-secret")

    class _FakeRootHttpClient:
        def __init__(self, *, base_url, verify=True, cert=None, **kwargs):
            self.base_url = base_url
            self.verify = verify
            self.cert = cert

        def request(self, method, path, *, json=None, headers=None, **kwargs):
            calls.append(
                {
                    "base_url": self.base_url,
                    "cert": self.cert,
                    "method": method,
                    "path": path,
                    "json": json,
                    "headers": headers,
                }
            )
            if self.cert is not None:
                raise mod.RootHttpError(
                    "unauthorized",
                    status_code=401,
                    error_code="unauthorized",
                    payload={"ok": False, "error": "unauthorized"},
                )
            assert headers == {"X-Root-Token": "root-secret"}
            return {"code": "ABCD-EFGH", "expires_at_utc": expires_at_utc}

    monkeypatch.setattr(mod, "RootHttpClient", _FakeRootHttpClient)

    context = mod._resolve_context()
    current = mod._node_current(context, request_id="ws-1:2")

    assert len(calls) == 2
    assert calls[0]["base_url"] == "https://ru.api.inimatic.com"
    assert calls[0]["path"] == "/v1/subnets/join-code"
    assert calls[0]["cert"] == (str(cert_path), str(key_path))
    assert calls[1]["headers"] == {"X-Root-Token": "root-secret"}
    assert current["status"] == "ready"
    assert current["code"] == "ABCD-EFGH"
    assert "myinimatic.web.app/assets/linux/init.sh" in current["linux_command"]
    assert "--zone ru" in current["linux_command"]
    assert "https://ru.api.inimatic.com" in current["linux_command"]
    assert "myinimatic.web.app/assets/windows/init.ps1" in current["windows_ps_command"]
    assert "-ZoneId 'ru'" in current["windows_ps_command"]
    assert "myinimatic.web.app/assets/windows/init.ps1" in current["windows_cmd_command"]
    assert "init.bat" not in current["windows_cmd_command"]
    assert '-RootUrl "https://ru.api.inimatic.com"' in current["windows_cmd_command"]
    assert '-ZoneId "ru"' in current["windows_cmd_command"]
    assert current["expires_at"] == expires_at_utc
    assert current["expires_at_display"] == "2030-01-02 03:04:05 UTC"
    assert "Valid until 2030-01-02 03:04:05 UTC" in current["summary"]


@pytest.mark.asyncio
async def test_on_prepare_writes_pending_before_background_result(monkeypatch):
    mod = _load_adaos_connect_module()
    writes: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        mod,
        "_resolve_context",
        lambda: {
            "cfg": SimpleNamespace(),
            "hub_id": "sn_test",
            "zone_id": "ru",
            "verify": True,
            "cert_tuple": None,
            "root_base_url": "https://ru.api.inimatic.com",
            "app_base_url": "https://myinimatic.web.app",
        },
    )

    async def _write_current(webspace_id: str, current: dict[str, object]) -> None:
        writes.append((webspace_id, copy.deepcopy(current)))

    def _prepare_current(mode: str, context: dict[str, object], *, request_id: str) -> dict[str, object]:
        time.sleep(0.05)
        current = mod._decorate_current(mod._base_current(mode), context, request_id=request_id, status="ready", busy=False)
        current["summary"] = "ready"
        current["code"] = "DONE"
        return current

    monkeypatch.setattr(mod, "_write_current", _write_current)
    monkeypatch.setattr(mod, "_prepare_current", _prepare_current)

    await mod.on_prepare({"mode": "node", "webspace_id": "ws-42"})

    assert writes
    first_ws, first_payload = writes[0]
    assert first_ws == "ws-42"
    assert first_payload["status"] == "pending"
    assert first_payload["busy"] is True
    assert "take a few seconds" in str(first_payload["summary"])

    await asyncio.sleep(0.15)

    assert len(writes) >= 2
    last_ws, last_payload = writes[-1]
    assert last_ws == "ws-42"
    assert last_payload["status"] == "ready"
    assert last_payload["busy"] is False
    assert last_payload["code"] == "DONE"


@pytest.mark.asyncio
async def test_on_prepare_reuses_cached_result_while_code_is_alive(monkeypatch):
    mod = _load_adaos_connect_module()
    writes: list[tuple[str, dict[str, object]]] = []
    prepare_calls = {"count": 0}
    now = {"value": 1_800_000_000.0}

    monkeypatch.setattr(
        mod,
        "_resolve_context",
        lambda: {
            "cfg": SimpleNamespace(),
            "hub_id": "sn_test",
            "zone_id": "ru",
            "verify": True,
            "cert_tuple": None,
            "root_base_url": "https://ru.api.inimatic.com",
            "app_base_url": "https://myinimatic.web.app",
        },
    )
    monkeypatch.setattr(mod.time, "time", lambda: now["value"])

    async def _write_current(webspace_id: str, current: dict[str, object]) -> None:
        writes.append((webspace_id, copy.deepcopy(current)))

    def _prepare_current(mode: str, context: dict[str, object], *, request_id: str) -> dict[str, object]:
        prepare_calls["count"] += 1
        current = mod._decorate_current(mod._base_current(mode), context, request_id=request_id, status="ready", busy=False)
        current["summary"] = "cached"
        current["code"] = "CACHE-1"
        mod._apply_expiry_fields(current, expires_at=now["value"] + 600)
        return current

    monkeypatch.setattr(mod, "_write_current", _write_current)
    monkeypatch.setattr(mod, "_prepare_current", _prepare_current)

    await mod.on_prepare({"mode": "node", "webspace_id": "ws-cache"})
    await asyncio.sleep(0.05)

    assert prepare_calls["count"] == 1
    assert writes[-1][1]["code"] == "CACHE-1"

    writes.clear()

    await mod.on_prepare({"mode": "node", "webspace_id": "ws-cache"})
    await asyncio.sleep(0.05)

    assert prepare_calls["count"] == 1
    assert len(writes) == 1
    assert writes[0][1]["status"] == "ready"
    assert writes[0][1]["busy"] is False
    assert writes[0][1]["code"] == "CACHE-1"


@pytest.mark.asyncio
async def test_on_prepare_renews_code_after_cache_expires(monkeypatch):
    mod = _load_adaos_connect_module()
    writes: list[tuple[str, dict[str, object]]] = []
    prepare_calls = {"count": 0}
    now = {"value": 1_800_000_000.0}

    monkeypatch.setattr(
        mod,
        "_resolve_context",
        lambda: {
            "cfg": SimpleNamespace(),
            "hub_id": "sn_test",
            "zone_id": "ru",
            "verify": True,
            "cert_tuple": None,
            "root_base_url": "https://ru.api.inimatic.com",
            "app_base_url": "https://myinimatic.web.app",
        },
    )
    monkeypatch.setattr(mod.time, "time", lambda: now["value"])

    async def _write_current(webspace_id: str, current: dict[str, object]) -> None:
        writes.append((webspace_id, copy.deepcopy(current)))

    def _prepare_current(mode: str, context: dict[str, object], *, request_id: str) -> dict[str, object]:
        prepare_calls["count"] += 1
        code = f"CACHE-{prepare_calls['count']}"
        current = mod._decorate_current(mod._base_current(mode), context, request_id=request_id, status="ready", busy=False)
        current["summary"] = code
        current["code"] = code
        mod._apply_expiry_fields(current, expires_at=now["value"] + 5)
        return current

    monkeypatch.setattr(mod, "_write_current", _write_current)
    monkeypatch.setattr(mod, "_prepare_current", _prepare_current)

    await mod.on_prepare({"mode": "telegram", "webspace_id": "ws-expire"})
    await asyncio.sleep(0.05)

    assert prepare_calls["count"] == 1
    assert writes[-1][1]["code"] == "CACHE-1"

    now["value"] += 6
    writes.clear()

    await mod.on_prepare({"mode": "telegram", "webspace_id": "ws-expire"})
    await asyncio.sleep(0.05)

    assert prepare_calls["count"] == 2
    assert writes[0][1]["status"] == "pending"
    assert writes[-1][1]["status"] == "ready"
    assert writes[-1][1]["code"] == "CACHE-2"
