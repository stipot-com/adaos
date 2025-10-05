from __future__ import annotations

import concurrent.futures
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from adaos.services.root import ClientContext, DeviceRole, RootAuthorityBackend, RootBackendError, Scope


def _write_config(path: Path, *, ttl: int = 600) -> Path:
    config = {
        "hmac_audit_key": "integration-test-key",
        "idempotency_ttl": ttl,
        "lifetimes": {
            "device_code_seconds": 30,
        },
        "rate_limits": {"device": 100, "qr": 100, "auth": 100},
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.fixture()
def context() -> ClientContext:
    return ClientContext(
        origin="https://app.example",
        client_ip="203.0.113.5",
        user_agent="pytest",
    )


@pytest.fixture()
def backend(tmp_path: Path, context: ClientContext) -> RootAuthorityBackend:
    db_path = tmp_path / "root.sqlite"
    config_path = tmp_path / "config.json"
    _write_config(config_path)
    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    yield backend
    backend.close()


def _device_start(
    backend: RootAuthorityBackend,
    *,
    subnet_id: str = "subnet-1",
    role: DeviceRole = DeviceRole.BROWSER_IO,
    scopes: list[Scope] | None = None,
    context: ClientContext,
    jwk_thumb: str = "thumb-1",
    idempotency_key: str | None,
) -> dict[str, Any]:
    return backend.device_start(
        subnet_id=subnet_id,
        role=role,
        scopes=scopes or [Scope.MANAGE_IO_DEVICES],
        context=context,
        jwk_thumb=jwk_thumb,
        idempotency_key=idempotency_key,
    )


def test_idempotent_post_returns_identical_payload(backend: RootAuthorityBackend, context: ClientContext) -> None:
    first = _device_start(backend, context=context, idempotency_key="key-1")
    second = _device_start(backend, context=context, idempotency_key="key-1")
    assert first == second


def test_parallel_requests_return_identical_payload_without_500(
    backend: RootAuthorityBackend, context: ClientContext
) -> None:
    def invoke() -> dict[str, Any]:
        return _device_start(backend, context=context, idempotency_key="parallel-key")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))

    assert len(results) == 2
    assert results[0] == results[1]


def test_missing_idempotency_key_returns_400_with_error_schema(
    backend: RootAuthorityBackend, context: ClientContext
) -> None:
    with pytest.raises(RootBackendError) as excinfo:
        _device_start(backend, context=context, idempotency_key=None)

    err = excinfo.value.envelope.as_dict()
    assert excinfo.value.status_code == 400
    assert err["code"] == "missing_idempotency_key"
    assert "message" in err
    assert "server_time_utc" in err
    assert "event_id" in err


def test_expired_idempotency_key_returns_409(tmp_path: Path, context: ClientContext) -> None:
    db_path = tmp_path / "root.sqlite"
    config_path = tmp_path / "config.json"
    _write_config(config_path, ttl=1)
    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    try:
        _device_start(backend, context=context, idempotency_key="ttl-key")
        time.sleep(1.2)
        with pytest.raises(RootBackendError) as excinfo:
            _device_start(backend, context=context, idempotency_key="ttl-key")
    finally:
        backend.close()

    err = excinfo.value.envelope.as_dict()
    assert excinfo.value.status_code == 409
    assert err["code"] == "idempotency_key_expired"


def test_server_time_and_event_id_present_on_success_and_error(
    backend: RootAuthorityBackend, context: ClientContext
) -> None:
    response = _device_start(backend, context=context, idempotency_key="key-2")
    server_time = datetime.fromisoformat(response["server_time_utc"])
    assert server_time.tzinfo is not None
    assert server_time.tzinfo.utcoffset(server_time) == timezone.utc.utcoffset(server_time)
    assert "event_id" in response

    with pytest.raises(RootBackendError) as excinfo:
        _device_start(backend, context=context, idempotency_key=None)

    err = excinfo.value.envelope.as_dict()
    err_time = datetime.fromisoformat(err["server_time_utc"])
    assert err_time.tzinfo is not None
    assert "event_id" in err


def test_sqlite_persistence_roundtrip_and_restart_survives(tmp_path: Path, context: ClientContext) -> None:
    db_path = tmp_path / "root.sqlite"
    config_path = tmp_path / "config.json"
    _write_config(config_path)

    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    response = _device_start(backend, context=context, idempotency_key="persist-1")
    backend.close()

    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    try:
        repeat = _device_start(backend, context=context, idempotency_key="persist-1")
    finally:
        backend.close()

    assert repeat == response


def test_context_hashes_are_hmac_sha256_if_present(backend: RootAuthorityBackend, context: ClientContext) -> None:
    response = _device_start(backend, context=context, idempotency_key="ctx-hash")
    record = backend.fetch_device_code(response["device_code"])
    assert record is not None
    import hmac
    import hashlib

    secret = backend.audit_key
    assert secret

    expected_ip = hmac.new(secret, context.client_ip.encode(), hashlib.sha256).hexdigest()
    expected_ua = hmac.new(secret, context.user_agent.encode(), hashlib.sha256).hexdigest()
    expected_origin = hmac.new(secret, context.origin.encode(), hashlib.sha256).hexdigest()

    assert record["ip_hash"] == expected_ip
    assert record["ua_hash"] == expected_ua
    assert record["origin"] == expected_origin


