from __future__ import annotations

from pathlib import Path

import json
import pytest

from adaos.services.browser.flows import BrowserIODeviceFlow, OwnerBrowserFlow
from adaos.services.root import ClientContext, RootAuthorityBackend, Scope


def _write_config(path: Path) -> Path:
    config = {
        "hmac_audit_key": "browser",
        "context_hmac_key": "browser-context",
        "idempotency_ttl": 600,
        "rate_limits": {"device": 100, "qr": 100, "auth": 100},
        "lifetimes": {
            "access_token_seconds": 120,
            "refresh_token_seconds": 300,
            "channel_token_seconds": 60,
            "device_code_seconds": 120,
            "qr_session_seconds": 120,
            "challenge_seconds": 60,
            "consent_ttl_seconds": 600,
        },
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.fixture()
def backend(tmp_path: Path) -> RootAuthorityBackend:
    db_path = tmp_path / "root.sqlite"
    config_path = _write_config(tmp_path / "config.json")
    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    yield backend
    backend.close()


def test_owner_and_browser_flows_interop(backend: RootAuthorityBackend) -> None:
    subnet_id = "subnet-browser"
    owner_device_id = "owner-main"
    owner_context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.60", user_agent="pytest-owner")
    owner_flow = OwnerBrowserFlow(
        backend=backend,
        subnet_id=subnet_id,
        owner_device_id=owner_device_id,
        context=owner_context,
    )
    start = owner_flow.start([Scope.MANAGE_IO_DEVICES], idempotency_key="owner-start")
    assert start["device_code"]
    approve = owner_flow.approve(
        granted_scopes=[Scope.MANAGE_IO_DEVICES, Scope.EMIT_EVENT],
        aliases=["Owner"],
        capabilities=["ui"],
        idempotency_key="owner-approve",
    )
    assert approve["device_id"]
    tokens = owner_flow.poll_tokens(idempotency_key="owner-token")
    assert tokens["access_token"]
    owner_flow.auth_session(audience=f"subnet:{subnet_id}", idempotency_key="owner-challenge")
    owner_flow.refresh()

    browser_context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.61", user_agent="pytest-browser")
    browser_flow = BrowserIODeviceFlow(backend=backend, subnet_id=subnet_id, context=browser_context)
    browser_flow.begin([Scope.EMIT_EVENT], idempotency_key="qr-start")
    approval = browser_flow.approve(owner_device_id=owner_device_id, scopes=[Scope.EMIT_EVENT], idempotency_key="qr-approve")
    assert approval["device_id"]
    browser_flow.complete_auth(idempotency_key="browser-auth", audience=f"subnet:{subnet_id}")
