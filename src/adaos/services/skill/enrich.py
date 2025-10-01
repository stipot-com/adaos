"""Manifest enrichment utilities for skill runtime deployments."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import yaml

from adaos.services.skill.runtime_env import SkillSlotPaths


@dataclass(slots=True, frozen=True)
class PolicyDefaults:
    timeout_seconds: float
    retry_count: int
    telemetry_enabled: bool
    sandbox_memory_mb: int | None = None
    sandbox_cpu_seconds: float | None = None


def _load_manifest_file(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"manifest file not found: {path}")
    if path.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(skill_dir: Path) -> Mapping[str, Any]:
    """Load skill manifest in either JSON or YAML form."""

    for name in ("manifest.json", "skill.json", "manifest.yaml", "skill.yaml"):
        candidate = skill_dir / name
        if candidate.exists():
            return _load_manifest_file(candidate)
    raise FileNotFoundError("skill manifest not found (manifest.json or skill.yaml)")


def _write_shim(slot: SkillSlotPaths, tool_name: str, manifest: Mapping[str, Any]) -> Path:
    bin_dir = slot.bin_dir
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim_path = bin_dir / f"{tool_name}.py"
    skill_module = manifest.get("runtime", {}).get("module")
    entry = None
    for tool in manifest.get("tools", []):
        if tool.get("name") == tool_name:
            entry = tool.get("entry")
            break
    if not entry:
        raise ValueError(f"tool '{tool_name}' is missing 'entry' in manifest")
    module_path, _, attr = entry.partition(":")
    if not attr:
        attr = "handle"

    skill_root = slot.root.parent.parent / slot.skill_name
    shim_template = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import pathlib\n"
        "import sys\n\n"
        "from adaos.skills.runtime_runner import execute_tool\n\n"
        "def main() -> int:\n"
        "    payload = sys.stdin.read() or \"{}\"\n"
        "    try:\n"
        "        data = json.loads(payload)\n"
        "    except json.JSONDecodeError as exc:\n"
        "        raise SystemExit(f'invalid payload: {exc}')\n"
        f"    skill_dir = pathlib.Path({repr(str(skill_root))})\n"
        f"    return execute_tool(skill_dir, module={repr(module_path or skill_module)}, attr={repr(attr)}, payload=data)\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )
    shim_path.write_text(shim_template, encoding="utf-8")
    os.chmod(shim_path, 0o755)
    return shim_path


def enrich_manifest(
    *,
    manifest: Mapping[str, Any],
    slot: SkillSlotPaths,
    interpreter: Path,
    defaults: PolicyDefaults,
    policy_overrides: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Produce a resolved manifest suitable for runtime execution."""

    policy_overrides = dict(policy_overrides or {})
    tools = {}
    manifests_tools = manifest.get("tools", [])
    for item in manifests_tools:
        tool_name = item.get("name")
        if not tool_name:
            continue
        shim_path = _write_shim(slot, tool_name, manifest)
        tool_payload = {
            "name": tool_name,
            "shim": str(shim_path),
            "command": [str(interpreter), str(shim_path)],
            "timeout_seconds": item.get("timeout", defaults.timeout_seconds),
            "retries": item.get("retries", defaults.retry_count),
            "schema": {
                "input": item.get("input_schema"),
                "output": item.get("output_schema"),
            },
            "permissions": item.get("permissions") or manifest.get("permissions"),
            "secrets": _preserve_secret_placeholders(item.get("secrets", [])),
        }
        tools[tool_name] = tool_payload

    resolved = {
        "name": manifest.get("name", slot.skill_name),
        "version": manifest.get("version"),
        "slot": slot.slot,
        "source": str((slot.root.parent.parent / slot.skill_name).resolve()),
        "runtime": {
            "type": manifest.get("runtime", {}).get("type", "python"),
            "interpreter": str(interpreter),
            "venv": str(slot.venv_dir),
            "env": str(slot.env_dir),
            "tmp": str(slot.tmp_dir),
        },
        "tools": tools,
        "policy": {
            "timeout_seconds": defaults.timeout_seconds,
            "retry_count": defaults.retry_count,
            "telemetry_enabled": defaults.telemetry_enabled,
            "sandbox_memory_mb": defaults.sandbox_memory_mb,
            "sandbox_cpu_seconds": defaults.sandbox_cpu_seconds,
        },
        "policy_overrides": policy_overrides,
        "secrets": _preserve_secret_placeholders(manifest.get("secrets", [])),
        "events": manifest.get("events"),
    }
    return resolved


def _preserve_secret_placeholders(values: Iterable[Any]) -> list[Any]:
    preserved: list[Any] = []
    for value in values or []:
        if isinstance(value, str) and not value.startswith("${secret:"):
            preserved.append(f"${{secret:{value}}}")
        else:
            preserved.append(value)
    return preserved


def write_resolved_manifest(slot: SkillSlotPaths, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = slot.resolved_manifest.with_suffix(".tmp")
    slot.resolved_manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, slot.resolved_manifest)

