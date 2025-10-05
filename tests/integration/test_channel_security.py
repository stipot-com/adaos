from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from adaos.services.browser.flows import OwnerBrowserFlow, sign_assertion
from adaos.services.hub.agent import HubAgent
from adaos.services.root import ClientContext, DeviceRole, RootAuthorityBackend, RootBackendError, Scope


def _write_config(path: Path) -> Path:
    config = {
        "hmac_audit_key": "channel-tests",
        "context_hmac_key": "channel-context",
        "idempotency_ttl": 600,
        "rate_limits": {"device": 100, "qr": 100, "auth": 100},
        "lifetimes": {
            "access_token_seconds": 120,
            "refresh_token_seconds": 600,
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
def backend(tmp_path: Path) -> tuple[RootAuthorityBackend, Path]:
    config_path = _write_config(tmp_path / "config.json")
    db_path = tmp_path / "root.sqlite"
    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    yield backend, tmp_path
    backend.close()


def _bootstrap_hub(backend: RootAuthorityBackend, subnet_id: str, storage_root: Path) -> HubAgent:
    storage_dir = storage_root / "hub-agent"
    agent = HubAgent(backend=backend, storage_dir=storage_dir, subnet_id=subnet_id)

    def approve(consent_id: str) -> None:
        backend.resolve_consent(
            owner_device_id="owner-controller",
            consent_id=consent_id,
            approve=True,
            idempotency_key=f"hub-consent-{consent_id}",
        )

    agent.bootstrap(
        scopes=[Scope.MANAGE_IO_DEVICES],
        display_name="Test Hub",
        approve=approve,
        idempotency_key="hub-bootstrap",
    )
    agent.connect()
    return agent


def test_channel_authorization_rotation_and_binding(backend: tuple[RootAuthorityBackend, Path]) -> None:
    backend_service, tmp_path = backend
    subnet_id = "subnet-channel"
    context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.77", user_agent="pytest-owner")
    owner_flow = OwnerBrowserFlow(
        backend=backend_service,
        subnet_id=subnet_id,
        owner_device_id="owner-controller",
        context=context,
    )
    owner_flow.start([Scope.MANAGE_IO_DEVICES], idempotency_key="owner-start")
    owner_flow.approve(
        granted_scopes=[Scope.MANAGE_IO_DEVICES],
        aliases=["Owner"],
        capabilities=["admin"],
        idempotency_key="owner-confirm",
    )
    tokens = owner_flow.poll_tokens(idempotency_key="owner-token")
    device_id = tokens["device_id"]
    channel_token = tokens["channel_token"]

    hub_agent = _bootstrap_hub(backend_service, subnet_id, tmp_path)
    hub_id = hub_agent.ensure_channel().node_id

    payload = {
        "nonce": channel_token,
        "aud": f"wss:subnet:{subnet_id}|hub:{hub_id}",
        "exp": int(datetime.now(timezone.utc).timestamp()) + 60,
        "cnf": {"jkt": owner_flow.thumbprint},
    }
    assertion = sign_assertion(owner_flow._key, {"alg": "ES256", "kid": owner_flow.thumbprint}, payload)
    response = backend_service.authorize_channel(
        device_id=device_id,
        channel_token=channel_token,
        assertion=assertion,
        hub_node_id=hub_id,
        idempotency_key="channel-auth",
    )
    assert response["device_id"] == device_id

    new_token = response["channel_token"]
    now = datetime.now(timezone.utc)
    with backend_service._conn:
        backend_service._conn.execute(
            "UPDATE tokens SET created_at = ?, expires_at = ? WHERE token = ?",
            (
                (now - timedelta(minutes=5)).isoformat(),
                (now + timedelta(seconds=20)).isoformat(),
                new_token,
            ),
        )
    owner_flow.storage.put("channel_token", new_token)
    owner_flow.storage.put("channel_issued_at", (now - timedelta(minutes=5)).isoformat())
    owner_flow.storage.put("channel_expires_at", (now + timedelta(seconds=20)).isoformat())

    rotated = owner_flow.ensure_channel(idempotency_key="ensure-channel", hub_node_id=hub_id)
    assert rotated["rotated"]
    rotated_token = rotated["channel_token"]
    assert rotated_token != new_token

    with pytest.raises(RootBackendError) as excinfo:
        bad_assertion = sign_assertion(
            owner_flow._key,
            {"alg": "ES256", "kid": owner_flow.thumbprint},
            {
                "nonce": new_token,
                "aud": f"wss:subnet:{subnet_id}|hub:{hub_id}",
                "exp": int(datetime.now(timezone.utc).timestamp()) + 30,
                "cnf": {"jkt": owner_flow.thumbprint},
            },
        )
        backend_service.authorize_channel(
            device_id=device_id,
            channel_token=new_token,
            assertion=bad_assertion,
            hub_node_id=hub_id,
            idempotency_key="channel-old",
        )
    assert excinfo.value.envelope.code in {"channel_revoked", "channel_expired"}

    wrong_payload = {
        "nonce": rotated_token,
        "aud": f"wss:subnet:{subnet_id}|hub:unknown",
        "exp": int(datetime.now(timezone.utc).timestamp()) + 60,
        "cnf": {"jkt": owner_flow.thumbprint},
    }
    wrong_assertion = sign_assertion(
        owner_flow._key,
        {"alg": "ES256", "kid": owner_flow.thumbprint},
        wrong_payload,
    )
    with pytest.raises(RootBackendError) as hub_error:
        backend_service.authorize_channel(
            device_id=device_id,
            channel_token=rotated_token,
            assertion=wrong_assertion,
            hub_node_id="unknown",
            idempotency_key="channel-wrong-hub",
        )
    assert hub_error.value.envelope.code in {"unknown_node", "forbidden"}
