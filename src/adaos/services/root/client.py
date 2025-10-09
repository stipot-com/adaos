from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

import httpx
import ssl


class RootHttpError(RuntimeError):
    """Raised when the Root API returns an error response."""

    def __init__(self, message: str, *, status_code: int, error_code: str | None = None, payload: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload


@dataclass(slots=True)
class RootHttpClient:
    """HTTP client for the Inimatic Root API."""

    base_url: str = "https://api.inimatic.com"
    timeout: float = 15.0
    verify: str | bool | ssl.SSLContext = True

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        request_headers: MutableMapping[str, str] | None = None
        if headers:
            request_headers = {str(k): str(v) for k, v in headers.items()}
        effective_verify = self.verify if verify is None else verify
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=timeout or self.timeout,
                verify=effective_verify,
                cert=cert,
            ) as client:
                response = client.request(method, path, json=json, headers=request_headers)
        except httpx.RequestError as exc:  # pragma: no cover - network errors are environment specific
            raise RootHttpError(f"{method} {path} failed: {exc}", status_code=0) from exc

        content: Any | None = None
        if response.content:
            try:
                content = response.json()
            except ValueError:
                content = response.text

        if response.status_code >= 400:
            error_code: str | None = None
            message = response.text or f"HTTP {response.status_code}"
            if isinstance(content, Mapping):
                detail = content.get("detail") or content.get("message") or content.get("error")
                if isinstance(detail, str):
                    message = detail
                code = content.get("code") or content.get("error")
                if isinstance(code, str):
                    error_code = code
            raise RootHttpError(message, status_code=response.status_code, error_code=error_code, payload=content)

        return content

    # ------------------------------------------------------------------
    # Legacy owner authentication endpoints (used by internal services)
    # ------------------------------------------------------------------
    def owner_start(self, owner_id: str) -> dict:
        return dict(self._request("POST", "/v1/auth/owner/start", json={"owner_id": owner_id}))

    def owner_poll(self, device_code: str) -> dict:
        return dict(self._request("POST", "/v1/auth/owner/poll", json={"device_code": device_code}))

    def token_refresh(self, refresh_token: str) -> dict:
        return dict(self._request("POST", "/v1/auth/owner/refresh", json={"refresh_token": refresh_token}))

    def whoami(self, access_token: str) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        result = self._request("GET", "/v1/whoami", headers=headers)
        return dict(result) if isinstance(result, Mapping) else {}

    # Owner hubs --------------------------------------------------------
    def owner_hubs_list(self, access_token: str) -> list[dict]:
        headers = {"Authorization": f"Bearer {access_token}"}
        result = self._request("GET", "/v1/owner/hubs", headers=headers)
        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, Mapping)]
        return []

    def owner_hubs_add(self, access_token: str, hub_id: str) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        payload = {"hub_id": hub_id}
        return dict(self._request("POST", "/v1/owner/hubs", headers=headers, json=payload))

    # PKI ---------------------------------------------------------------
    def pki_enroll(self, access_token: str, hub_id: str, csr_pem: str, ttl: str | None) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        payload: dict[str, Any] = {"hub_id": hub_id, "csr_pem": csr_pem}
        if ttl:
            payload["ttl"] = ttl
        return dict(self._request("POST", "/v1/pki/enroll", headers=headers, json=payload))

    # ------------------------------------------------------------------
    # Root bootstrap & developer endpoints
    # ------------------------------------------------------------------
    def request_bootstrap_token(
        self,
        root_token: str,
        *,
        meta: Mapping[str, Any] | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        payload = dict(meta or {})
        return dict(self._request("POST", "/v1/bootstrap_token", json=payload, headers=headers, verify=verify, timeout=30.0))

    def register_subnet(
        self,
        csr_pem: str,
        *,
        bootstrap_token: str,
        verify: str | bool | ssl.SSLContext | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        headers: dict[str, str] = {"X-Bootstrap-Token": bootstrap_token}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        payload = {"csr_pem": csr_pem}
        return dict(self._request("POST", "/v1/subnets/register", json=payload, headers=headers, verify=verify, timeout=120.0))

    def device_authorize(
        self,
        *,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict:
        body = dict(payload or {})
        return dict(self._request("POST", "/v1/device/authorize", json=body, verify=verify, cert=cert))

    def device_poll(
        self,
        device_code: str,
        *,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
    ) -> dict:
        body = {"device_code": device_code}
        return dict(self._request("POST", "/v1/device/poll", json=body, verify=verify, cert=cert))

    def push_skill_draft(
        self,
        *,
        name: str,
        archive_b64: str,
        node_id: str | None,
        verify: str | bool | ssl.SSLContext,
        cert: tuple[str, str],
        sha256: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": name, "archive_b64": archive_b64}
        if node_id:
            payload["node_id"] = node_id
        if sha256:
            payload["sha256"] = sha256
        return dict(
            self._request(
                "POST",
                "/v1/skills/draft",
                json=payload,
                verify=verify,
                cert=cert,
                timeout=120.0,
            )
        )

    def push_scenario_draft(
        self,
        *,
        name: str,
        archive_b64: str,
        node_id: str | None,
        verify: str | bool | ssl.SSLContext,
        cert: tuple[str, str],
        sha256: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": name, "archive_b64": archive_b64}
        if node_id:
            payload["node_id"] = node_id
        if sha256:
            payload["sha256"] = sha256
        return dict(
            self._request(
                "POST",
                "/v1/scenarios/draft",
                json=payload,
                verify=verify,
                cert=cert,
                timeout=120.0,
            )
        )


__all__ = ["RootHttpClient", "RootHttpError"]
