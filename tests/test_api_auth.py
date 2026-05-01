from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from adaos.apps.api import auth


def test_expected_token_prefers_runtime_env_over_ctx_config(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_TOKEN", "runtime-env-token")
    monkeypatch.setattr(auth, "get_ctx", lambda: SimpleNamespace(config=SimpleNamespace(token="stale-config-token")))

    assert auth._expected_token() == "runtime-env-token"


def test_ensure_token_accepts_runtime_env_token_when_ctx_config_is_stale(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_TOKEN", "runtime-env-token")
    monkeypatch.setattr(auth, "get_ctx", lambda: SimpleNamespace(config=SimpleNamespace(token="stale-config-token")))

    auth.ensure_token("runtime-env-token")


def test_ensure_token_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_TOKEN", "runtime-env-token")
    monkeypatch.setattr(auth, "get_ctx", lambda: SimpleNamespace(config=SimpleNamespace(token="stale-config-token")))

    try:
        auth.ensure_token("wrong-token")
    except HTTPException as exc:
        assert exc.status_code == 401
        assert exc.detail == "Invalid or missing X-AdaOS-Token"
    else:
        raise AssertionError("ensure_token() must reject unexpected tokens")


def test_require_token_accepts_x_adaos_token_header_via_request_headers(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_TOKEN", "dev-local-token")
    monkeypatch.setattr(auth, "get_ctx", lambda: SimpleNamespace(config=SimpleNamespace(token="stale-config-token")))

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(auth.require_token)])
    async def _protected() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/protected", headers={"X-AdaOS-Token": "dev-local-token"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
