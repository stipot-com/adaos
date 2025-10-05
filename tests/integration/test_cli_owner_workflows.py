from __future__ import annotations

import json
import threading
import time
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from adaos.apps.cli.commands.dev import (
    hub_bootstrap,
    root_consents_approve,
    root_consents_list,
    root_devices_alias_set,
    root_devices_list,
    root_devices_revoke,
    root_install,
)
from adaos.services.browser.flows import BrowserIODeviceFlow
from adaos.services.root import (
    ClientContext,
    ConsentStatus,
    DeviceRole,
    RootAuthorityBackend,
    Scope,
)
from adaos.services.root.cli_state import state_path


def _write_config(path: Path) -> Path:
    config = {
        "hmac_audit_key": "cli-testing",
        "context_hmac_key": "cli-context",
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
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base_dir = tmp_path / "adaos"
    config_path = _write_config(tmp_path / "config.json")
    db_path = tmp_path / "root.sqlite"
    env = {
        "ADAOS_TESTING": "1",
        "ADAOS_BASE_DIR": str(base_dir),
        "ADAOS_ROOT_DB_PATH": str(db_path),
        "ADAOS_ROOT_CONFIG_PATH": str(config_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    try:
        yield env, backend
    finally:
        backend.close()


def _confirm_device(backend: RootAuthorityBackend, row, env: dict[str, str]) -> None:
    context = ClientContext(
        origin=env.get("ADAOS_ROOT_ORIGIN", "https://app.inimatic.com"),
        client_ip=env.get("ADAOS_ROOT_CLIENT_IP", "127.0.0.1"),
        user_agent=env.get("ADAOS_ROOT_UA", "adaos-cli/1.0"),
    )
    backend.device_confirm(
        owner_device_id="owner-controller",
        user_code=row["user_code"],
        granted_scopes=[Scope.MANAGE_IO_DEVICES, Scope.MANAGE_MEMBERS],
        jwk_thumb=row["init_thumb"],
        public_key_pem=row["public_key_pem"],
        aliases=["CLI"],
        capabilities=["cli"],
        context=context,
        idempotency_key="confirm-cli-device",
    )


def _create_browser_device(backend: RootAuthorityBackend, subnet_id: str) -> str:
    qr_context = ClientContext(
        origin="https://app.inimatic.com",
        client_ip="203.0.113.120",
        user_agent="pytest-browser",
    )
    flow = BrowserIODeviceFlow(backend=backend, subnet_id=subnet_id, context=qr_context)
    flow.begin([Scope.EMIT_EVENT], idempotency_key="qr-start-cli")
    flow.approve(owner_device_id="owner-controller", scopes=[Scope.EMIT_EVENT], idempotency_key="qr-approve-cli")
    return flow.storage.get("device_id") or ""


def _pending_csr(backend: RootAuthorityBackend, subnet_id: str) -> str:
    csr_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "CLI Hub")]))
        .sign(csr_key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
    submission = backend.submit_csr(
        csr_pem=csr_pem,
        role=DeviceRole.HUB,
        subnet_id=subnet_id,
        scopes=[Scope.MANAGE_IO_DEVICES],
        idempotency_key="csr-cli",
    )
    return submission["consent_id"]


def test_cli_install_and_owner_commands(cli_environment) -> None:
    env, backend = cli_environment

    thread_result: dict[str, Any] = {}

    def _run_install() -> None:
        buffer = StringIO()
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                root_install(scope=[Scope.MANAGE_IO_DEVICES.value], wait_seconds=60)
        except BaseException as exc:  # noqa: BLE001
            thread_result["error"] = exc
            thread_result["output"] = buffer.getvalue()
        else:
            thread_result["error"] = None
            thread_result["output"] = buffer.getvalue()

    thread = threading.Thread(target=_run_install)
    thread.start()

    pending_row = None
    for _ in range(200):
        row = backend._conn.execute(
            "SELECT * FROM device_codes WHERE status = 'pending'"
        ).fetchone()
        if row:
            pending_row = row
            break
        if not thread.is_alive():
            break
        time.sleep(0.05)

    if pending_row is None:
        thread.join(timeout=5)
        output = thread_result.get("output", "")
        error = thread_result.get("error")
        if error is None:
            pytest.fail("cli install command did not start")
        pytest.fail(f"cli install failed: {error}\n{output}")

    _confirm_device(backend, pending_row, env)

    thread.join(timeout=10)
    assert thread_result.get("error") is None, thread_result.get("output", "")

    base_dir = Path(env["ADAOS_BASE_DIR"])
    state_file = state_path(base_dir)
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    cli_device_id = state["device_id"]
    subnet_id = state["subnet_id"]

    browser_device_id = _create_browser_device(backend, subnet_id)
    assert browser_device_id

    buffer = StringIO()
    assert os.environ.get("ADAOS_BASE_DIR") == env["ADAOS_BASE_DIR"]
    with redirect_stdout(buffer):
        root_devices_list(role=None)
    assert cli_device_id in buffer.getvalue()

    with redirect_stdout(StringIO()):
        root_devices_alias_set(device_id=browser_device_id, alias="display-unit")
    alias_row = backend._conn.execute(
        "SELECT alias_slug FROM device_aliases WHERE device_id = ?",
        (browser_device_id,),
    ).fetchone()
    assert alias_row is not None

    with redirect_stdout(StringIO()):
        root_devices_revoke(device_id=browser_device_id, reason="compromised")
    status_row = backend._conn.execute(
        "SELECT revoked FROM devices WHERE id = ?",
        (browser_device_id,),
    ).fetchone()
    assert status_row is not None and int(status_row["revoked"]) == 1

    consent_id = _pending_csr(backend, subnet_id)
    buffer = StringIO()
    with redirect_stdout(buffer):
        root_consents_list(status=ConsentStatus.PENDING, consent_type=None)
    assert consent_id in buffer.getvalue()

    with redirect_stdout(StringIO()):
        root_consents_approve(consent_id)

    with redirect_stdout(StringIO()):
        hub_bootstrap(display_name="CLI Hub", scope=[Scope.MANAGE_IO_DEVICES.value])
    hub_dir = base_dir / "root-cli" / "hub"
    assert (hub_dir / "hub.key").exists()
