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

    def get_managed_target(self, target_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/root/mcp/targets/{target_id}"))

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
