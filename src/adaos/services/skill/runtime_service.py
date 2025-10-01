"""High level service implementing the skill runtime lifecycle."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from adaos.services.agent_context import AgentContext
from adaos.services.skill.enrich import (
    PolicyDefaults,
    enrich_manifest,
    load_manifest,
    write_resolved_manifest,
)
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment, SkillSlotPaths
from adaos.services.skill.tests_runner import TestResult, run_tests


@dataclass(slots=True)
class InstallResult:
    skill: str
    version: str
    slot: str
    resolved_manifest: Path
    tests: Dict[str, TestResult]


class SkillRuntimeService:
    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Install pipeline
    # ------------------------------------------------------------------
    def install(
        self,
        name: str,
        *,
        version_override: Optional[str] = None,
        run_tests: bool = False,
        preferred_slot: Optional[str] = None,
    ) -> InstallResult:
        skills_root = Path(self.ctx.paths.skills_dir())
        skill_dir = skills_root / name
        if not skill_dir.exists():
            template_dir = Path(self.ctx.paths.package_dir) / ".." / ".." / ".adaos" / "skills" / name
            template_dir = template_dir.resolve()
            if template_dir.exists():
                import shutil

                shutil.copytree(template_dir, skill_dir)
            else:
                raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        manifest = load_manifest(skill_dir)
        version = version_override or str(manifest.get("version") or "0.0.0")
        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=name)
        env.prepare_version(version)

        slot_name = preferred_slot or env.select_inactive_slot(version)
        slot = env.build_slot_paths(version, slot_name)

        # clean slot before install
        env.cleanup_slot(version, slot_name)
        env.prepare_version(version)
        slot = env.build_slot_paths(version, slot_name)

        self._ensure_runtime_env(slot, manifest)
        interpreter = self._resolve_interpreter(slot)
        defaults = self._policy_defaults()

        policy_overrides = {
            "profile": self.ctx.settings.profile,
            "default_wall_time_sec": self.ctx.settings.default_wall_time_sec,
            "default_cpu_time_sec": self.ctx.settings.default_cpu_time_sec,
            "default_max_rss_mb": self.ctx.settings.default_max_rss_mb,
        }

        resolved = enrich_manifest(
            manifest=manifest,
            slot=slot,
            interpreter=interpreter,
            defaults=defaults,
            policy_overrides=policy_overrides,
        )

        tests = {}
        if run_tests:
            log_file = slot.logs_dir / "tests.log"
            tests = run_tests(skill_dir, log_path=log_file)
            if any(t.status != "passed" for t in tests.values()):
                env.cleanup_slot(version, slot_name)
                raise RuntimeError("skill tests failed")

        write_resolved_manifest(slot, resolved)

        metadata = env.read_version_metadata(version)
        metadata.setdefault("slots", {})[slot_name] = {
            "resolved_manifest": str(slot.resolved_manifest),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "tests": {name: result.status for name, result in tests.items()},
        }
        metadata["version"] = version
        metadata.setdefault("history", {})["last_install_slot"] = slot_name
        env.write_version_metadata(version, metadata)

        return InstallResult(
            skill=name,
            version=version,
            slot=slot_name,
            resolved_manifest=slot.resolved_manifest,
            tests=tests,
        )

    def _resolve_interpreter(self, slot: SkillSlotPaths) -> Path:
        python = slot.venv_dir / "bin" / "python"
        if os.name == "nt":
            python = slot.venv_dir / "Scripts" / "python.exe"
        if python.exists():
            return python
        import sys

        return Path(sys.executable)

    def _ensure_runtime_env(self, slot: SkillSlotPaths, manifest: dict) -> None:
        runtime = (manifest.get("runtime") or {}).get("type", "python").lower()
        if runtime == "python":
            self._ensure_python_env(slot)

    def _ensure_python_env(self, slot: SkillSlotPaths) -> None:
        if slot.venv_dir.exists() and any(slot.venv_dir.iterdir()):
            return
        import venv

        builder = venv.EnvBuilder(with_pip=False, clear=True)
        builder.create(str(slot.venv_dir))

    def _policy_defaults(self) -> PolicyDefaults:
        settings = self.ctx.settings
        return PolicyDefaults(
            timeout_seconds=settings.default_wall_time_sec,
            retry_count=1,
            telemetry_enabled=True,
            sandbox_memory_mb=settings.default_max_rss_mb,
            sandbox_cpu_seconds=settings.default_cpu_time_sec,
        )

    # ------------------------------------------------------------------
    # Activation / rollback
    # ------------------------------------------------------------------
    def activate(self, name: str, *, version: Optional[str] = None, slot: Optional[str] = None) -> str:
        env = SkillRuntimeEnvironment(skills_root=Path(self.ctx.paths.skills_dir()), skill_name=name)
        target_version = version or env.resolve_active_version()
        if not target_version:
            raise RuntimeError("no installed versions")
        env.prepare_version(target_version)
        active_slot = slot or env.select_inactive_slot(target_version)
        env.set_active_slot(target_version, active_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
        return active_slot

    def rollback(self, name: str) -> str:
        env = SkillRuntimeEnvironment(skills_root=Path(self.ctx.paths.skills_dir()), skill_name=name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        slot = env.rollback_slot(version)
        return slot

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    def status(self, name: str) -> Dict[str, Any]:
        env = SkillRuntimeEnvironment(skills_root=Path(self.ctx.paths.skills_dir()), skill_name=name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no versions installed")
        env.prepare_version(version)
        active_slot = env.read_active_slot(version)
        metadata = env.read_version_metadata(version)
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        resolved_path = Path(slot_meta.get("resolved_manifest") or env.build_slot_paths(version, active_slot).resolved_manifest)
        tests = slot_meta.get("tests", {})
        return {
            "name": name,
            "version": version,
            "active_slot": active_slot,
            "resolved_manifest": str(resolved_path),
            "tests": tests,
            "history": metadata.get("history", {}),
        }

    def uninstall(self, name: str, *, purge_data: bool = False) -> None:
        env = SkillRuntimeEnvironment(skills_root=Path(self.ctx.paths.skills_dir()), skill_name=name)
        for version in env.list_versions():
            for slot in ("A", "B"):
                env.cleanup_slot(version, slot)
            version_root = env.version_root(version)
            if version_root.exists():
                self._remove_tree(version_root)
        if purge_data and env.data_root().exists():
            for child in env.data_root().iterdir():
                if child.is_dir():
                    self._remove_tree(child)
                else:
                    child.unlink()
        marker = env.active_version_marker()
        if marker.exists():
            marker.unlink()
        runtime_root = env.runtime_root
        if runtime_root.exists():
            try:
                runtime_root.rmdir()
            except OSError:
                pass

    def _remove_tree(self, path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                self._remove_tree(child)
            else:
                child.unlink()
        path.rmdir()

