from __future__ import annotations

from pathlib import Path

import json
import pytest

from adaos.services.browser.flows import BrowserIODeviceFlow, OwnerBrowserFlow
from adaos.services.hub.agent import HubAgent
from adaos.services.root import ClientContext, DeviceRole, RootAuthorityBackend, Scope


def _write_config(path: Path) -> Path:
    config = {
        "hmac_audit_key": "integration",
        "context_hmac_key": "integration-context",
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


def _owner_flow(backend: RootAuthorityBackend, subnet_id: str, owner_device_id: str) -> OwnerBrowserFlow:
    context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.11", user_agent="pytest-owner")
    flow = OwnerBrowserFlow(
        backend=backend,
        subnet_id=subnet_id,
        owner_device_id=owner_device_id,
        context=context,
    )
    flow.start([Scope.MANAGE_IO_DEVICES], idempotency_key="owner-start")
    flow.approve(
        granted_scopes=[Scope.MANAGE_IO_DEVICES],
        aliases=["Owner"],
        capabilities=["admin"],
        idempotency_key="owner-confirm",
    )
    flow.poll_tokens(idempotency_key="owner-token")
    return flow


def test_hub_agent_bootstrap_and_channel(tmp_path: Path, backend: RootAuthorityBackend) -> None:
    subnet_id = "subnet-hub"
    owner_device_id = "owner-root"
    owner_flow = _owner_flow(backend, subnet_id, owner_device_id)

    qr_context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.51", user_agent="pytest-browser")
    browser_flow = BrowserIODeviceFlow(backend=backend, subnet_id=subnet_id, context=qr_context)
    browser_flow.begin([Scope.EMIT_EVENT], idempotency_key="qr-start")
    browser_flow.approve(owner_device_id=owner_device_id, scopes=[Scope.EMIT_EVENT], idempotency_key="qr-approve")
    browser_device_id = browser_flow.storage.get("device_id") or ""
    backend.update_device(
        actor_device_id=browser_device_id,
        device_id=browser_device_id,
        aliases=["display"],
        capabilities=None,
        idempotency_key="alias-self",
    )

    hub_dir = tmp_path / "hub"

    approvals: dict[str, int] = {}

    def approve_consent(consent_id: str) -> None:
        approvals[consent_id] = approvals.get(consent_id, 0) + 1
        backend.resolve_consent(
            owner_device_id=owner_device_id,
            consent_id=consent_id,
            approve=True,
            idempotency_key=f"consent-{consent_id}",
        )

    agent = HubAgent(backend=backend, storage_dir=hub_dir, subnet_id=subnet_id)
    bundle = agent.bootstrap(
        scopes=[Scope.MANAGE_IO_DEVICES, Scope.EMIT_EVENT],
        display_name="AdaOS Hub",
        approve=approve_consent,
        idempotency_key="hub-csr",
    )
    assert "certificate" in bundle
    assert approvals

    # Re-running bootstrap should use persisted certificate
    bundle_again = agent.bootstrap(
        scopes=[Scope.MANAGE_IO_DEVICES],
        display_name="AdaOS Hub",
        approve=approve_consent,
        idempotency_key="hub-csr-repeat",
    )
    assert bundle_again == bundle

    channel = agent.connect()
    assert channel.token
    backend.verify_hub_channel(node_id=channel.node_id, token=channel.token)

    rotated = agent.ensure_channel(margin_seconds=0)
    assert rotated.token != channel.token or rotated.expires_at >= channel.expires_at
    agent.verify_channel()

    directory = agent.refresh_directory(actor_device_id=browser_device_id, role=DeviceRole.BROWSER_IO)
    assert agent.resolve_alias("display") == browser_device_id
    assert directory

    denylist = agent.refresh_denylist()
    assert browser_device_id not in denylist

    # Missing scope for subscribe should raise
    with pytest.raises(PermissionError):
        agent.send_skill_payload(target_device_id=browser_device_id, required_scopes=[Scope.SUBSCRIBE_EVENT])

    # Revoke browser device and ensure denylist updated
    backend.revoke_device(
        actor_device_id=owner_device_id,
        device_id=browser_device_id,
        reason="compromised",
        idempotency_key="revoke-browser",
    )
    agent.refresh_denylist()
    with pytest.raises(PermissionError):
        agent.send_skill_payload(target_device_id=browser_device_id, required_scopes=[Scope.EMIT_EVENT])
