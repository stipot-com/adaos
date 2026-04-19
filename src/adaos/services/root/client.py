# src\adaos\services\root\client.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional, Tuple
import httpx
import ssl, os
import logging
import time


class RootHttpError(RuntimeError):
    """Raised when the Root API returns an error response."""

    def __init__(self, message: str, *, status_code: int, error_code: str | None = None, payload: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload


_ROOT_HTTP_LOG = logging.getLogger("adaos.root-http")


@dataclass(slots=True)
class RootHttpClient:
    """HTTP client for the Inimatic Root API."""

    base_url: str = "https://api.inimatic.com"
    timeout: float = 15.0
    verify: str | bool | ssl.SSLContext = True
    # mTLS client cert (cert_file, key_file)
    cert: Optional[Tuple[str, str]] = None
    # default headers applied to every request (can be overridden/extended)
    default_headers: dict[str, str] = field(default_factory=dict)

    # ---------- public helpers ------------------------------------------------
    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        access_token: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> "RootHttpClient":
        """
        Factory that extracts base_url, mTLS paths and composes default headers.

        Expected settings fields (optional):
          - settings.api_base or settings.root.api_base
          - settings.pki.ca, settings.pki.cert, settings.pki.key
        """
        # base_url resolution
        base_url = getattr(settings, "api_base", None) or getattr(getattr(settings, "root", None), "api_base", None) or "https://api.inimatic.com"

        # TLS verification (CA) & client cert
        ca_path = getattr(getattr(settings, "pki", None), "ca", None)
        cert_path = getattr(getattr(settings, "pki", None), "cert", None)
        key_path = getattr(getattr(settings, "pki", None), "key", None)

        verify: str | bool | ssl.SSLContext = True
        cert_tuple: Optional[Tuple[str, str]] = None

        if ca_path and os.path.exists(ca_path):
            verify = ca_path
        if cert_path and key_path and os.path.exists(cert_path) and os.path.exists(key_path):
            cert_tuple = (cert_path, key_path)

        # default headers
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})

        return cls(base_url=base_url, verify=verify, cert=cert_tuple, default_headers=headers)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        data: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        accept_204: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """
        Public wrapper that automatically applies mTLS and default headers.
        """
        merged_headers: MutableMapping[str, str] | None = None
        if self.default_headers or headers:
            merged_headers = dict(self.default_headers)
            if headers:
                merged_headers.update({str(k): str(v) for k, v in headers.items()})

        return self._request(
            method,
            path,
            json=json,
            data=data,
            params=params,
            headers=merged_headers,
            verify=self.verify,
            cert=self.cert,
            timeout=timeout or self.timeout,
            accept_204=accept_204,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        data: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
        timeout: float | None = None,
        accept_204: bool = False,
    ) -> Any:
        trace_request = str(path or "").startswith("/v1/hub/control/")
        started = time.perf_counter()
        request_headers: MutableMapping[str, str] | None = None
        if headers:
            request_headers = {str(k): str(v) for k, v in headers.items()}
        effective_verify = self.verify if verify is None else verify
        if isinstance(effective_verify, str):
            mode = (os.getenv("ADAOS_ROOT_CA_MODE") or "append").strip().lower()
            if mode == "append":
                ca_path = Path(effective_verify)
                if ca_path.exists():
                    ctx = ssl.create_default_context()
                    # Add user-provided CA certificates without discarding system defaults.
                    ctx.load_verify_locations(cafile=str(ca_path))
                    effective_verify = ctx
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=timeout or self.timeout,
                verify=effective_verify,
                cert=cert,
            ) as client:
                response = client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    data=data,
                    headers=request_headers,
                )
        except httpx.RequestError as exc:  # pragma: no cover - network errors are environment specific
            if trace_request:
                _ROOT_HTTP_LOG.warning(
                    "root http failed method=%s path=%s duration_s=%.3f error_type=%s error=%s",
                    method,
                    path,
                    time.perf_counter() - started,
                    type(exc).__name__,
                    str(exc),
                )
            raise RootHttpError(f"{method} {path} failed: {exc}", status_code=0) from exc

        content: Any | None = None
        if response.content:
            try:
                content = response.json()
            except ValueError:
                content = response.text

        if response.status_code == 204 and accept_204:
            if trace_request:
                _ROOT_HTTP_LOG.info(
                    "root http response method=%s path=%s duration_s=%.3f status=%s",
                    method,
                    path,
                    time.perf_counter() - started,
                    response.status_code,
                )
            return {}

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
            if trace_request:
                _ROOT_HTTP_LOG.warning(
                    "root http response method=%s path=%s duration_s=%.3f status=%s error_code=%s",
                    method,
                    path,
                    time.perf_counter() - started,
                    response.status_code,
                    error_code,
                )
            raise RootHttpError(message, status_code=response.status_code, error_code=error_code, payload=content)

        if trace_request:
            _ROOT_HTTP_LOG.info(
                "root http response method=%s path=%s duration_s=%.3f status=%s",
                method,
                path,
                time.perf_counter() - started,
                response.status_code,
            )
        return content if content is not None else {}

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
        return dict(self._request("POST", "/v1/bootstrap_token", json=payload, headers=headers, verify=(verify if verify is not None else self.verify), timeout=30.0))

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
        return dict(self._request("POST", "/v1/subnets/register", json=payload, headers=headers, verify=(verify if verify is not None else self.verify), timeout=120.0))

    def dev_logs(
        self,
        *,
        root_token: str,
        minutes: int = 30,
        limit: int = 2000,
        hub_id: str | None = None,
        contains: str | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        params: dict[str, Any] = {"minutes": int(minutes), "limit": int(limit)}
        if hub_id:
            params["hub_id"] = hub_id
        if contains:
            params["contains"] = contains
        return dict(
            self._request(
                "GET",
                "/v1/dev/logs",
                params=params,
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def dev_log_files(
        self,
        *,
        root_token: str,
        contains: str | None = None,
        limit: int = 500,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        params: dict[str, Any] = {"limit": int(limit)}
        if contains:
            params["contains"] = contains
        return dict(
            self._request(
                "GET",
                "/v1/dev/log_files",
                params=params,
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def dev_log_tail(
        self,
        *,
        root_token: str,
        file: str,
        lines: int = 200,
        max_bytes: int = 2_000_000,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        params: dict[str, Any] = {"file": str(file), "lines": int(lines), "max_bytes": int(max_bytes)}
        return dict(
            self._request(
                "GET",
                "/v1/dev/log_tail",
                params=params,
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def device_authorize(
        self,
        *,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict:
        body = dict(payload or {})
        return dict(
            self._request("POST", "/v1/auth/owner/start", json=body, verify=(verify if verify is not None else self.verify), cert=(cert if cert is not None else self.cert))
        )

    def device_poll(
        self,
        device_code: str,
        *,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
    ) -> dict:
        body = {"device_code": device_code}
        return dict(self._request("POST", "/v1/auth/owner/poll", json=body, verify=(verify if verify is not None else self.verify), cert=(cert if cert is not None else self.cert)))

    def push_skill_draft(
        self,
        *,
        name: str,
        archive_b64: str,
        node_id: str | None,
        verify: str | bool | ssl.SSLContext = None,
        cert: tuple[str, str] | None = None,
        sha256: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": name, "archive_b64": archive_b64}
        if node_id:
            payload["node_id"] = node_id
        if sha256:
            payload["sha256"] = sha256
        return dict(
            self._request("POST", "/v1/skills/draft", json=payload, verify=(self.verify if verify is None else verify), cert=(self.cert if cert is None else cert), timeout=120.0)
        )

    def delete_draft_artifact(
        self,
        *,
        kind: str,
        name: str,
        node_id: str | None,  # если хаб удаляет драфт «от имени ноды»
        all_nodes: bool,  # true — для хаба удалить у всех нод
        verify: str | bool | ssl.SSLContext = None,
        cert: tuple[str, str] | None = None,
        timeout: float = 30.0,
    ) -> dict:
        params: dict[str, Any] = {"name": name}
        if node_id:
            params["node_id"] = node_id
        if all_nodes:
            params["all_nodes"] = "true"
        return dict(
            self._request(
                "DELETE",
                f"/v1/{kind}/draft",
                params=params,
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=timeout,
                accept_204=True,
            )
        )

    def delete_skill_draft(self, **kw) -> dict:
        kw["kind"] = "skills"
        return self.delete_draft_artifact(**kw)

    def delete_scenario_draft(self, **kw) -> dict:
        kw["kind"] = "scenarios"
        return self.delete_draft_artifact(**kw)

    def delete_registry_artifact(
        self,
        *,
        kind: str,  # 'skills' | 'scenarios'
        name: str,
        version: str | None,
        all_versions: bool,
        force: bool,
        verify: str | bool | ssl.SSLContext = None,
        cert: tuple[str, str] | None = None,
        timeout: float = 30.0,
    ) -> dict:
        params: dict[str, Any] = {"name": name}
        if version:
            params["version"] = version
        if all_versions:
            params["all_versions"] = "true"
        if force:
            params["force"] = "true"
        try:
            return dict(
                self._request(
                    "DELETE",
                    f"/v1/{kind}/registry",
                    params=params,
                    verify=(self.verify if verify is None else verify),
                    cert=(self.cert if cert is None else cert),
                    timeout=timeout,
                    accept_204=True,
                )
            )
        except RootHttpError as exc:
            # registry может отсутствовать — это НЕ ошибка удаления (идемпотентность)
            if getattr(exc, "status_code", None) == 404:
                return {}
            raise

    # шорткаты
    def delete_skill_registry(self, **kw) -> dict:
        kw["kind"] = "skills"
        return self.delete_registry_artifact(**kw)

    def delete_scenario_registry(self, **kw) -> dict:
        kw["kind"] = "scenarios"
        return self.delete_registry_artifact(**kw)

    def get_draft_info(
        self,
        *,
        kind: str,  # 'skills' | 'scenarios'
        name: str,
        node_id: str | None,
        verify: str | bool | ssl.SSLContext = None,
        cert: tuple[str, str] | None = None,
        timeout: float = 15.0,
    ) -> dict:
        params: dict[str, Any] = {"name": name}
        if node_id:
            params["node_id"] = node_id
        return dict(
            self._request(
                "GET",
                f"/v1/{kind}/draft",
                params=params,
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=timeout,
            )
        )

    def get_skill_draft_info(self, **kw) -> dict:
        kw["kind"] = "skills"
        return self.get_draft_info(**kw)

    def get_scenario_draft_info(self, **kw) -> dict:
        kw["kind"] = "scenarios"
        return self.get_draft_info(**kw)

    def get_draft_archive(
        self,
        *,
        kind: str,  # 'skills' | 'scenarios'
        name: str,
        node_id: str | None,
        verify: str | bool | ssl.SSLContext = None,
        cert: tuple[str, str] | None = None,
        timeout: float = 60.0,
    ) -> dict:
        params: dict[str, Any] = {"name": name}
        if node_id:
            params["node_id"] = node_id
        return dict(
            self._request(
                "GET",
                f"/v1/{kind}/draft/archive",
                params=params,
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=timeout,
            )
        )

    def get_skill_draft_archive(self, **kw) -> dict:
        kw["kind"] = "skills"
        return self.get_draft_archive(**kw)

    def get_scenario_draft_archive(self, **kw) -> dict:
        kw["kind"] = "scenarios"
        return self.get_draft_archive(**kw)

    def push_scenario_draft(
        self,
        *,
        name: str,
        archive_b64: str,
        node_id: str | None,
        verify: str | bool | ssl.SSLContext = None,
        cert: tuple[str, str] | None = None,
        sha256: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": name, "archive_b64": archive_b64}
        if node_id:
            payload["node_id"] = node_id
        if sha256:
            payload["sha256"] = sha256
        return dict(
            self._request(
                "POST", "/v1/scenarios/draft", json=payload, verify=(self.verify if verify is None else verify), cert=(self.cert if cert is None else cert), timeout=120.0
            )
        )

    def hub_core_update_report(
        self,
        *,
        payload: Mapping[str, Any],
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
    ) -> dict:
        return dict(
            self._request(
                "POST",
                "/v1/hub/core_update/report",
                json=dict(payload),
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=30.0,
            )
        )

    def hub_control_report(
        self,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
    ) -> dict:
        return dict(
            self._request(
                "POST",
                "/v1/hub/control/report",
                json=dict(payload),
                headers=headers,
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=30.0,
            )
        )

    def hub_memory_profile_report(
        self,
        *,
        payload: Mapping[str, Any],
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
    ) -> dict:
        return dict(
            self._request(
                "POST",
                "/v1/hub/memory_profile/report",
                json=dict(payload),
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=30.0,
            )
        )

    def root_dispatch_core_update(
        self,
        *,
        root_token: str,
        payload: Mapping[str, Any],
        rollback: bool = False,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        path = "/v1/hubs/core_update/rollback" if rollback else "/v1/hubs/core_update/start"
        return dict(
            self._request(
                "POST",
                path,
                json=dict(payload),
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def root_core_update_reports(
        self,
        *,
        root_token: str,
        hub_id: str | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        params: dict[str, Any] = {}
        if hub_id:
            params["hub_id"] = hub_id
        return dict(
            self._request(
                "GET",
                "/v1/hubs/core_update/reports",
                params=params,
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def root_control_reports(
        self,
        *,
        root_token: str,
        hub_id: str | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        params: dict[str, Any] = {}
        if hub_id:
            params["hub_id"] = hub_id
        return dict(
            self._request(
                "GET",
                "/v1/hubs/control/reports",
                params=params,
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def root_memory_profile_reports(
        self,
        *,
        root_token: str,
        hub_id: str | None = None,
        session_id: str | None = None,
        session_state: str | None = None,
        suspected_only: bool | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        params: dict[str, Any] = {}
        if hub_id:
            params["hub_id"] = hub_id
        if session_id:
            params["session_id"] = session_id
        if session_state:
            params["state"] = session_state
        if suspected_only is not None:
            params["suspected_only"] = "true" if suspected_only else "false"
        return dict(
            self._request(
                "GET",
                "/v1/hubs/memory_profile/reports",
                params=params,
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def root_memory_profile_report(
        self,
        *,
        root_token: str,
        session_id: str,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        return dict(
            self._request(
                "GET",
                f"/v1/hubs/memory_profile/reports/{session_id}",
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def root_memory_profile_artifact(
        self,
        *,
        root_token: str,
        session_id: str,
        artifact_id: str,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        return dict(
            self._request(
                "GET",
                f"/v1/hubs/memory_profile/reports/{session_id}/artifacts/{artifact_id}",
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def root_core_update_subnets(
        self,
        *,
        root_token: str,
        verify: str | bool | ssl.SSLContext | None = None,
    ) -> dict:
        headers = {"X-Root-Token": root_token}
        return dict(
            self._request(
                "GET",
                "/v1/hubs/core_update/subnets",
                headers=headers,
                verify=(verify if verify is not None else self.verify),
                timeout=30.0,
            )
        )

    def hub_core_update_release(
        self,
        *,
        branch: str | None = None,
        current_commit: str | None = None,
        verify: str | bool | ssl.SSLContext | None = None,
        cert: tuple[str, str] | None = None,
    ) -> dict:
        params: dict[str, Any] = {}
        if branch:
            params["branch"] = branch
        if current_commit:
            params["current_commit"] = current_commit
        return dict(
            self._request(
                "GET",
                "/v1/hub/core_update/release",
                params=params,
                verify=(self.verify if verify is None else verify),
                cert=(self.cert if cert is None else cert),
                timeout=30.0,
            )
        )


__all__ = ["RootHttpClient", "RootHttpError"]
