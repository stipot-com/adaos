from __future__ import annotations

import json
import time
from pathlib import Path

from adaos.services import bootstrap as bootstrap_mod


def _write_sidecar_diag(path: Path, *, ts: float, last_error: str | None) -> None:
    record = {
        "ts": ts,
        "remote_url": "wss://nats.inimatic.com/nats",
        "last_error": last_error,
    }
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_should_refresh_nats_credentials_for_explicit_auth_failure() -> None:
    err = Exception("nats: 'Authorization Violation'")

    assert bootstrap_mod._should_refresh_nats_credentials(err, server="nats://127.0.0.1:7422") is True
    assert bootstrap_mod._nats_credentials_refresh_evidence(err, server="nats://127.0.0.1:7422") == "explicit_auth_error"


def test_should_not_refresh_nats_credentials_for_unexpected_eof_without_sidecar_auth_signal(tmp_path: Path) -> None:
    diag_path = tmp_path / "realtime_sidecar.jsonl"
    err = Exception("nats: unexpected EOF")

    assert (
        bootstrap_mod._should_refresh_nats_credentials(
            err,
            server="nats://127.0.0.1:7422",
            sidecar_diag_file=diag_path,
        )
        is False
    )
    assert (
        bootstrap_mod._nats_credentials_refresh_evidence(
            err,
            server="nats://127.0.0.1:7422",
            sidecar_diag_file=diag_path,
        )
        is None
    )


def test_should_refresh_nats_credentials_for_unexpected_eof_only_with_fresh_sidecar_auth_signal(tmp_path: Path) -> None:
    diag_path = tmp_path / "realtime_sidecar.jsonl"
    _write_sidecar_diag(
        diag_path,
        ts=time.time(),
        last_error="ConnectionClosedError: received 1008 (policy violation) Authentication Failure",
    )
    err = Exception("nats: unexpected EOF")

    assert (
        bootstrap_mod._should_refresh_nats_credentials(
            err,
            server="nats://127.0.0.1:7422",
            sidecar_diag_file=diag_path,
        )
        is True
    )
    assert (
        bootstrap_mod._nats_credentials_refresh_evidence(
            err,
            server="nats://127.0.0.1:7422",
            sidecar_diag_file=diag_path,
        )
        == "sidecar_auth_failure_after_eof"
    )


def test_should_not_refresh_nats_credentials_for_stale_sidecar_auth_signal(tmp_path: Path) -> None:
    diag_path = tmp_path / "realtime_sidecar.jsonl"
    _write_sidecar_diag(
        diag_path,
        ts=time.time() - 120.0,
        last_error="ConnectionClosedError: received 1008 (policy violation) Authentication Failure",
    )
    err = Exception("nats: unexpected EOF")

    assert (
        bootstrap_mod._should_refresh_nats_credentials(
            err,
            server="nats://127.0.0.1:7422",
            sidecar_diag_file=diag_path,
        )
        is False
    )


def test_should_not_refresh_nats_credentials_for_unexpected_eof_on_non_sidecar_server(tmp_path: Path) -> None:
    diag_path = tmp_path / "realtime_sidecar.jsonl"
    _write_sidecar_diag(
        diag_path,
        ts=time.time(),
        last_error="ConnectionClosedError: received 1008 (policy violation) Authentication Failure",
    )
    err = Exception("nats: unexpected EOF")

    assert (
        bootstrap_mod._should_refresh_nats_credentials(
            err,
            server="nats://nats.inimatic.com:4222",
            sidecar_diag_file=diag_path,
        )
        is False
    )
