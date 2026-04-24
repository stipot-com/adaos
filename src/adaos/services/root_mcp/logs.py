from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import requests

from adaos.services.agent_context import get_ctx

LOG_CATEGORIES: set[str] = {"adaos", "events", "yjs", "skills"}


def normalize_log_category(category: str) -> str:
    token = str(category or "").strip().lower()
    if token not in LOG_CATEGORIES:
        raise ValueError(f"unknown_log_category:{token or 'empty'}")
    return token


def root_logs_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.logs_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_directory():
    from adaos.services.registry.subnet_directory import get_directory

    return get_directory()


def _get_hub_link_manager():
    from adaos.services.subnet.link_manager import get_hub_link_manager

    return get_hub_link_manager()


def tail_text_lines(path: Path, *, max_lines: int) -> list[str]:
    if max_lines <= 0 or not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return []


def match_log_category(category: str, name: str, *, contains: str | None = None, skill: str | None = None) -> bool:
    token = str(name or "").strip()
    contains_token = str(contains or "").strip()
    if contains_token and contains_token not in token:
        return False
    if category == "adaos":
        return token == "adaos.log" or token.startswith("adaos.log.")
    if category == "events":
        return token == "events.log" or token.startswith("events.log.")
    if category == "yjs":
        return token == "yjs_load_mark.jsonl" or "yjs" in token
    if category == "skills":
        if token == "service.log" or not token.startswith("service.") or not token.endswith(".log"):
            return False
        skill_token = str(skill or "").strip()
        if not skill_token:
            return True
        return token == f"service.{skill_token}.log"
    return False


def list_local_logs(
    *,
    category: str,
    limit: int = 5,
    lines: int = 200,
    contains: str | None = None,
    skill: str | None = None,
    file: str | None = None,
    logs_dir: Path | None = None,
    source_mode: str = "root_local_logs_dir",
) -> dict[str, Any]:
    category_token = normalize_log_category(category)
    target_logs_dir = logs_dir or root_logs_dir()
    max_files = max(1, min(int(limit), 50))
    max_lines = max(1, min(int(lines), 2000))
    requested_file = str(file or "").strip().replace("\\", "/")
    items: list[dict[str, Any]] = []

    if requested_file:
        path = (target_logs_dir / requested_file).resolve()
        if target_logs_dir.resolve() not in [path, *path.parents]:
            return {
                "category": category_token,
                "source_mode": source_mode,
                "available": False,
                "error": "path_outside_logs_dir",
                "query": {"limit": max_files, "lines": max_lines, "contains": contains, "skill": skill, "file": requested_file},
                "items": [],
            }
        if path.exists() and path.is_file() and match_log_category(category_token, path.name, contains=contains, skill=skill):
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "rel": requested_file,
                    "size_bytes": int(stat.st_size),
                    "modified_at": float(stat.st_mtime),
                    "tail": tail_text_lines(path, max_lines=max_lines),
                }
            )
        return {
            "category": category_token,
            "source_mode": source_mode,
            "available": bool(items),
            "query": {"limit": max_files, "lines": max_lines, "contains": contains, "skill": skill, "file": requested_file},
            "items": items,
        }

    candidates: list[Path] = []
    for entry in target_logs_dir.iterdir():
        if not entry.is_file():
            continue
        if match_log_category(category_token, entry.name, contains=contains, skill=skill):
            candidates.append(entry)
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)
    for path in candidates[:max_files]:
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "rel": path.name,
                "size_bytes": int(stat.st_size),
                "modified_at": float(stat.st_mtime),
                "tail": tail_text_lines(path, max_lines=max_lines),
            }
        )
    return {
        "category": category_token,
        "source_mode": source_mode,
        "available": True,
        "query": {"limit": max_files, "lines": max_lines, "contains": contains, "skill": skill, "file": None},
        "items": items,
    }


def _member_log_url(base_url: str, category: str) -> str:
    return f"{str(base_url).rstrip('/')}/api/node/logs/{category}"


def _request_member_logs(
    *,
    base_url: str,
    category: str,
    token: str,
    limit: int,
    lines: int,
    contains: str | None,
    skill: str | None,
    file: str | None,
    timeout: float,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": int(limit), "lines": int(lines)}
    if contains:
        params["contains"] = str(contains)
    if skill:
        params["skill"] = str(skill)
    if file:
        params["file"] = str(file)
    response = requests.get(
        _member_log_url(base_url, category),
        headers={"X-AdaOS-Token": token},
        params=params,
        timeout=float(timeout),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("member_log_payload_invalid")
    return payload


async def aggregate_subnet_logs(
    *,
    category: str,
    subnet_id: str,
    limit: int = 5,
    lines: int = 200,
    contains: str | None = None,
    skill: str | None = None,
    file: str | None = None,
    include_hub: bool = True,
    timeout: float = 5.0,
) -> dict[str, Any]:
    category_token = normalize_log_category(category)
    effective_subnet_id = str(subnet_id or "").strip()
    if not effective_subnet_id:
        raise ValueError("subnet_id is required")
    ctx = get_ctx()
    conf = getattr(ctx, "config", None)
    current_node_id = str(getattr(conf, "node_id", None) or "").strip()
    current_subnet_id = str(getattr(conf, "subnet_id", None) or "").strip()
    current_role = str(getattr(conf, "role", None) or "").strip().lower()
    internal_token = str(getattr(conf, "token", None) or os.getenv("ADAOS_TOKEN") or "").strip()
    directory = _get_directory()
    known_nodes = [
        dict(item)
        for item in directory.list_known_nodes()
        if str(item.get("subnet_id") or "").strip() == effective_subnet_id
    ]
    link_snapshot = _get_hub_link_manager().snapshot()
    connected_members = {
        str(item.get("node_id") or "").strip(): dict(item)
        for item in list(link_snapshot.get("members") or [])
        if str(item.get("node_id") or "").strip()
    }
    aggregated_at = time.time()

    active_nodes: list[dict[str, Any]] = []
    for item in known_nodes:
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            continue
        connected = node_id in connected_members
        online = bool(item.get("online"))
        if online or connected:
            active_nodes.append(item)

    nodes: list[dict[str, Any]] = []
    if include_hub and current_role == "hub" and current_subnet_id == effective_subnet_id:
        nodes.append(
            {
                "node_id": current_node_id or "hub",
                "hostname": getattr(conf, "hostname", None),
                "roles": ["hub"],
                "online": True,
                "connected": True,
                "base_url": None,
                "source": "hub_local_logs_dir",
                "ok": True,
                "logs": list_local_logs(
                    category=category_token,
                    limit=limit,
                    lines=lines,
                    contains=contains,
                    skill=skill,
                    file=file,
                    source_mode="node_local_logs_dir",
                ),
            }
        )

    async def _collect_member(node: dict[str, Any]) -> dict[str, Any]:
        node_id = str(node.get("node_id") or "").strip()
        base_url = str(node.get("base_url") or "").strip()
        connected = node_id in connected_members
        payload: dict[str, Any] = {
            "node_id": node_id,
            "hostname": node.get("hostname"),
            "roles": list(node.get("roles") or []),
            "online": bool(node.get("online")),
            "connected": connected,
            "base_url": base_url or None,
            "runtime_projection": dict(node.get("runtime_projection") or {}) if isinstance(node.get("runtime_projection"), dict) else {},
            "source": "member_http_api",
        }
        if current_node_id and node_id == current_node_id:
            payload["ok"] = True
            payload["source"] = "hub_local_logs_dir"
            payload["logs"] = list_local_logs(
                category=category_token,
                limit=limit,
                lines=lines,
                contains=contains,
                skill=skill,
                file=file,
                source_mode="node_local_logs_dir",
            )
            return payload
        if not base_url:
            payload["ok"] = False
            payload["error"] = "member_base_url_missing"
            return payload
        if not internal_token:
            payload["ok"] = False
            payload["error"] = "internal_token_missing"
            return payload
        try:
            response = await asyncio.to_thread(
                _request_member_logs,
                base_url=base_url,
                category=category_token,
                token=internal_token,
                limit=limit,
                lines=lines,
                contains=contains,
                skill=skill,
                file=file,
                timeout=timeout,
            )
            payload["ok"] = True
            payload["logs"] = response.get("logs") if isinstance(response.get("logs"), dict) else response
            return payload
        except Exception as exc:
            payload["ok"] = False
            payload["error"] = f"{type(exc).__name__}: {exc}"
            return payload

    member_nodes = [item for item in active_nodes if str(item.get("node_id") or "").strip() != current_node_id]
    if member_nodes:
        nodes.extend(await asyncio.gather(*[_collect_member(item) for item in member_nodes]))

    ok_total = sum(1 for item in nodes if bool(item.get("ok")))
    return {
        "category": category_token,
        "source_mode": "hub_active_subnet_nodes",
        "available": bool(nodes),
        "subnet_id": effective_subnet_id,
        "query": {
            "limit": max(1, min(int(limit), 50)),
            "lines": max(1, min(int(lines), 2000)),
            "contains": contains,
            "skill": skill,
            "file": file,
            "include_hub": bool(include_hub),
            "scope": "subnet_active",
        },
        "aggregation": {
            "aggregated_at": aggregated_at,
            "known_total": len(known_nodes),
            "active_total": len(active_nodes),
            "connected_total": len(connected_members),
            "included_total": len(nodes),
            "ok_total": ok_total,
            "error_total": max(0, len(nodes) - ok_total),
        },
        "nodes": nodes,
    }
