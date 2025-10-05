from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import hashlib
import hmac

from adaos.services.root import ClientContext, DeviceRole, RootAuthorityBackend, RootBackendError, Scope


@pytest.fixture()
def backend(tmp_path):
    db_path = tmp_path / "root.sqlite"
    backend = RootAuthorityBackend(db_path=db_path)
    yield backend
    backend.close()


def _context() -> ClientContext:
    return ClientContext(origin="https://owner.app", client_ip="198.51.100.10", user_agent="pytest")


def test_idempotency_cache_expires(backend):
    ctx = _context()
    first = backend.device_start(
        subnet_id="subnet-1",
        role=DeviceRole.BROWSER_IO,
        scopes=[Scope.MANAGE_IO_DEVICES],
        context=ctx,
        jwk_thumb="thumb-1",
        idempotency_key="same-key",
    )
    backend._clock = lambda: datetime.now(tz=timezone.utc) + timedelta(minutes=20)
    with pytest.raises(RootBackendError) as excinfo:
        backend.device_start(
            subnet_id="subnet-1",
            role=DeviceRole.BROWSER_IO,
            scopes=[Scope.MANAGE_IO_DEVICES],
            context=ctx,
            jwk_thumb="thumb-1",
            idempotency_key="same-key",
        )
    assert excinfo.value.envelope.code == "idempotency_key_expired"


def test_missing_idempotency_key_raises(backend):
    ctx = _context()
    with pytest.raises(RootBackendError) as excinfo:
        backend.device_start(
            subnet_id="subnet-1",
            role=DeviceRole.BROWSER_IO,
            scopes=[Scope.MANAGE_IO_DEVICES],
            context=ctx,
            jwk_thumb="thumb-1",
            idempotency_key="",
        )
    assert excinfo.value.envelope.code == "missing_idempotency_key"


def test_fetch_device_code_roundtrip(backend):
    ctx = _context()
    response = backend.device_start(
        subnet_id="subnet-1",
        role=DeviceRole.BROWSER_IO,
        scopes=[Scope.MANAGE_IO_DEVICES],
        context=ctx,
        jwk_thumb="thumb-1",
        idempotency_key="unique-1",
    )
    record = backend.fetch_device_code(response["device_code"])
    assert record["user_code"] == response["user_code"]
    expected = hmac.new(backend.audit_key, ctx.origin.encode(), hashlib.sha256).hexdigest()
    assert record["origin"] == expected

