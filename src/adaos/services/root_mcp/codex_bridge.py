from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .client import RootMcpClient, RootMcpClientConfig
from .tokens import DEFAULT_ACCESS_TOKEN_CAPABILITIES


DEFAULT_CODEX_TARGET_CAPABILITIES: list[str] = [
    *DEFAULT_ACCESS_TOKEN_CAPABILITIES,
    "hub.get_operational_surface",
    "hub.get_status",
    "hub.get_runtime_summary",
    "hub.get_activity_log",
    "hub.get_capability_usage_summary",
    "hub.get_logs",
    "hub.run_healthchecks",
]


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_text(value: Any) -> str | None:
    token = str(value or "").strip()
    return token or None


def _normalize_unique(items: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in items or []:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(slots=True)
class CodexBridgeProfile:
    root_url: str
    target_id: str | None = None
    subnet_id: str | None = None
    zone: str | None = None
    bootstrap_mode: str = "mcp_session_lease"
    session_id: str | None = None
    capability_profile: str | None = None
    access_token: str | None = None
    access_token_file: str | None = None
    server_name: str = "adaos-test-hub"
    audience: str = "codex-vscode"
    generated_at: str | None = None
    capabilities: list[str] = field(default_factory=lambda: list(DEFAULT_CODEX_TARGET_CAPABILITIES))

    def resolved_access_token(self) -> str:
        direct = _normalize_text(self.access_token)
        if direct:
            return direct
        env_name = _normalize_text(os.getenv("ADAOS_MCP_ACCESS_TOKEN_ENV"))
        if env_name:
            token = _normalize_text(os.getenv(env_name))
            if token:
                return token
        path = _normalize_text(self.access_token_file)
        if path:
            try:
                token = Path(path).read_text(encoding="utf-8").strip()
            except Exception as exc:  # pragma: no cover - file errors depend on host
                raise RuntimeError(f"failed to read Root MCP access token from {path}: {exc}") from exc
            if token:
                return token
        raise RuntimeError("Root MCP access token is missing; re-run 'adaos dev root mcp prepare-codex'")

    def client_config(self) -> RootMcpClientConfig:
        return RootMcpClientConfig(
            root_url=str(self.root_url),
            subnet_id=self.subnet_id,
            zone=self.zone,
            access_token=self.resolved_access_token(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "root_url": self.root_url,
            "target_id": self.target_id,
            "subnet_id": self.subnet_id,
            "zone": self.zone,
            "bootstrap_mode": self.bootstrap_mode,
            "session_id": self.session_id,
            "capability_profile": self.capability_profile,
            "access_token_file": self.access_token_file,
            "audience": self.audience,
            "generated_at": self.generated_at or _iso_now(),
            "capabilities": list(self.capabilities),
        }


def default_profile_paths(base_dir: str | Path, server_name: str) -> tuple[Path, Path]:
    token = str(server_name or "adaos-test-hub").strip() or "adaos-test-hub"
    safe_name = token.replace(":", "-").replace("/", "-").replace("\\", "-").replace(" ", "-")
    root = Path(base_dir) / ".adaos" / "mcp"
    return root / f"{safe_name}.profile.json", root / f"{safe_name}.token"


def load_codex_bridge_profile(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> CodexBridgeProfile:
    environment = dict(env or os.environ)
    profile_path = _normalize_text(path) or _normalize_text(environment.get("ADAOS_MCP_PROFILE"))
    payload: dict[str, Any] = {}
    if profile_path:
        resolved = Path(profile_path)
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"failed to read Codex MCP profile {resolved}: {exc}") from exc
    root_url = _normalize_text(payload.get("root_url")) or _normalize_text(environment.get("ADAOS_MCP_ROOT_URL"))
    if not root_url:
        raise RuntimeError("Root MCP bridge requires root_url; set ADAOS_MCP_PROFILE or ADAOS_MCP_ROOT_URL")
    return CodexBridgeProfile(
        root_url=root_url,
        target_id=_normalize_text(payload.get("target_id")) or _normalize_text(environment.get("ADAOS_MCP_TARGET_ID")),
        subnet_id=_normalize_text(payload.get("subnet_id")) or _normalize_text(environment.get("ADAOS_MCP_SUBNET_ID")),
        zone=_normalize_text(payload.get("zone")) or _normalize_text(environment.get("ADAOS_MCP_ZONE")),
        bootstrap_mode=_normalize_text(payload.get("bootstrap_mode")) or "mcp_session_lease",
        session_id=_normalize_text(payload.get("session_id")) or _normalize_text(environment.get("ADAOS_MCP_SESSION_ID")),
        capability_profile=_normalize_text(payload.get("capability_profile")) or _normalize_text(environment.get("ADAOS_MCP_CAPABILITY_PROFILE")),
        access_token=_normalize_text(payload.get("access_token")) or _normalize_text(environment.get("ADAOS_MCP_ACCESS_TOKEN")),
        access_token_file=_normalize_text(payload.get("access_token_file")) or _normalize_text(environment.get("ADAOS_MCP_ACCESS_TOKEN_FILE")),
        server_name=_normalize_text(payload.get("server_name")) or "adaos-test-hub",
        audience=_normalize_text(payload.get("audience")) or "codex-vscode",
        generated_at=_normalize_text(payload.get("generated_at")),
        capabilities=_normalize_unique(list(payload.get("capabilities") or [])) or list(DEFAULT_CODEX_TARGET_CAPABILITIES),
    )


def write_codex_bridge_profile(
    *,
    profile_path: str | Path,
    token_path: str | Path,
    profile: CodexBridgeProfile,
    access_token: str,
) -> tuple[Path, Path]:
    profile_file = Path(profile_path)
    token_file = Path(token_path)
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(str(access_token).strip() + "\n", encoding="utf-8")
    stored = CodexBridgeProfile(
        root_url=profile.root_url,
        target_id=profile.target_id,
        subnet_id=profile.subnet_id,
        zone=profile.zone,
        bootstrap_mode=profile.bootstrap_mode,
        session_id=profile.session_id,
        capability_profile=profile.capability_profile,
        access_token_file=str(token_file),
        server_name=profile.server_name,
        audience=profile.audience,
        generated_at=profile.generated_at or _iso_now(),
        capabilities=_normalize_unique(profile.capabilities) or list(DEFAULT_CODEX_TARGET_CAPABILITIES),
    )
    profile_file.write_text(_json_text(stored.to_dict()) + "\n", encoding="utf-8")
    return profile_file, token_file


def build_codex_stdio_command(
    *,
    server_name: str,
    python_executable: str,
    profile_path: str | Path,
) -> list[str]:
    return [
        "codex",
        "mcp",
        "add",
        str(server_name or "adaos-test-hub").strip() or "adaos-test-hub",
        "--env",
        f"ADAOS_MCP_PROFILE={Path(profile_path)}",
        "--",
        str(python_executable),
        "-m",
        "adaos",
        "dev",
        "root",
        "mcp",
        "serve",
    ]


def _tool_text(payload: Any, *, error: bool = False) -> dict[str, Any]:
    text = _json_text(payload)
    response = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
    }
    if error:
        response["isError"] = True
    return response


class CodexRootMcpBridge:
    def __init__(self, profile: CodexBridgeProfile):
        self.profile = profile

    def _client(self) -> RootMcpClient:
        return RootMcpClient(self.profile.client_config())

    def _effective_target_id(self, arguments: Mapping[str, Any] | None) -> str:
        target_id = _normalize_text((arguments or {}).get("target_id")) or self.profile.target_id
        if not target_id:
            raise ValueError("target_id is required for this bridge call")
        return target_id

    def instructions(self) -> str:
        target = self.profile.target_id or "the configured managed target"
        bootstrap = "MCP Session Lease" if self.profile.bootstrap_mode == "mcp_session_lease" else "bounded access token"
        return (
            "This MCP server is a local stdio bridge from Codex to AdaOS Root MCP. "
            f"It is currently bound to {target} using {bootstrap}. "
            "For descriptive AdaOS programming context, prefer get_architecture_catalog, get_sdk_metadata, "
            "get_template_catalog, and public registry summaries from AdaOSDevPlane. "
            "For operational context, prefer get_status, get_runtime_summary, and get_operational_surface "
            "before requesting logs or healthchecks."
        )

    def tool_definitions(self) -> list[dict[str, Any]]:
        target_optional = self.profile.target_id is not None
        target_properties = {"target_id": {"type": "string", "description": "Managed target id. Defaults to the configured test hub."}}
        target_required = [] if target_optional else ["target_id"]
        return [
            {
                "name": "foundation",
                "description": "Read the AdaOS Root MCP foundation snapshot used by this bridge.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_architecture_catalog",
                "description": "Read the AdaOS architecture catalog through the AdaOSDevPlane descriptive surface.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_sdk_metadata",
                "description": "Read AdaOS SDK export metadata through the AdaOSDevPlane descriptive surface.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "string", "enum": ["mini", "std", "rich"], "default": "std"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_template_catalog",
                "description": "Read the root-curated skill and scenario template catalog through AdaOSDevPlane.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_public_skill_registry",
                "description": "Read the published workspace skill registry summary through AdaOSDevPlane.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_public_scenario_registry",
                "description": "Read the published workspace scenario registry summary through AdaOSDevPlane.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_profileops_status",
                "description": "Read root-published profiler status and latest session summary for the managed target.",
                "inputSchema": {"type": "object", "properties": target_properties, "required": target_required, "additionalProperties": False},
            },
            {
                "name": "list_profileops_sessions",
                "description": "List root-published profiler sessions for the managed target.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **target_properties,
                        "state": {"type": "string"},
                        "suspected_only": {"type": "boolean", "default": False},
                    },
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_profileops_session",
                "description": "Read one root-published profiler session for the managed target.",
                "inputSchema": {
                    "type": "object",
                    "properties": {**target_properties, "session_id": {"type": "string"}},
                    "required": [*target_required, "session_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_profileops_incidents",
                "description": "List suspected profiler incidents for the managed target.",
                "inputSchema": {"type": "object", "properties": target_properties, "required": target_required, "additionalProperties": False},
            },
            {
                "name": "list_profileops_artifacts",
                "description": "List root-published profiler artifacts for a profiler session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {**target_properties, "session_id": {"type": "string"}},
                    "required": [*target_required, "session_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_profileops_artifact",
                "description": "Read one root-published profiler artifact for a profiler session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **target_properties,
                        "session_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0, "default": 0},
                        "max_bytes": {"type": "integer", "minimum": 1, "default": 262144},
                    },
                    "required": [*target_required, "session_id", "artifact_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "start_profileops_session",
                "description": "Start a supervisor-owned profiler session through the bounded ProfileOps control surface.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **target_properties,
                        "profile_mode": {"type": "string", "default": "sampled_profile"},
                        "reason": {"type": "string"},
                        "trigger_source": {"type": "string", "default": "root_mcp"},
                    },
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "stop_profileops_session",
                "description": "Stop a supervisor-owned profiler session through the bounded ProfileOps control surface.",
                "inputSchema": {
                    "type": "object",
                    "properties": {**target_properties, "session_id": {"type": "string"}, "reason": {"type": "string"}},
                    "required": [*target_required, "session_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "retry_profileops_session",
                "description": "Retry a supervisor-owned profiler session through the bounded ProfileOps control surface.",
                "inputSchema": {
                    "type": "object",
                    "properties": {**target_properties, "session_id": {"type": "string"}, "reason": {"type": "string"}},
                    "required": [*target_required, "session_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "publish_profileops_session",
                "description": "Publish a supervisor-owned profiler session to root through the bounded ProfileOps control surface.",
                "inputSchema": {
                    "type": "object",
                    "properties": {**target_properties, "session_id": {"type": "string"}, "reason": {"type": "string"}},
                    "required": [*target_required, "session_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_managed_targets",
                "description": "List managed targets visible to the current Root MCP token scope.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "environment": {"type": "string", "description": "Optional environment filter such as test."},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_managed_target",
                "description": "Describe the configured managed target or another accessible target.",
                "inputSchema": {
                    "type": "object",
                    "properties": target_properties,
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_operational_surface",
                "description": "Inspect the published infra_access_skill surface, token management, and WebUI hints.",
                "inputSchema": {
                    "type": "object",
                    "properties": target_properties,
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_status",
                "description": "Read the current hub status, route, root-control, and deployment state.",
                "inputSchema": {
                    "type": "object",
                    "properties": target_properties,
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_runtime_summary",
                "description": "Read the current runtime summary published for the managed hub.",
                "inputSchema": {
                    "type": "object",
                    "properties": target_properties,
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_activity_log",
                "description": "Read recent Root MCP activity and control-report events for the target.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **target_properties,
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                        "errors_only": {"type": "boolean", "default": False},
                    },
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_capability_usage_summary",
                "description": "Read aggregated capability-usage counters for the target.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **target_properties,
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                    },
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_logs",
                "description": "Read bounded logs through infra_access_skill when the target exposes local_process execution.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **target_properties,
                        "tail": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                    },
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "run_healthchecks",
                "description": "Run bounded healthchecks through infra_access_skill when the target exposes local_process execution.",
                "inputSchema": {
                    "type": "object",
                    "properties": target_properties,
                    "required": target_required,
                    "additionalProperties": False,
                },
            },
            {
                "name": "recent_audit",
                "description": "Read recent Root MCP audit events for this bridge scope.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                        "tool_id": {"type": "string"},
                        "trace_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_yjs_load_mark_history",
                "description": "Read queryable YJS load-mark history captured beside adaos.log.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 100},
                        "webspace_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["owner", "root"]},
                        "bucket_id": {"type": "string"},
                        "display_contains": {"type": "string"},
                        "status": {"type": "string"},
                        "last_source": {"type": "string"},
                        "since_ts": {"type": "number"},
                        "until_ts": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_yjs_logs",
                "description": "Read bounded YJS log tails captured in the root-visible logs directory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                        "lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                        "contains": {"type": "string"},
                        "file": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_skill_logs",
                "description": "Read bounded skill service log tails captured in the root-visible logs directory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                        "lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                        "skill": {"type": "string"},
                        "contains": {"type": "string"},
                        "file": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_adaos_logs",
                "description": "Read bounded adaos.log tails captured in the root-visible logs directory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                        "lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                        "contains": {"type": "string"},
                        "file": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_events_logs",
                "description": "Read bounded events.log tails captured in the root-visible logs directory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                        "lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                        "contains": {"type": "string"},
                        "file": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_subnet_info",
                "description": "Read the currently scoped root-known subnet information and visible sessions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        client = self._client()
        tool = str(name or "").strip()
        if tool == "foundation":
            return _tool_text(client.foundation())
        if tool == "get_architecture_catalog":
            return _tool_text(client.get_adaos_dev_architecture_catalog())
        if tool == "get_sdk_metadata":
            return _tool_text(client.get_adaos_dev_sdk_metadata(level=str(args.get("level") or "std")))
        if tool == "get_template_catalog":
            return _tool_text(client.get_adaos_dev_template_catalog())
        if tool == "get_public_skill_registry":
            return _tool_text(client.get_adaos_dev_public_skill_registry())
        if tool == "get_public_scenario_registry":
            return _tool_text(client.get_adaos_dev_public_scenario_registry())
        if tool == "get_profileops_status":
            return _tool_text(client.get_profileops_status(self._effective_target_id(args)))
        if tool == "list_profileops_sessions":
            return _tool_text(
                client.list_profileops_sessions(
                    self._effective_target_id(args),
                    state=_normalize_text(args.get("state")),
                    suspected_only=bool(args.get("suspected_only")),
                )
            )
        if tool == "get_profileops_session":
            return _tool_text(client.get_profileops_session(self._effective_target_id(args), str(args.get("session_id") or "")))
        if tool == "list_profileops_incidents":
            return _tool_text(client.list_profileops_incidents(self._effective_target_id(args)))
        if tool == "list_profileops_artifacts":
            return _tool_text(client.list_profileops_artifacts(self._effective_target_id(args), str(args.get("session_id") or "")))
        if tool == "get_profileops_artifact":
            return _tool_text(
                client.get_profileops_artifact(
                    self._effective_target_id(args),
                    str(args.get("session_id") or ""),
                    str(args.get("artifact_id") or ""),
                    offset=int(args.get("offset") or 0),
                    max_bytes=int(args.get("max_bytes") or 256 * 1024),
                )
            )
        if tool == "start_profileops_session":
            return _tool_text(
                client.start_profileops_session(
                    self._effective_target_id(args),
                    profile_mode=str(args.get("profile_mode") or "sampled_profile"),
                    reason=str(args.get("reason") or "root_mcp.memory.start"),
                    trigger_source=str(args.get("trigger_source") or "root_mcp"),
                )
            )
        if tool == "stop_profileops_session":
            return _tool_text(
                client.stop_profileops_session(
                    self._effective_target_id(args),
                    str(args.get("session_id") or ""),
                    reason=str(args.get("reason") or "root_mcp.memory.stop"),
                )
            )
        if tool == "retry_profileops_session":
            return _tool_text(
                client.retry_profileops_session(
                    self._effective_target_id(args),
                    str(args.get("session_id") or ""),
                    reason=str(args.get("reason") or "root_mcp.memory.retry"),
                )
            )
        if tool == "publish_profileops_session":
            return _tool_text(
                client.publish_profileops_session(
                    self._effective_target_id(args),
                    str(args.get("session_id") or ""),
                    reason=str(args.get("reason") or "root_mcp.memory.publish"),
                )
            )
        if tool == "list_managed_targets":
            return _tool_text(client.list_managed_targets(environment=_normalize_text(args.get("environment"))))
        if tool == "get_managed_target":
            return _tool_text(client.get_managed_target(self._effective_target_id(args)))
        if tool == "get_operational_surface":
            return _tool_text(client.get_operational_surface(self._effective_target_id(args)))
        if tool == "get_status":
            return _tool_text(client.get_target_status(self._effective_target_id(args)))
        if tool == "get_runtime_summary":
            return _tool_text(client.get_target_runtime_summary(self._effective_target_id(args)))
        if tool == "get_activity_log":
            return _tool_text(
                client.get_target_activity_log(
                    self._effective_target_id(args),
                    limit=int(args.get("limit") or 50),
                    errors_only=bool(args.get("errors_only")),
                )
            )
        if tool == "get_capability_usage_summary":
            return _tool_text(
                client.get_target_capability_usage_summary(
                    self._effective_target_id(args),
                    limit=int(args.get("limit") or 200),
                )
            )
        if tool == "get_logs":
            return _tool_text(
                client.get_target_logs(
                    self._effective_target_id(args),
                    tail=int(args.get("tail") or 200),
                )
            )
        if tool == "run_healthchecks":
            return _tool_text(client.run_target_healthchecks(self._effective_target_id(args)))
        if tool == "recent_audit":
            return _tool_text(
                client.recent_audit(
                    limit=int(args.get("limit") or 50),
                    tool_id=_normalize_text(args.get("tool_id")),
                    trace_id=_normalize_text(args.get("trace_id")),
                    target_id=self.profile.target_id,
                    subnet_id=self.profile.subnet_id,
                )
            )
        if tool == "get_yjs_load_mark_history":
            return _tool_text(
                client.get_yjs_load_mark_history(
                    limit=int(args.get("limit") or 100),
                    webspace_id=_normalize_text(args.get("webspace_id")),
                    kind=_normalize_text(args.get("kind")),
                    bucket_id=_normalize_text(args.get("bucket_id")),
                    display_contains=_normalize_text(args.get("display_contains")),
                    status=_normalize_text(args.get("status")),
                    last_source=_normalize_text(args.get("last_source")),
                    since_ts=float(args["since_ts"]) if args.get("since_ts") is not None else None,
                    until_ts=float(args["until_ts"]) if args.get("until_ts") is not None else None,
                )
            )
        if tool == "get_yjs_logs":
            return _tool_text(
                client.get_yjs_logs(
                    limit=int(args.get("limit") or 5),
                    lines=int(args.get("lines") or 200),
                    contains=_normalize_text(args.get("contains")),
                    file=_normalize_text(args.get("file")),
                )
            )
        if tool == "get_skill_logs":
            return _tool_text(
                client.get_skill_logs(
                    limit=int(args.get("limit") or 5),
                    lines=int(args.get("lines") or 200),
                    skill=_normalize_text(args.get("skill")),
                    contains=_normalize_text(args.get("contains")),
                    file=_normalize_text(args.get("file")),
                )
            )
        if tool == "get_adaos_logs":
            return _tool_text(
                client.get_adaos_logs(
                    limit=int(args.get("limit") or 5),
                    lines=int(args.get("lines") or 200),
                    contains=_normalize_text(args.get("contains")),
                    file=_normalize_text(args.get("file")),
                )
            )
        if tool == "get_events_logs":
            return _tool_text(
                client.get_events_logs(
                    limit=int(args.get("limit") or 5),
                    lines=int(args.get("lines") or 200),
                    contains=_normalize_text(args.get("contains")),
                    file=_normalize_text(args.get("file")),
                )
            )
        if tool == "get_subnet_info":
            return _tool_text(client.get_subnet_info(target_id=_normalize_text(args.get("target_id"))))
        raise KeyError(tool)

    def handle_request(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        method = str(request.get("method") or "").strip()
        request_id = request.get("id")
        params = request.get("params")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.profile.server_name, "version": "0.1.0"},
                    "instructions": self.instructions(),
                }
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tool_definitions()}}
            if method == "tools/call":
                payload = dict(params or {})
                name = str(payload.get("name") or "").strip()
                arguments = payload.get("arguments")
                return {"jsonrpc": "2.0", "id": request_id, "result": self.call_tool(name, arguments)}
            if method.startswith("notifications/"):
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not supported: {method}"},
            }
        except Exception as exc:
            if method == "tools/call":
                error_payload = {
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "type": exc.__class__.__name__,
                    },
                }
                return {"jsonrpc": "2.0", "id": request_id, "result": _tool_text(error_payload, error=True)}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }


def _read_framed_message(stream: BinaryIO) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        try:
            key, value = line.decode("utf-8").split(":", 1)
        except ValueError:
            continue
        headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length") or "0")
    except ValueError:
        length = 0
    if length <= 0:
        return None
    body = stream.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _write_framed_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def serve_codex_stdio_bridge(profile: CodexBridgeProfile | None = None) -> None:
    bridge = CodexRootMcpBridge(profile or load_codex_bridge_profile())
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    while True:
        message = _read_framed_message(input_stream)
        if message is None:
            return
        response = bridge.handle_request(message)
        if response is not None:
            _write_framed_message(output_stream, response)


__all__ = [
    "CodexBridgeProfile",
    "CodexRootMcpBridge",
    "DEFAULT_CODEX_TARGET_CAPABILITIES",
    "build_codex_stdio_command",
    "default_profile_paths",
    "load_codex_bridge_profile",
    "serve_codex_stdio_bridge",
    "write_codex_bridge_profile",
]
