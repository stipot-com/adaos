from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from adaos.services.root.client import RootHttpClient


@dataclass(slots=True)
class RootMcpClientConfig:
    root_url: str
    subnet_id: str | None = None
    access_token: str | None = None
    zone: str | None = None
    timeout: float = 15.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    def headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.subnet_id:
            headers["X-AdaOS-Subnet-Id"] = str(self.subnet_id)
        if self.zone:
            headers["X-AdaOS-Zone"] = str(self.zone)
        return headers


@dataclass(slots=True)
class RootMcpClient:
    config: RootMcpClientConfig
    http: RootHttpClient | None = None

    def __post_init__(self) -> None:
        if self.http is None:
            self.http = RootHttpClient(
                base_url=self.config.root_url,
                timeout=float(self.config.timeout),
                default_headers=self.config.headers(),
            )

    def foundation(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/root/mcp/foundation"))

    def list_contracts(self, *, surface: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if surface:
            params["surface"] = surface
        return dict(self._request("GET", "/v1/root/mcp/contracts", params=params))

    def list_descriptors(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/root/mcp/descriptors"))

    def get_descriptor(self, descriptor_id: str, *, level: str = "std") -> dict[str, Any]:
        params: dict[str, Any] = {}
        if level:
            params["level"] = level
        return dict(self._request("GET", f"/v1/root/mcp/descriptors/{descriptor_id}", params=params))

    def list_managed_targets(self, *, environment: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment
        return dict(self._request("GET", "/v1/root/mcp/targets", params=params))

    def upsert_managed_target(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/v1/root/mcp/targets", json=dict(payload)))

    def get_managed_target(self, target_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/root/mcp/targets/{target_id}"))

    def issue_access_token(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/v1/root/mcp/access-tokens", json=dict(payload)))

    def list_access_tokens(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        audience: str | None = None,
        target_id: str | None = None,
        active_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": int(limit)}
        if status:
            params["status"] = status
        if audience:
            params["audience"] = audience
        if target_id:
            params["target_id"] = target_id
        if active_only:
            params["active_only"] = True
        return dict(self._request("GET", "/v1/root/mcp/access-tokens", params=params))

    def revoke_access_token(self, token_id: str, *, reason: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = str(reason)
        return dict(self._request("POST", f"/v1/root/mcp/access-tokens/{token_id}/revoke", json=payload))

    def get_operational_surface(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.get_operational_surface",
            arguments={"target_id": str(target_id)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def issue_target_access_token(
        self,
        target_id: str,
        *,
        audience: str,
        ttl_seconds: int | None = None,
        capabilities: list[str] | None = None,
        note: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": str(target_id),
            "audience": str(audience),
        }
        if ttl_seconds is not None:
            arguments["ttl_seconds"] = int(ttl_seconds)
        if capabilities is not None:
            arguments["capabilities"] = [str(item) for item in capabilities]
        if note:
            arguments["note"] = str(note)
        return self.call(
            "hub.issue_access_token",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def list_target_access_tokens(
        self,
        target_id: str,
        *,
        limit: int = 50,
        status: str | None = None,
        active_only: bool = False,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": str(target_id),
            "limit": int(limit),
            "active_only": bool(active_only),
        }
        if status:
            arguments["status"] = str(status)
        return self.call(
            "hub.list_access_tokens",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def revoke_target_access_token(
        self,
        target_id: str,
        token_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": str(target_id),
            "token_id": str(token_id),
        }
        if reason:
            arguments["reason"] = str(reason)
        return self.call(
            "hub.revoke_access_token",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def recent_audit(
        self,
        *,
        limit: int = 50,
        tool_id: str | None = None,
        trace_id: str | None = None,
        target_id: str | None = None,
        subnet_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": int(limit)}
        if tool_id:
            params["tool_id"] = tool_id
        if trace_id:
            params["trace_id"] = trace_id
        if target_id:
            params["target_id"] = target_id
        if subnet_id:
            params["subnet_filter"] = subnet_id
        return dict(self._request("GET", "/v1/root/mcp/audit", params=params))

    def call(
        self,
        tool_id: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_id": str(tool_id),
            "arguments": dict(arguments or {}),
            "dry_run": bool(dry_run),
        }
        if request_id:
            payload["request_id"] = str(request_id)
        if trace_id:
            payload["trace_id"] = str(trace_id)
        return dict(self._request("POST", "/v1/root/mcp/call", json=payload))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        client = self.http
        if client is None:  # pragma: no cover - guarded by __post_init__
            raise RuntimeError("RootMcpClient is not initialized")
        return client.request(method, path, json=json, params=params)


__all__ = ["RootMcpClient", "RootMcpClientConfig"]
