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

    def list_contracts(self, *, surface: str | None = None, plane_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if surface:
            params["surface"] = surface
        if plane_id:
            params["plane_id"] = plane_id
        return dict(self._request("GET", "/v1/root/mcp/contracts", params=params))

    def list_planes(self) -> dict[str, Any]:
        return self.call("development.list_planes")

    def get_plane(self, plane_id: str) -> dict[str, Any]:
        return self.call("development.get_plane", arguments={"plane_id": str(plane_id)})

    def list_descriptors(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/root/mcp/descriptors"))

    def get_descriptor(self, descriptor_id: str, *, level: str = "std") -> dict[str, Any]:
        params: dict[str, Any] = {}
        if level:
            params["level"] = level
        return dict(self._request("GET", f"/v1/root/mcp/descriptors/{descriptor_id}", params=params))

    def get_adaos_dev_architecture_catalog(
        self,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call("adaos_dev.get_architecture_catalog", request_id=request_id, trace_id=trace_id, dry_run=dry_run)

    def get_adaos_dev_sdk_metadata(
        self,
        *,
        level: str = "std",
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "adaos_dev.get_sdk_metadata",
            arguments={"level": str(level)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_adaos_dev_template_catalog(
        self,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call("adaos_dev.get_template_catalog", request_id=request_id, trace_id=trace_id, dry_run=dry_run)

    def get_adaos_dev_public_skill_registry(
        self,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call("adaos_dev.get_public_skill_registry", request_id=request_id, trace_id=trace_id, dry_run=dry_run)

    def get_adaos_dev_public_scenario_registry(
        self,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call("adaos_dev.get_public_scenario_registry", request_id=request_id, trace_id=trace_id, dry_run=dry_run)

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

    def issue_session_lease(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/v1/root/mcp/sessions", json=dict(payload)))

    def list_session_leases(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        audience: str | None = None,
        target_id: str | None = None,
        capability_profile: str | None = None,
        active_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": int(limit)}
        if status:
            params["status"] = status
        if audience:
            params["audience"] = audience
        if target_id:
            params["target_id"] = target_id
        if capability_profile:
            params["capability_profile"] = capability_profile
        if active_only:
            params["active_only"] = True
        return dict(self._request("GET", "/v1/root/mcp/sessions", params=params))

    def get_session_lease(self, session_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/root/mcp/sessions/{session_id}"))

    def revoke_session_lease(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = str(reason)
        return dict(self._request("POST", f"/v1/root/mcp/sessions/{session_id}/revoke", json=payload))

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

    def get_target_status(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.get_status",
            arguments={"target_id": str(target_id)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_target_runtime_summary(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.get_runtime_summary",
            arguments={"target_id": str(target_id)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_target_activity_log(
        self,
        target_id: str,
        *,
        limit: int = 50,
        errors_only: bool = False,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.get_activity_log",
            arguments={
                "target_id": str(target_id),
                "limit": int(limit),
                "errors_only": bool(errors_only),
            },
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_target_capability_usage_summary(
        self,
        target_id: str,
        *,
        limit: int = 200,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.get_capability_usage_summary",
            arguments={"target_id": str(target_id), "limit": int(limit)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_profileops_status(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call("hub.memory.get_status", arguments={"target_id": str(target_id)}, request_id=request_id, trace_id=trace_id, dry_run=dry_run)

    def list_profileops_sessions(
        self,
        target_id: str,
        *,
        state: str | None = None,
        suspected_only: bool = False,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"target_id": str(target_id), "suspected_only": bool(suspected_only)}
        if state:
            arguments["state"] = str(state)
        return self.call("hub.memory.list_sessions", arguments=arguments, request_id=request_id, trace_id=trace_id, dry_run=dry_run)

    def get_profileops_session(
        self,
        target_id: str,
        session_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.get_session",
            arguments={"target_id": str(target_id), "session_id": str(session_id)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def list_profileops_incidents(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call("hub.memory.list_incidents", arguments={"target_id": str(target_id)}, request_id=request_id, trace_id=trace_id, dry_run=dry_run)

    def list_profileops_artifacts(
        self,
        target_id: str,
        session_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.list_artifacts",
            arguments={"target_id": str(target_id), "session_id": str(session_id)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_profileops_artifact(
        self,
        target_id: str,
        session_id: str,
        artifact_id: str,
        *,
        offset: int = 0,
        max_bytes: int = 256 * 1024,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.get_artifact",
            arguments={
                "target_id": str(target_id),
                "session_id": str(session_id),
                "artifact_id": str(artifact_id),
                "offset": int(offset),
                "max_bytes": int(max_bytes),
            },
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def start_profileops_session(
        self,
        target_id: str,
        *,
        profile_mode: str = "sampled_profile",
        reason: str = "root_mcp.memory.start",
        trigger_source: str = "root_mcp",
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.start_profile",
            arguments={
                "target_id": str(target_id),
                "profile_mode": str(profile_mode),
                "reason": str(reason),
                "trigger_source": str(trigger_source),
            },
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def stop_profileops_session(
        self,
        target_id: str,
        session_id: str,
        *,
        reason: str = "root_mcp.memory.stop",
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.stop_profile",
            arguments={"target_id": str(target_id), "session_id": str(session_id), "reason": str(reason)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def retry_profileops_session(
        self,
        target_id: str,
        session_id: str,
        *,
        reason: str = "root_mcp.memory.retry",
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.retry_profile",
            arguments={"target_id": str(target_id), "session_id": str(session_id), "reason": str(reason)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def publish_profileops_session(
        self,
        target_id: str,
        session_id: str,
        *,
        reason: str = "root_mcp.memory.publish",
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.memory.publish_profile",
            arguments={"target_id": str(target_id), "session_id": str(session_id), "reason": str(reason)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def get_target_logs(
        self,
        target_id: str,
        *,
        tail: int = 200,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.get_logs",
            arguments={"target_id": str(target_id), "tail": int(tail)},
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def run_target_healthchecks(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.run_healthchecks",
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

    def issue_target_mcp_session(
        self,
        target_id: str,
        *,
        audience: str,
        ttl_seconds: int | None = None,
        capability_profile: str | None = None,
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
        if capability_profile:
            arguments["capability_profile"] = str(capability_profile)
        if capabilities is not None:
            arguments["capabilities"] = [str(item) for item in capabilities]
        if note:
            arguments["note"] = str(note)
        return self.call(
            "hub.issue_mcp_session",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def list_target_mcp_sessions(
        self,
        target_id: str,
        *,
        limit: int = 50,
        status: str | None = None,
        capability_profile: str | None = None,
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
        if capability_profile:
            arguments["capability_profile"] = str(capability_profile)
        return self.call(
            "hub.list_mcp_sessions",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def revoke_target_mcp_session(
        self,
        target_id: str,
        session_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": str(target_id),
            "session_id": str(session_id),
        }
        if reason:
            arguments["reason"] = str(reason)
        return self.call(
            "hub.revoke_mcp_session",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def deploy_target_ref(
        self,
        target_id: str,
        *,
        ref: str,
        note: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": str(target_id),
            "ref": str(ref),
        }
        if note:
            arguments["note"] = str(note)
        return self.call(
            "hub.deploy_ref",
            arguments=arguments,
            request_id=request_id,
            trace_id=trace_id,
            dry_run=dry_run,
        )

    def rollback_last_test_deploy(
        self,
        target_id: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "hub.rollback_last_test_deploy",
            arguments={"target_id": str(target_id)},
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

    def get_yjs_load_mark_history(
        self,
        *,
        limit: int = 100,
        webspace_id: str | None = None,
        kind: str | None = None,
        bucket_id: str | None = None,
        display_contains: str | None = None,
        status: str | None = None,
        last_source: str | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": int(limit)}
        if webspace_id:
            params["webspace_id"] = webspace_id
        if kind:
            params["kind"] = kind
        if bucket_id:
            params["bucket_id"] = bucket_id
        if display_contains:
            params["display_contains"] = display_contains
        if status:
            params["status"] = status
        if last_source:
            params["last_source"] = last_source
        if since_ts is not None:
            params["since_ts"] = float(since_ts)
        if until_ts is not None:
            params["until_ts"] = float(until_ts)
        return dict(self._request("GET", "/v1/root/mcp/yjs/load-mark/history", params=params))

    def get_logs(
        self,
        category: str,
        *,
        limit: int = 5,
        lines: int = 200,
        contains: str | None = None,
        skill: str | None = None,
        file: str | None = None,
        scope: str | None = None,
        include_hub: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": int(limit),
            "lines": int(lines),
        }
        if contains:
            params["contains"] = contains
        if skill:
            params["skill"] = skill
        if file:
            params["file"] = file
        if scope:
            params["scope"] = scope
        if include_hub is not None:
            params["include_hub"] = bool(include_hub)
        return dict(self._request("GET", f"/v1/root/mcp/logs/{category}", params=params))

    def get_yjs_logs(
        self,
        *,
        limit: int = 5,
        lines: int = 200,
        contains: str | None = None,
        file: str | None = None,
        scope: str | None = None,
        include_hub: bool | None = None,
    ) -> dict[str, Any]:
        return self.get_logs("yjs", limit=limit, lines=lines, contains=contains, file=file, scope=scope, include_hub=include_hub)

    def get_skill_logs(
        self,
        *,
        limit: int = 5,
        lines: int = 200,
        skill: str | None = None,
        contains: str | None = None,
        file: str | None = None,
        scope: str | None = None,
        include_hub: bool | None = None,
    ) -> dict[str, Any]:
        return self.get_logs("skills", limit=limit, lines=lines, skill=skill, contains=contains, file=file, scope=scope, include_hub=include_hub)

    def get_adaos_logs(
        self,
        *,
        limit: int = 5,
        lines: int = 200,
        contains: str | None = None,
        file: str | None = None,
        scope: str | None = None,
        include_hub: bool | None = None,
    ) -> dict[str, Any]:
        return self.get_logs("adaos", limit=limit, lines=lines, contains=contains, file=file, scope=scope, include_hub=include_hub)

    def get_events_logs(
        self,
        *,
        limit: int = 5,
        lines: int = 200,
        contains: str | None = None,
        file: str | None = None,
        scope: str | None = None,
        include_hub: bool | None = None,
    ) -> dict[str, Any]:
        return self.get_logs("events", limit=limit, lines=lines, contains=contains, file=file, scope=scope, include_hub=include_hub)

    def get_subnet_info(
        self,
        *,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if target_id:
            params["target_id"] = target_id
        return dict(self._request("GET", "/v1/root/mcp/subnet/info", params=params))

    def get_subnet_analysis_health(
        self,
        *,
        target_id: str | None = None,
        probe_logs: bool = True,
        lines: int = 20,
        include_hub: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "probe_logs": bool(probe_logs),
            "lines": int(lines),
            "include_hub": bool(include_hub),
        }
        if target_id:
            params["target_id"] = target_id
        return dict(self._request("GET", "/v1/root/mcp/subnet/analysis-health", params=params))

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
