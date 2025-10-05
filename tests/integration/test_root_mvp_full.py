from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ObjectIdentifier
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from adaos.services.root import (
    ClientContext,
    DeviceRole,
    RootAuthorityBackend,
    RootBackendError,
    Scope,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _thumbprint_for_key(private_key: ec.EllipticCurvePrivateKey) -> tuple[str, str]:
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()
    x_bytes = public_numbers.x.to_bytes(32, "big")
    y_bytes = public_numbers.y.to_bytes(32, "big")
    jwk = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(x_bytes),
        "y": _b64url(y_bytes),
    }
    serialized = json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashes.Hash(hashes.SHA256())
    digest.update(serialized)
    thumb = _b64url(digest.finalize())
    pem = public_key.public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return thumb, pem


def _sign_jws(private_key: ec.EllipticCurvePrivateKey, header: dict[str, object], payload: dict[str, object]) -> str:
    header_json = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    header_b64 = _b64url(header_json)
    payload_b64 = _b64url(payload_json)
    message = f"{header_b64}.{payload_b64}".encode("ascii")
    signature_der = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature_der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    signature_b64 = _b64url(signature)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def _write_config(path: Path) -> Path:
    config = {
        "hmac_audit_key": "test-audit-key",
        "context_hmac_key": "test-context",
        "idempotency_ttl": 600,
        "rate_limits": {"device": 100, "qr": 100, "auth": 100},
        "lifetimes": {
            "access_token_seconds": 120,
            "refresh_token_seconds": 600,
            "channel_token_seconds": 60,
            "device_code_seconds": 120,
            "qr_session_seconds": 60,
            "challenge_seconds": 60,
            "consent_ttl_seconds": 300,
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


def test_full_device_flow_with_audit_and_csr(backend: RootAuthorityBackend) -> None:
    context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.5", user_agent="pytest")
    private_key = ec.generate_private_key(ec.SECP256R1())
    thumb, public_pem = _thumbprint_for_key(private_key)

    start = backend.device_start(
        subnet_id="subnet-1",
        role=DeviceRole.BROWSER_IO,
        scopes=[Scope.MANAGE_IO_DEVICES],
        context=context,
        jwk_thumb=thumb,
        public_key_pem=public_pem,
        idempotency_key="start-1",
    )

    confirm = backend.device_confirm(
        owner_device_id="owner-device",
        user_code=start["user_code"],
        granted_scopes=[Scope.MANAGE_IO_DEVICES],
        jwk_thumb=thumb,
        public_key_pem=public_pem,
        aliases=["Display"],
        capabilities=["display"],
        context=context,
        idempotency_key="confirm-1",
    )
    device_id = confirm["device_id"]

    payload = {
        "nonce": start["device_code"],
        "aud": f"subnet:{confirm['subnet_id']}",
        "exp": int(time.time()) + 60,
        "cnf": {"jkt": thumb},
    }
    assertion = _sign_jws(private_key, {"alg": "ES256", "kid": thumb}, payload)
    tokens = backend.device_token(device_code=start["device_code"], assertion=assertion, idempotency_key="token-1")
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    assert tokens["device_id"] == device_id

    # Introspect token using HoK assertion
    proof = _sign_jws(
        private_key,
        {"alg": "ES256", "kid": thumb},
        {"nonce": access_token, "aud": f"subnet:{confirm['subnet_id']}", "exp": int(time.time()) + 60, "cnf": {"jkt": thumb}},
    )
    introspection = backend.introspect_token(token=access_token, assertion=proof)
    assert introspection["token"] == access_token

    # Refresh tokens and ensure rotation succeeds
    refresh_proof = _sign_jws(
        private_key,
        {"alg": "ES256", "kid": thumb},
        {"nonce": refresh_token, "aud": f"subnet:{confirm['subnet_id']}", "exp": int(time.time()) + 60, "cnf": {"jkt": thumb}},
    )
    refreshed = backend.token_refresh(refresh_token=refresh_token, assertion=refresh_proof)
    assert refreshed["access_token"] != access_token

    # Browser challenge complete
    challenge = backend.auth_challenge(device_id=device_id, audience=f"subnet:{confirm['subnet_id']}", idempotency_key="challenge-1")
    auth_assertion = _sign_jws(
        private_key,
        {"alg": "ES256", "kid": thumb},
        {
            "nonce": challenge["nonce"],
            "aud": challenge["aud"],
            "exp": challenge["exp"],
            "cnf": {"jkt": thumb},
        },
    )
    auth_tokens = backend.auth_complete(device_id=device_id, assertion=auth_assertion, idempotency_key="auth-complete-1")
    assert auth_tokens["device_id"] == device_id

    # QR session approval
    qr_context = ClientContext(origin="https://app.inimatic.com", client_ip="203.0.113.50", user_agent="pytest-qr")
    qr_key = ec.generate_private_key(ec.SECP256R1())
    qr_thumb, qr_pem = _thumbprint_for_key(qr_key)
    qr_start = backend.qr_start(
        subnet_id="subnet-1",
        scopes=[Scope.EMIT_EVENT],
        context=qr_context,
        init_thumb=qr_thumb,
        idempotency_key="qr-start-1",
    )
    qr_approved = backend.qr_approve(
        owner_device_id="owner-device",
        session_id=qr_start["session_id"],
        jwk_thumb=qr_thumb,
        public_key_pem=qr_pem,
        scopes=[Scope.EMIT_EVENT],
        context=qr_context,
        idempotency_key="qr-approve-1",
    )
    browser_device_id = qr_approved["device_id"]

    # Alias update by another subnet member
    updated = backend.update_device(
        actor_device_id=browser_device_id,
        device_id=device_id,
        aliases=["display-main"],
        capabilities=None,
        idempotency_key="alias-1",
    )
    assert "display-main" in updated["aliases"]

    # Alias conflict detection
    with pytest.raises(RootBackendError):
        backend.update_device(
            actor_device_id=browser_device_id,
            device_id=browser_device_id,
            aliases=["display-main"],
            capabilities=None,
            idempotency_key="alias-conflict",
        )

    # CSR flow for hub
    csr_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "AdaOS Hub")]))
        .sign(csr_key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
    submission = backend.submit_csr(
        csr_pem=csr_pem,
        role=DeviceRole.HUB,
        subnet_id="subnet-1",
        scopes=[Scope.MANAGE_IO_DEVICES],
        idempotency_key="csr-submit-1",
    )
    consent_id = submission["consent_id"]
    backend.resolve_consent(owner_device_id="owner-device", consent_id=consent_id, approve=True, idempotency_key="consent-1")
    certificate = backend.collect_certificate(consent_id=consent_id)
    cert = x509.load_pem_x509_certificate(certificate["cert"].encode("ascii"))
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    san_values = [uri.value for uri in sans]
    assert f"urn:adaos:role={DeviceRole.HUB.value}" in san_values
    assert f"urn:adaos:subnet=subnet-1" in san_values
    assert any(value.startswith("urn:adaos:scopes=") for value in san_values)
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca is False
    key_usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert key_usage.key_cert_sign is False
    scope_extension = cert.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.57264.1")).value
    scope_payload = json.loads(scope_extension.value.decode("utf-8"))
    assert scope_payload["scopes"] == [Scope.MANAGE_IO_DEVICES.value]
    chain_blocks = [
        chunk
        for chunk in certificate["chain"].split("-----END CERTIFICATE-----")
        if "-----BEGIN CERTIFICATE-----" in chunk
    ]
    assert len(chain_blocks) == 2

    # Subnet CA delegation for offline issuance
    subnet_ca_key = ec.generate_private_key(ec.SECP256R1())
    subnet_csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Subnet CA")]))
        .sign(subnet_ca_key, hashes.SHA256())
    )
    subnet_submission = backend.submit_csr(
        csr_pem=subnet_csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        role=DeviceRole.SERVICE,
        subnet_id="subnet-1",
        node_id=submission["node_id"],
        scopes=[Scope.MANAGE_MEMBERS],
        idempotency_key="csr-subnet-ca",
    )
    backend.resolve_consent(
        owner_device_id="owner-device",
        consent_id=subnet_submission["consent_id"],
        approve=True,
        idempotency_key="consent-subnet-ca",
    )
    subnet_bundle = backend.collect_certificate(consent_id=subnet_submission["consent_id"])
    subnet_cert = x509.load_pem_x509_certificate(subnet_bundle["cert"].encode("ascii"))
    subnet_constraints = subnet_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert subnet_constraints.ca is True and subnet_constraints.path_length == 0
    subnet_sans = subnet_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    subnet_values = [uri.value for uri in subnet_sans]
    assert f"urn:adaos:role={DeviceRole.SERVICE.value}" in subnet_values
    subnet_key_usage = subnet_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert subnet_key_usage.key_cert_sign is True
    subnet_chain_blocks = [
        chunk
        for chunk in subnet_bundle["chain"].split("-----END CERTIFICATE-----")
        if "-----BEGIN CERTIFICATE-----" in chunk
    ]
    assert len(subnet_chain_blocks) == 2

    # Revoke device and ensure tokens invalidated
    backend.revoke_device(actor_device_id="owner-device", device_id=device_id, reason="compromised", idempotency_key="revoke-1")
    with pytest.raises(RootBackendError):
        backend.introspect_token(
            token=access_token,
            assertion=_sign_jws(
                private_key,
                {"alg": "ES256", "kid": thumb},
                {"nonce": access_token, "aud": f"subnet:{confirm['subnet_id']}", "exp": int(time.time()) + 60, "cnf": {"jkt": thumb}},
            ),
        )

    # Audit export verifies signatures
    export = backend.export_audit(subnet_id="subnet-1")
    ndjson = export.as_ndjson()
    assert ndjson
    for line in ndjson.splitlines():
        record = json.loads(line)
        assert record["subnet_id"] == "subnet-1"
        assert "signature" in record
