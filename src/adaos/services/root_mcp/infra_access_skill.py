from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from adaos.services.agent_context import get_ctx


_SKILL_NAME = "infra_access_skill"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _normalize_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for item in str(raw).split(","):
        token = item.strip()
        if token and token not in out:
            out.append(token)
    return out


def _parse_bool(raw: str | None, *, default: bool = False) -> bool:
    token = str(raw or "").strip().lower()
    if not token:
        return default
    return token in {"1", "true", "yes", "on"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _candidate_skill_dirs() -> list[Path]:
    candidates: list[Path] = []
    try:
        ctx = get_ctx()
        for attr in ("skills_dir", "skills_workspace_dir"):
            raw = getattr(ctx.paths, attr, None)
            if raw is None:
                continue
            base = raw() if callable(raw) else raw
            if not base:
                continue
            candidates.append(Path(base) / _SKILL_NAME)
    except Exception:
        pass

    repo_candidate = _repo_root() / ".adaos" / "workspace" / "skills" / _SKILL_NAME
    candidates.append(repo_candidate)

    deduped: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        try:
            token = str(item.resolve())
        except Exception:
            token = str(item)
        if token in seen:
            continue
        seen.add(token)
        deduped.append(item)
    return deduped


def resolve_skill_dir() -> Path | None:
    for candidate in _candidate_skill_dirs():
        if candidate.exists():
            return candidate
    return None


def skill_state() -> dict[str, Any]:
    skill_dir = resolve_skill_dir()
    available = skill_dir is not None and skill_dir.exists()
    if not available or skill_dir is None:
        return {
            "available": False,
            "installed": False,
            "skill_name": _SKILL_NAME,
            "availability": "missing",
            "manifest": {},
            "config": {},
            "webui": {"available": False, "app_ids": [], "modal_ids": [], "widget_ids": []},
        }

    manifest = _read_yaml(skill_dir / "skill.yaml")
    config = _read_json(skill_dir / "config.json")
    webui_raw = _read_json(skill_dir / "webui.json") if (skill_dir / "webui.json").exists() else {}
    registry = dict(webui_raw.get("registry") or {}) if isinstance(webui_raw.get("registry"), dict) else {}
    modals = dict(registry.get("modals") or {}) if isinstance(registry.get("modals"), dict) else {}
    widget_registry = dict(registry.get("widgets") or {}) if isinstance(registry.get("widgets"), dict) else {}
    apps = webui_raw.get("apps") if isinstance(webui_raw.get("apps"), list) else []
    widgets = webui_raw.get("widgets") if isinstance(webui_raw.get("widgets"), list) else []

    return {
        "available": True,
        "installed": True,
        "skill_name": str(manifest.get("name") or _SKILL_NAME),
        "availability": "installed",
        "path": str(skill_dir),
        "manifest": manifest,
        "config": config,
        "webui": {
            "available": bool(webui_raw),
            "app_ids": [str(item.get("id") or "").strip() for item in apps if isinstance(item, dict) and str(item.get("id") or "").strip()],
            "modal_ids": [str(key).strip() for key in modals.keys() if str(key).strip()],
            "widget_ids": [
                str(item.get("id") or "").strip()
                for item in widgets
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ],
            "widget_registry_ids": [str(key).strip() for key in widget_registry.keys() if str(key).strip()],
        },
    }


def build_operational_surface() -> dict[str, Any]:
    state = skill_state()
    config = dict(state.get("config") or {})
    manifest = dict(state.get("manifest") or {})
    webui = dict(state.get("webui") or {})

    configured_caps = _normalize_str_list(config.get("capabilities"))
    env_caps = _parse_csv(os.getenv("ADAOS_INFRA_ACCESS_CAPABILITIES"))
    capabilities = env_caps or configured_caps

    token_management = dict(config.get("token_management") or {}) if isinstance(config.get("token_management"), dict) else {}
    token_management_enabled = bool(token_management.get("enabled", bool(state.get("available"))))
    if token_management_enabled and "hub.issue_access_token" not in capabilities:
        capabilities.append("hub.issue_access_token")

    enabled_default = bool(config.get("enabled", bool(state.get("available"))))
    enabled = _parse_bool(os.getenv("ADAOS_INFRA_ACCESS_SKILL_ENABLED"), default=enabled_default)

    configured_mode = str(config.get("execution_mode") or "").strip().lower()
    execution_mode = str(os.getenv("ADAOS_INFRA_ACCESS_EXECUTION_MODE") or configured_mode or "reported_only").strip().lower()
    execution_adapter = "infra_access.local_process" if execution_mode == "local_process" else "report_only"

    configured_services = _normalize_str_list(config.get("allowed_services"))
    configured_tests = _normalize_str_list(config.get("allowed_test_paths"))
    allowed_services = _parse_csv(os.getenv("ADAOS_INFRA_ACCESS_ALLOWED_SERVICES")) or configured_services
    allowed_test_paths = _parse_csv(os.getenv("ADAOS_INFRA_ACCESS_ALLOWED_TEST_PATHS")) or configured_tests

    observability = dict(config.get("observability") or {}) if isinstance(config.get("observability"), dict) else {}
    observability_enabled = bool(observability.get("enabled", True))
    observability_channels = _normalize_str_list(observability.get("channels")) or [
        "root_mcp.audit",
        "hub.control_report",
    ]

    availability = "enabled" if enabled else ("installed" if state.get("available") else "missing")
    return {
        "published_by": "skill:infra_access_skill",
        "enabled": enabled,
        "availability": availability,
        "capabilities": capabilities,
        "execution_mode": execution_mode,
        "execution_adapter": execution_adapter,
        "allowed_services": allowed_services,
        "allowed_test_paths": allowed_test_paths,
        "skill": {
            "name": str(state.get("skill_name") or _SKILL_NAME),
            "version": str(manifest.get("version") or "").strip() or None,
            "entry": str(manifest.get("entry") or "").strip() or None,
            "description": str(manifest.get("description") or "").strip() or None,
            "available": bool(state.get("available")),
        },
        "webui": {
            "available": bool(webui.get("available")),
            "app_ids": list(webui.get("app_ids") or []),
            "modal_ids": list(webui.get("modal_ids") or []),
            "widget_ids": list(webui.get("widget_ids") or []),
            "widget_registry_ids": list(webui.get("widget_registry_ids") or []),
        },
        "observability": {
            "enabled": observability_enabled,
            "channels": observability_channels,
            "request_audit": True,
            "history_mode": "root_audit_plus_control_reports",
        },
        "token_management": {
            "enabled": token_management_enabled,
            "issuer_mode": str(token_management.get("issuer_mode") or "root_mcp").strip() or "root_mcp",
            "web_client_ready": bool(token_management_enabled and webui.get("available")),
        },
    }


__all__ = ["build_operational_surface", "resolve_skill_dir", "skill_state"]
