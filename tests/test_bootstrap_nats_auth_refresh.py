from __future__ import annotations

import json
import sys
import types
import time
from pathlib import Path

if "nats" not in sys.modules:
    sys.modules["nats"] = types.SimpleNamespace()
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
