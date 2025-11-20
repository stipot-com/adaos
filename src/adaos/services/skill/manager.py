# src\adaos\services\skill\manager.py
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import yaml

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, SkillRepository, SkillRegistry
from adaos.ports.paths import PathProvider
from adaos.services.eventbus import emit
from adaos.ports import Capabilities
from adaos.services.fs.safe_io import remove_tree
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.services.git.workspace_guard import ensure_clean
from adaos.services.settings import Settings
from adaos.services.agent_context import AgentContext, get_ctx, use_ctx
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment, SkillSlotPaths
from adaos.services.skill.tests_runner import TestResult, run_tests
from adaos.skills.runtime_runner import execute_tool
from adaos.services.skill.validation import SkillValidationService, ValidationReport
from adaos.services.crypto.secrets_service import SecretsService
from adaos.services.skill.secrets_backend import SkillSecretsBackend
from adaos.services.skill.resolver import SkillPathResolver
from adaos.services.capacity import install_skill_in_capacity, uninstall_skill_from_capacity
from adaos.apps.yjs.webspace import default_webspace_id

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


@dataclass(slots=True)
class RuntimeInstallResult:
    name: str
    version: str
    slot: str
    resolved_manifest: Path
    tests: Dict[str, TestResult]


@dataclass(slots=True, frozen=True)
class PolicyDefaults:
    timeout_seconds: float
    retry_count: int
    telemetry_enabled: bool
    sandbox_memory_mb: int | None = None
    sandbox_cpu_seconds: float | None = None


class SkillManager:
    def __init__(
        self,
        *,
        git: GitClient,  # Deprecated. TODO Move to ctx
        paths: PathProvider,  # Deprecated. TODO Move to ctx
        caps: Capabilities,
        settings: Settings | None = None,
        registry: SkillRegistry = None,
        repo: SkillRepository | None = None,  # Deprecated. TODO Move to ctx
        bus: EventBus | None = None,
    ):
        self.reg = registry
        self.bus = bus
        self.caps = caps
        self.settings = settings
        self.ctx: AgentContext = get_ctx()

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "skills.manage")
        return self.ctx.skills_repo.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "skills.manage")
        self.ctx.skills_repo.ensure()
        return self.ctx.skills_repo.list()

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        return self.ctx.skills_repo.get(skill_id)

    def sync(self) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        self.ctx.skills_repo.ensure()
        root = self.ctx.paths.workspace_dir()
        names = [r.name for r in self.reg.list()]
        prefixed = [f"skills/{n}" for n in names]
        ensure_clean(self.ctx.git, str(root), prefixed)
        self.ctx.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.ctx.git.sparse_set(str(root), prefixed, no_cone=True)
        self.ctx.git.pull(str(root))
        emit(self.bus, "skill.sync", {"count": len(names)}, "skill.mgr")

    def install(
        self,
        name: str,
        pin: str | None = None,
        validate: bool = True,
        strict: bool = True,
        probe_tools: bool = False,
    ) -> tuple[SkillMeta, Optional[object]]:
        """
        Возвращает (meta, report|None). При strict и ошибках валидации можно выбрасывать исключение.
        """
        self.caps.require("core", "skills.manage")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")

        # 1) регистрируем (идемпотентно)
        self.reg.register(name, pin=pin)
        # 2) в тестах/без .git — только реестр
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if test_mode:
            return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
        # 3) mono-only установка через репозиторий (sparse-add + pull)
        meta = self.ctx.skills_repo.install(name, branch=None)
        """ if not validate:
            return meta, None """
        report = SkillValidationService(self.ctx).validate(meta.id.value, strict=strict, probe_tools=probe_tools)
        if strict and not report.ok:
            # опционально можно откатывать установку:
            # self.ctx.skills_repo.uninstall(meta.id.value)
            # и/или пробрасывать исключение
            return meta, report

        return meta, report  # return f"installed: {name}"

    def validate_skill(
        self,
        name: str,
        *,
        strict: bool = True,
        probe_tools: bool = False,
        source: str = "workspace",  # "dev" | "workspace" | "installed" (строка для простоты)
        path: Path | None = None,  # явный путь имеет приоритет
    ) -> ValidationReport:
        """Run validation for a skill via the service layer."""

        self.caps.require("core", "skills.manage")
        ctx: AgentContext = self.ctx
        previous = ctx.skill_ctx.get()
        try:
            svc = SkillValidationService(ctx)

            if path is None:
                # собираем резолвер из путей контекста
                resolver = SkillPathResolver(
                    dev_root=ctx.paths.dev_skills_dir(),
                    workspace_root=ctx.paths.skills_workspace_dir(),
                )
                root_path = resolver.resolve(name, space=source)  # FileNotFoundError bubbling up -> handled by caller
            else:
                root_path = Path(path).resolve()

            report = svc.validate_path(
                root_path,
                name=name,
                strict=strict,
                probe_tools=probe_tools,
            )
        finally:
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
        return report

    def run_skill_tests(
        self,
        name: str,
        *,
        source: str = "workspace",  # "dev" | "workspace" | "installed"
        path: Path | None = None,  # явный путь имеет приоритет
    ) -> Dict[str, TestResult]:
        """Execute runtime tests without preparing a new slot.
        Location-agnostic via resolver: dev/workspace/installed or explicit path.
        NOTE: semantics unchanged — tests rely on installed versions/slots.
        """

        # 1) resolve skill_dir via explicit path or resolver (space)
        if path is not None:
            skill_dir = Path(path).resolve()
            if not skill_dir.exists() or not skill_dir.is_dir():
                raise FileNotFoundError(f"skill path not found or not a directory: {skill_dir}")
        else:
            from .resolver import SkillPathResolver

            resolver = SkillPathResolver(
                dev_root=self.ctx.paths.dev_skills_dir(),
                workspace_root=self.ctx.paths.skills_workspace_dir(),
            )
            skill_dir = resolver.resolve(name, space=source if source in ("dev", "workspace") else "workspace")

        # 2) derive skills_root as parent folder that contains this skill directory
        skills_root = skill_dir.parent

        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=name)
        version = env.resolve_active_version()
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        package_root = Path(package_dir).resolve().parent if package_dir else None

        interpreter: Path | None = None
        python_paths: list[str] = []
        skill_source = skill_dir
        skill_env_path: Path | None = None
        log_path: Path

        if not version:
            raise RuntimeError("no versions installed")

        env.prepare_version(version)
        current_link = env.ensure_current_link(version)
        metadata = env.read_version_metadata(version)
        active_slot = env.read_active_slot(version)
        slot_paths = env.build_slot_paths(version, active_slot)
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        manifest_override = slot_meta.get("resolved_manifest") if isinstance(slot_meta, dict) else None
        manifest_path = Path(manifest_override or slot_paths.resolved_manifest)
        if not manifest_path.exists():
            for candidate in env.iter_slot_paths(version):
                candidate_meta = metadata.get("slots", {}).get(candidate.slot, {})
                override = candidate_meta.get("resolved_manifest") if isinstance(candidate_meta, dict) else None
                candidate_manifest = Path(override or candidate.resolved_manifest)
                if candidate_manifest.exists():
                    slot_paths = candidate
                    manifest_path = candidate_manifest
                    break

        if not manifest_path.exists():
            raise RuntimeError("no prepared slot with resolved manifest; install the skill first")

        log_path = slot_paths.logs_dir / "tests.manual.log"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}

        runtime_info = manifest.get("runtime", {})
        interpreter_value = runtime_info.get("interpreter")
        if interpreter_value:
            interpreter = Path(interpreter_value)
        python_paths.extend([p for p in runtime_info.get("python_paths", []) if p])

        source_override = manifest.get("source")
        skill_source = Path(source_override) if source_override else slot_paths.src_dir

        skill_env_raw = runtime_info.get("skill_env")
        if skill_env_raw:
            skill_env_path = Path(skill_env_raw)
        if not skill_env_path:
            skill_env_path = slot_paths.skill_env_path

        if package_root:
            python_paths.append(str(package_root))

        # Include dev/workspace convenience paths for compatibility with
        # existing skill tests that import via `skills.*` from the developer
        # workspace. This does not affect CLI test isolation which manages
        # its own PYTHONPATH.
        dev_dir = self.ctx.paths.dev_dir()
        python_paths.insert(0, str(skill_dir))
        python_paths.insert(0, str(dev_dir))

        results = run_tests(
            skill_source,
            log_path=log_path,
            interpreter=interpreter,
            python_paths=python_paths,
            skill_env_path=skill_env_path,
            skill_name=name,
            skill_version=version,
            slot_current_dir=current_link,
        )
        for test_name, result in list(results.items()):
            if result and result.status in ("error", "failed"):
                detail = f"{result.detail} (log: {log_path})" if result.detail else f"log: {log_path}"
                results[test_name] = replace(result, detail=detail)
        return results

    def uninstall(self, name: str) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")
        # если записи нет — считаем idempotent
        rec = self.reg.get(name)
        if not rec:
            return f"uninstalled: {name} (not found)"
        self.reg.unregister(name)
        root = self.ctx.paths.workspace_dir()
        # в тестах/без .git — только реестр, без git операций
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if test_mode or not (root / ".git").exists():
            suffix = " test-mode" if test_mode else ""
            return f"uninstalled: {name} (registry-only{suffix})"
        names = [r.name for r in self.reg.list()]
        prefixed = [f"skills/{n}" for n in names]
        ensure_clean(self.ctx.git, str(root), prefixed)
        self.ctx.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.ctx.git.sparse_set(str(root), prefixed, no_cone=True)
        self.ctx.git.pull(str(root))
        remove_error: Exception | None = None
        try:
            remove_tree(
                str(root / "skills" / name),
                fs=self.ctx.paths.ctx.fs if hasattr(self.ctx.paths, "ctx") else get_ctx().fs,
            )
        except PermissionError as exc:
            remove_error = exc
        self.cleanup_runtime(name, purge_data=True)
        if remove_error is not None:
            raise RuntimeError(f"не удалось удалить рабочую копию навыка '{name}'. Закройте файлы под " f"путем {(root / 'skills' / name)} и повторите попытку.") from remove_error
        emit(self.bus, "skill.uninstalled", {"id": name}, "skill.mgr")
        try:
            uninstall_skill_from_capacity(name)
            try:
                from adaos.services.node_config import load_config
                from adaos.services.capacity import get_local_capacity
                from adaos.services.registry.subnet_directory import get_directory
                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_skill_capacity(conf.node_id, cap.get("skills") or [])
            except Exception:
                pass
        except Exception:
            pass

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.caps.require("core", "skills.manage", "git.write", "net.git")
        root = self.ctx.paths.workspace_dir()
        if not (root / ".git").exists():
            raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")

        sub = name.strip()
        subpath = f"skills/{sub}"
        changed = self.ctx.git.changed_files(str(root), subpath=subpath)
        if not changed:
            return "nothing-to-push"
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        # безопасно получаем автора
        if self.settings:
            author_name = self.settings.git_author_name
            author_email = self.settings.git_author_email
        else:
            # fallback, если кто-то создаст менеджер без settings
            try:
                ctx = get_ctx()
                author_name = ctx.settings.git_author_name
                author_email = ctx.settings.git_author_email
            except Exception:
                author_name, author_email = "AdaOS Bot", "bot@adaos.local"
        msg = sanitize_message(message)
        sha = self.ctx.git.commit_subpath(
            str(root),
            subpath=subpath,
            message=msg,
            author_name=author_name,
            author_email=author_email,
            signoff=signoff,
        )
        if sha != "nothing-to-commit":
            self.ctx.git.push(str(root))
        return sha

    # ------------------------------------------------------------------
    # Runtime lifecycle helpers
    # ------------------------------------------------------------------
    def prepare_runtime(
        self,
        name: str,
        *,
        version_override: str | None = None,
        run_tests: bool = False,
        preferred_slot: str | None = None,
    ) -> RuntimeInstallResult:
        skills_root = self.ctx.paths.skills_dir()
        skill_dir = skills_root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        manifest = self._load_manifest(skill_dir)
        version = version_override or str(manifest.get("version") or "0.0.0")

        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=name)
        env.prepare_version(version)

        slot_name = preferred_slot or env.select_inactive_slot(version)
        slot = env.build_slot_paths(version, slot_name)

        # Ensure clean slot state before preparing runtime
        env.cleanup_slot(version, slot_name)
        env.prepare_version(version)
        slot = env.build_slot_paths(version, slot_name)

        try:
            staged_dir = self._stage_skill_sources(skill_dir, slot)
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise

        try:
            interpreter, python_paths = self._prepare_runtime_environment(
                env=env,
                slot=slot,
                manifest=manifest,
                skill_dir=staged_dir,
            )
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise
        defaults = self._policy_defaults()
        policy_overrides = self._policy_overrides()

        resolved = self._enrich_manifest(
            manifest=manifest,
            slot=slot,
            interpreter=interpreter,
            python_paths=python_paths,
            defaults=defaults,
            policy_overrides=policy_overrides,
            skill_dir=staged_dir,
        )

        tests: Dict[str, TestResult] = {}
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        package_root = None
        if package_dir:
            package_root = Path(package_dir).resolve().parent
        if run_tests:
            log_file = slot.logs_dir / "tests.log"
            extra_paths = list(python_paths)
            if package_root:
                extra_paths.append(str(package_root))
            tests = run_tests(
                staged_dir,
                log_path=log_file,
                interpreter=interpreter,
                python_paths=extra_paths,
                skill_env_path=slot.skill_env_path,
                skill_name=name,
                skill_version=version,
                slot_current_dir=slot.root,
            )
            if any(result.status != "passed" for result in tests.values()):
                env.cleanup_slot(version, slot_name)
                raise RuntimeError("skill tests failed")

        self._write_resolved_manifest(slot, resolved)

        metadata = env.read_version_metadata(version)
        slots_meta = metadata.setdefault("slots", {})
        slots_meta[slot_name] = {
            "resolved_manifest": str(slot.resolved_manifest),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "tests": {name: result.status for name, result in tests.items()},
        }
        metadata["version"] = version
        history = metadata.setdefault("history", {})
        history["last_install_slot"] = slot_name
        history["last_install_version"] = version
        history["last_install_at"] = datetime.now(timezone.utc).isoformat()
        history["last_default_tool"] = resolved.get("default_tool")
        env.write_version_metadata(version, metadata)

        return RuntimeInstallResult(
            name=name,
            version=version,
            slot=slot_name,
            resolved_manifest=slot.resolved_manifest,
            tests=tests,
        )

    def activate_runtime(self, name: str, *, version: str | None = None, slot: str | None = None) -> str:
        env = self._runtime_env(name)
        target_version = version or self._latest_prepared_version(env) or env.resolve_active_version()
        if not target_version:
            raise RuntimeError("no installed versions")
        env.prepare_version(target_version)
        metadata = env.read_version_metadata(target_version)
        target_slot = slot or self._preferred_activation_slot(env, target_version, metadata)
        slot_paths = env.build_slot_paths(target_version, target_slot)
        slot_meta = metadata.get("slots", {}).get(target_slot, {})
        manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
        if not manifest_path.exists():
            raise RuntimeError(f"slot {target_slot} of version {target_version} is not prepared; run 'adaos skill install {name} --slot={target_slot}' first")
        env.set_active_slot(target_version, target_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
        history = metadata.setdefault("history", {})
        history["last_active_slot"] = target_slot
        history["last_active_at"] = datetime.now(timezone.utc).isoformat()
        env.write_version_metadata(target_version, metadata)
        self._smoke_import(env=env, name=name, version=target_version)
        try:
            install_skill_in_capacity(name, target_version, active=True)
            try:
                from adaos.services.node_config import load_config
                from adaos.services.capacity import get_local_capacity
                from adaos.services.registry.subnet_directory import get_directory
                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_skill_capacity(conf.node_id, cap.get("skills") or [])
            except Exception:
                pass
        except Exception:
            pass
        return target_slot

    def rollback_runtime(self, name: str) -> str:
        env = self._runtime_env(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        return env.rollback_slot(version)

    def dev_rollback_runtime(self, name: str) -> str:
        env = self._runtime_env_dev(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        return env.rollback_slot(version)

    def activate_for_space(
        self,
        name: str,
        *,
        space: str = "default",
        webspace_id: str | None = None,
        version: str | None = None,
        slot: str | None = None,
    ) -> str:
        """
        Convenience helper that routes activation to the appropriate runtime
        (default vs dev) and emits a unified skills.activated event.
        """
        if space == "dev":
            target = self.activate_dev_runtime(name, version=version, slot=slot)
        else:
            target = self.activate_runtime(name, version=version, slot=slot)
        bus_webspace = webspace_id or default_webspace_id()
        if self.bus:
            payload: Dict[str, Any] = {"skill_name": name, "space": space, "webspace_id": bus_webspace}
            emit(self.bus, "skills.activated", payload, "skill.mgr")
        return target

    def rollback_for_space(self, name: str, *, space: str = "default", webspace_id: str | None = None) -> str:
        """
        Roll back the active runtime slot for the requested space and emit
        a skills.rolledback event for observers.
        """
        if space == "dev":
            target = self.dev_rollback_runtime(name)
        else:
            target = self.rollback_runtime(name)
        bus_webspace = webspace_id or default_webspace_id()
        if self.bus:
            payload: Dict[str, Any] = {"skill_name": name, "space": space, "webspace_id": bus_webspace}
            emit(self.bus, "skills.rolledback", payload, "skill.mgr")
        return target

    def runtime_status(self, name: str) -> Dict[str, Any]:
        env = self._runtime_env(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no versions installed")
        env.prepare_version(version)
        active_slot = env.read_active_slot(version)
        metadata = env.read_version_metadata(version)
        slot_paths = env.build_slot_paths(version, active_slot)
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        resolved_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
        ready = resolved_path.exists()
        history = metadata.get("history", {})
        state: Dict[str, Any] = {
            "name": name,
            "version": version,
            "active_slot": active_slot,
            "resolved_manifest": str(resolved_path),
            "ready": ready,
            "tests": slot_meta.get("tests", {}),
            "history": history,
        }
        if not ready:
            state["pending_slot"] = history.get("last_install_slot")
            state["pending_version"] = history.get("last_install_version")
            state["default_tool"] = history.get("last_default_tool")
        else:
            try:
                manifest = json.loads(resolved_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
            state["default_tool"] = manifest.get("default_tool")
        return state

    def dev_runtime_status(self, name: str) -> Dict[str, Any]:
        env = self._runtime_env_dev(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no versions installed")
        env.prepare_version(version)
        active_slot = env.read_active_slot(version)
        metadata = env.read_version_metadata(version)
        slot_paths = env.build_slot_paths(version, active_slot)
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        resolved_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
        ready = resolved_path.exists()
        history = metadata.get("history", {})
        state: Dict[str, Any] = {
            "name": name,
            "version": version,
            "active_slot": active_slot,
            "resolved_manifest": str(resolved_path),
            "ready": ready,
            "tests": slot_meta.get("tests", {}),
            "history": history,
        }
        if not ready:
            state["pending_slot"] = history.get("last_install_slot")
            state["pending_version"] = history.get("last_install_version")
            state["default_tool"] = history.get("last_default_tool")
        else:
            try:
                manifest = json.loads(resolved_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
            state["default_tool"] = manifest.get("default_tool")
        return state

    def cleanup_runtime(self, name: str, *, purge_data: bool = False) -> None:
        env = self._runtime_env(name)
        for version in env.list_versions():
            for slot in ("A", "B"):
                env.cleanup_slot(version, slot)
            version_root = env.version_root(version)
            if version_root.exists():
                self._remove_tree(version_root)
        if purge_data:
            data_root = env.data_root()
            if data_root.exists():
                self._remove_tree(data_root)
        marker = env.active_version_marker()
        if marker.exists():
            marker.unlink()
        runtime_root = env.runtime_root
        if runtime_root.exists():
            try:
                runtime_root.rmdir()
            except OSError:
                pass

    def gc_runtime(self, name: str | None = None) -> Dict[str, Iterable[str]]:
        skills_root = self.ctx.paths.skills_dir()
        targets = [name] if name else [p.name for p in (skills_root / ".runtime").glob("*") if p.is_dir()]
        cleaned: Dict[str, Iterable[str]] = {}
        for skill in targets:
            env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill)
            active_version = env.resolve_active_version()
            removed: list[str] = []
            for version in env.list_versions():
                if version == active_version:
                    continue
                for slot in ("A", "B"):
                    env.cleanup_slot(version, slot)
                self._remove_tree(env.version_root(version))
                removed.append(version)
            cleaned[skill] = removed
        return cleaned

    def doctor_runtime(self, name: str) -> Dict[str, Any]:
        status = self.runtime_status(name)
        ctx = self.ctx
        base = ctx.paths.skills_dir()
        return {
            "skill_root": str((base / name).resolve()),
            "runtime_root": str((base / ".runtime" / name).resolve()),
            "active_slot": status["active_slot"],
            "resolved_manifest": status["resolved_manifest"],
        }

    def setup_skill(self, name: str) -> Any:
        """Run the optional setup tool for a skill."""

        status = self.runtime_status(name)
        if not status.get("ready", True):
            pending_version = status.get("pending_version") or status.get("version")
            raise RuntimeError(f"skill '{name}' version {pending_version or '<unknown>'} is not activated. " "Run 'adaos skill activate' before setup.")

        manifest_path = Path(status["resolved_manifest"])
        if not manifest_path.exists():
            raise RuntimeError("skill runtime is not prepared; install and activate the skill first")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = manifest.get("tools") or {}
        if "setup" not in tools:
            raise RuntimeError("setup not supported for this skill")

        return self.run_tool(
            name,
            "setup",
            {},
            allow_inactive=False,
        )

    def dev_setup_skill(self, name: str) -> Any:
        """Run the optional setup tool for a DEV skill."""

        status = self.dev_runtime_status(name)
        if not status.get("ready", True):
            pending_version = status.get("pending_version") or status.get("version")
            raise RuntimeError(f"skill '{name}' version {pending_version or '<unknown>'} is not activated. Run 'adaos dev skill activate' before setup.")

        manifest_path = Path(status["resolved_manifest"])
        if not manifest_path.exists():
            raise RuntimeError("skill runtime is not prepared; install and activate the skill first")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = manifest.get("tools") or {}
        if "setup" not in tools:
            raise RuntimeError("setup not supported for this skill")

        return self.run_dev_tool(
            name,
            "setup",
            {},
            allow_inactive=False,
        )

    def run_tool(
        self,
        name: str,
        tool: str | None,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        allow_inactive: bool = False,
        slot: str | None = None,
    ) -> Any:
        status = self.runtime_status(name)
        env = self._runtime_env(name)
        version = status.get("version")
        active_slot = status.get("active_slot")
        manifest_path = Path(status["resolved_manifest"])
        slot_name = active_slot

        if not status.get("ready", True):
            target_slot = slot or status.get("pending_slot")
            target_version = status.get("pending_version") or version
            if not allow_inactive or not target_slot or not target_version:
                raise RuntimeError(
                    f"skill '{name}' version {status.get('pending_version') or status.get('version')} is not activated. "
                    f"Activate slot {target_slot or status.get('active_slot')} and retry."
                )
            env.prepare_version(target_version)
            metadata = env.read_version_metadata(target_version)
            slot_paths = env.build_slot_paths(target_version, target_slot)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} for version {target_version} is not prepared")
            version = target_version
            slot_name = target_slot
        elif slot and slot != active_slot:
            env.prepare_version(version)
            metadata = env.read_version_metadata(version)
            slot_paths = env.build_slot_paths(version, slot)
            slot_meta = metadata.get("slots", {}).get(slot, {})
            candidate = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not candidate.exists():
                raise RuntimeError(f"slot {slot} for version {version} is not prepared")
            manifest_path = candidate
            slot_name = slot

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or {}
        if tool:
            target_tool = tool
        else:
            target_tool = data.get("default_tool")
        if not target_tool:
            raise KeyError("tool name not provided and no default tool defined")
        tool_spec = tools.get(target_tool)
        if not tool_spec:
            available = ", ".join(sorted(tools)) or "<none>"
            raise KeyError(f"tool '{target_tool}' not found (available: {available})")

        module = tool_spec.get("module")
        attr = tool_spec.get("callable") or target_tool
        skill_dir = Path(data.get("source") or (self.ctx.paths.skills_dir() / name))
        slot_name = data.get("slot") or slot_name
        slot = env.build_slot_paths(version or data.get("version"), slot_name)
        runtime_info = data.get("runtime", {})
        extra_paths = [Path(p) for p in runtime_info.get("python_paths", []) if p]
        skill_env_path = Path(runtime_info.get("skill_env") or slot.skill_env_path)

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_secrets = ctx.secrets
        ctx.secrets = SecretsService(SkillSecretsBackend(env.data_root() / "files" / "secrets.json"), ctx.caps)
        execution_timeout = timeout or tool_spec.get("timeout_seconds")

        def _call_tool() -> Any:
            with use_ctx(ctx):
                return execute_tool(
                    skill_dir,
                    module=module,
                    attr=attr,
                    payload=payload,
                    extra_paths=extra_paths,
                )

        try:
            if not ctx.skill_ctx.set(name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{name}'")
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(skill_env_path)

            if execution_timeout:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
                from contextvars import copy_context

                with ThreadPoolExecutor(max_workers=1) as pool:
                    ctxvars = copy_context()
                    future = pool.submit(lambda: ctxvars.run(_call_tool))
                    try:
                        result = future.result(timeout=execution_timeout)
                    except FuturesTimeoutError as exc:
                        future.cancel()
                        raise TimeoutError(f"tool '{target_tool}' timed out after {execution_timeout} seconds") from exc
            else:
                result = _call_tool()
        finally:
            ctx.secrets = prev_secrets
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env

        self._persist_skill_env(env, slot)
        return result

    def run_dev_tool(
        self,
        name: str,
        tool: str | None,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        allow_inactive: bool = False,
        slot: str | None = None,
    ) -> Any:
        status = self.dev_runtime_status(name)
        env = self._runtime_env_dev(name)
        version = status.get("version")
        active_slot = status.get("active_slot")
        manifest_path = Path(status["resolved_manifest"])
        slot_name = active_slot

        if not status.get("ready", True):
            target_slot = slot or status.get("pending_slot")
            target_version = status.get("pending_version") or version
            if not allow_inactive or not target_slot or not target_version:
                raise RuntimeError(
                    f"skill '{name}' version {status.get('pending_version') or status.get('version')} is not activated. "
                    f"Activate slot {target_slot or status.get('active_slot')} and retry."
                )
            env.prepare_version(target_version)
            metadata = env.read_version_metadata(target_version)
            slot_paths = env.build_slot_paths(target_version, target_slot)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} for version {target_version} is not prepared")
            version = target_version
            slot_name = target_slot
        elif slot and slot != active_slot:
            env.prepare_version(version)
            metadata = env.read_version_metadata(version)
            slot_paths = env.build_slot_paths(version, slot)
            slot_meta = metadata.get("slots", {}).get(slot, {})
            candidate = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not candidate.exists():
                raise RuntimeError(f"slot {slot} for version {version} is not prepared")
            manifest_path = candidate
            slot_name = slot

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or {}
        if tool:
            target_tool = tool
        else:
            target_tool = data.get("default_tool")
        if not target_tool:
            raise KeyError("tool name not provided and no default tool defined")
        tool_spec = tools.get(target_tool)
        if not tool_spec:
            available = ", ".join(sorted(tools)) or "<none>"
            raise KeyError(f"tool '{target_tool}' not found (available: {available})")

        module = tool_spec.get("module")
        attr = tool_spec.get("callable") or target_tool
        skill_dir = Path(data.get("source") or (self.ctx.paths.dev_skills_dir() / name))
        slot_name = data.get("slot") or slot_name
        slot = env.build_slot_paths(version or data.get("version"), slot_name)
        runtime_info = data.get("runtime", {})
        extra_paths = [Path(p) for p in runtime_info.get("python_paths", []) if p]
        skill_env_path = Path(runtime_info.get("skill_env") or slot.skill_env_path)

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_secrets = ctx.secrets
        ctx.secrets = SecretsService(SkillSecretsBackend(env.data_root() / "files" / "secrets.json"), ctx.caps)
        execution_timeout = timeout or tool_spec.get("timeout_seconds")

        def _call_tool() -> Any:
            with use_ctx(ctx):
                return execute_tool(
                    skill_dir,
                    module=module,
                    attr=attr,
                    payload=payload,
                    extra_paths=extra_paths,
                )

        try:
            if not ctx.skill_ctx.set(name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{name}'")
        except Exception:
            pass
        try:
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(skill_env_path)
            result = _call_tool()
        finally:
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env

        self._persist_skill_env(env, slot)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _runtime_env(self, name: str) -> SkillRuntimeEnvironment:
        return SkillRuntimeEnvironment(
            skills_root=self.ctx.paths.skills_dir(),
            skill_name=name,
        )

    def _runtime_env_dev(self, name: str) -> SkillRuntimeEnvironment:
        return SkillRuntimeEnvironment(
            skills_root=self.ctx.paths.dev_skills_dir(),
            skill_name=name,
        )

    def _load_manifest(self, skill_dir: Path) -> Dict[str, Any]:
        candidates = ["resolved.manifest.json", "skill.yaml", "manifest.yaml", "manifest.json", "skill.json"]
        for name in candidates:
            path = skill_dir / name
            if not path.exists():
                continue
            if path.suffix in {".yaml", ".yml"}:
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError("skill manifest not found")

    def _prepare_runtime_environment(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        manifest: Mapping[str, Any],
        skill_dir: Path,
    ) -> tuple[Path, list[str]]:
        runtime_cfg = manifest.get("runtime") or {}
        runtime_type = (runtime_cfg.get("type") or ("python" if "python" in runtime_cfg else "python")).lower()
        if runtime_type == "python":
            return self._prepare_python_runtime(
                env=env,
                slot=slot,
                manifest=manifest,
                runtime_cfg=runtime_cfg,
                skill_dir=skill_dir,
            )
        raise NotImplementedError(f"runtime type '{runtime_type}' is not supported")

    def _stage_skill_sources(self, source: Path, slot: SkillSlotPaths) -> Path:
        destination_root = slot.src_dir
        namespace_root = destination_root / "skills"
        target = namespace_root / source.name
        if destination_root.exists():
            self._remove_tree(destination_root)
        namespace_root.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo", ".runtime")
        shutil.copytree(source, target, ignore=ignore)
        package_init = target / "__init__.py"
        if not package_init.exists():
            package_init.write_text("", encoding="utf-8")
        handlers_dir = target / "handlers"
        handler_main = handlers_dir / "main.py"
        if not handler_main.exists():
            raise FileNotFoundError(f"handler entrypoint missing: {handler_main}")
        handlers_init = handlers_dir / "__init__.py"
        if not handlers_init.exists():
            handlers_init.write_text("from .main import handle  # noqa: F401\n", encoding="utf-8")
        return target

    def _smoke_import(self, *, env: SkillRuntimeEnvironment, name: str, version: str) -> None:
        module_name = f"skills.{name}.handlers.main"
        try:
            current_link = env.ensure_current_link(version)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise RuntimeError(f"failed to prepare slot link for {name}: {exc}") from exc

        src_path = current_link / "src"
        if not src_path.exists():
            raise RuntimeError(f"active slot for {name} lacks src directory: {src_path}")

        vendor_path = current_link / "vendor"

        original_sys_path = list(sys.path)
        try:
            suffixes = (
                f"/{name}/slots/current/src",
                f"/{name}/slots/A/src",
                f"/{name}/slots/B/src",
                f"/{name}/slots/current/vendor",
                f"/{name}/slots/A/vendor",
                f"/{name}/slots/B/vendor",
            )
            sys.path[:] = [entry for entry in sys.path if not any(entry.replace("\\", "/").endswith(suffix) for suffix in suffixes)]
            paths_to_add = []
            if vendor_path.is_dir():
                paths_to_add.append(str(vendor_path))
            paths_to_add.append(str(src_path))
            for candidate in reversed(paths_to_add):
                if candidate not in sys.path:
                    sys.path.insert(0, candidate)
            for mod in list(sys.modules.keys()):
                if mod == module_name or mod.startswith(f"skills.{name}."):
                    sys.modules.pop(mod, None)
            importlib.invalidate_caches()
            importlib.import_module(module_name)
        except Exception as exc:
            raise RuntimeError(f"failed to import handler module for {name}: {exc}") from exc
        finally:
            sys.path[:] = original_sys_path
            for mod in list(sys.modules.keys()):
                if mod == module_name or mod.startswith(f"skills.{name}."):
                    sys.modules.pop(mod, None)

    def _prepare_python_runtime(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        manifest: Mapping[str, Any],
        runtime_cfg: Mapping[str, Any],
        skill_dir: Path,
    ) -> tuple[Path, list[str]]:
        interpreter = Path(sys.executable)
        python_paths = self._install_python_dependencies(
            manifest=manifest,
            slot=slot,
            skill_dir=skill_dir,
        )
        self._sync_skill_env(env=env, skill_dir=skill_dir, slot=slot)
        return interpreter, python_paths

    def _install_python_dependencies(
        self,
        *,
        manifest: Mapping[str, Any],
        slot: SkillSlotPaths,
        skill_dir: Path,
    ) -> list[str]:
        requirements_file = skill_dir / "requirements.in"
        dependencies = self._collect_dependencies(manifest)
        python_args: list[str] = []
        if requirements_file.exists():
            python_args.extend(["-r", str(requirements_file)])
        if dependencies:
            python_args.extend(dependencies)

        if not python_args:
            return []

        constraints = self._constraints_file()
        base_cmd = [
            str(sys.executable),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
        ]
        if constraints:
            base_cmd.extend(["-c", str(constraints)])

        shared_cmd = [*base_cmd, *python_args]
        vendor_dir = slot.vendor_dir
        
        def _run(cmd: list[str]) -> tuple[bool, str]:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError as e:
                return False, str(e)
            ok = (p.returncode == 0)
            out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
            return ok, out

        # 1) Try pip in current interpreter; bootstrap pip if missing
        ok, out = _run(shared_cmd)
        if not ok and ("No module named pip" in out or "No module named pip" in out.replace("\r", "\n")):
            _run([str(sys.executable), "-m", "ensurepip", "--upgrade"])  # best-effort
            ok, out = _run(shared_cmd)
        if ok:
            # clean vendor if present
            if vendor_dir.exists():
                for child in vendor_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except FileNotFoundError:
                            pass
            return []

        # 2) Fallback: pip --target vendor (after ensurepip)
        vendor_dir.mkdir(parents=True, exist_ok=True)
        vendor_cmd = [
            *base_cmd,
            "--target",
            str(vendor_dir),
            "--no-warn-script-location",
            *python_args,
        ]
        ok2, out2 = _run(vendor_cmd)
        if not ok2 and ("No module named pip" in out2 or "No module named pip" in out2.replace("\r", "\n")):
            _run([str(sys.executable), "-m", "ensurepip", "--upgrade"])  # best-effort
            ok2, out2 = _run(vendor_cmd)
        if ok2:
            return [str(vendor_dir)]

        # 3) Last resort: try `uv pip install` (if available)
        uv_base = ["uv", "pip", "install", "--upgrade"]
        if constraints:
            uv_base.extend(["-c", str(constraints)])
        ok3, out3 = _run([*uv_base, *python_args])
        if ok3:
            # uv installs into environment; keep vendor clean
            if vendor_dir.exists():
                for child in vendor_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except FileNotFoundError:
                            pass
            return []

        # Try uv with --target vendor
        uv_vendor = [*uv_base, "--target", str(vendor_dir), "--no-warn-script-location", *python_args]
        ok4, out4 = _run(uv_vendor)
        if ok4:
            return [str(vendor_dir)]

        # Failed all strategies
        raise RuntimeError(
            f"failed to install dependencies for skill '{slot.skill_name}':\n"
            f"pip(shared) -> {out}\n"
            f"pip(target) -> {out2}\n"
            f"uv(shared) -> {out3}\n"
            f"uv(target) -> {out4}"
        )

    def _constraints_file(self) -> Path | None:
        candidates: list[Path] = []
        workspace = self.ctx.paths.workspace_dir()
        candidates.append(workspace / "constraints.txt")
        candidates.append(workspace / "requirements" / "constraints.txt")
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        if package_dir:
            package_root = Path(package_dir).resolve().parent
            candidates.append(package_root / "constraints.txt")
            candidates.append(package_root / "requirements" / "constraints.txt")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _collect_dependencies(self, manifest: Mapping[str, Any]) -> list[str]:
        deps = manifest.get("dependencies") or []
        runtime_cfg = manifest.get("runtime") or {}
        runtime_deps = runtime_cfg.get("dependencies") or []
        combined: list[str] = []
        for value in list(deps) + list(runtime_deps):
            if not value:
                continue
            if isinstance(value, str):
                combined.append(value)
            elif isinstance(value, Mapping) and "name" in value:
                version = value.get("version")
                combined.append(f"{value['name']}{version or ''}")
        return combined

    def _sync_skill_env(self, *, env: SkillRuntimeEnvironment, skill_dir: Path, slot: SkillSlotPaths) -> None:
        store_path = env.data_root() / "files" / ".skill_env.json"
        candidates = [store_path, skill_dir / ".skill_env.json"]
        target = slot.skill_env_path
        for candidate in candidates:
            if candidate.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                if candidate is not store_path:
                    store_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, store_path)
                break

    def _persist_skill_env(self, env: SkillRuntimeEnvironment, slot: SkillSlotPaths) -> None:
        source = slot.skill_env_path
        if not source.exists():
            return
        store = env.data_root() / "files"
        store.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, store / ".skill_env.json")

    def _latest_prepared_version(self, env: SkillRuntimeEnvironment) -> Optional[str]:
        latest_version: Optional[str] = None
        latest_time: Optional[datetime] = None
        for version in env.list_versions():
            metadata = env.read_version_metadata(version)
            history = metadata.get("history", {})
            stamp = history.get("last_install_at")
            if not stamp:
                continue
            try:
                ts = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if latest_time is None or ts > latest_time:
                latest_time = ts
                latest_version = version
        return latest_version

    def _preferred_activation_slot(
        self,
        env: SkillRuntimeEnvironment,
        version: str,
        metadata: Mapping[str, Any],
    ) -> str:
        history = metadata.get("history", {})
        preferred = history.get("last_install_slot")
        if preferred in {"A", "B"}:
            slot_paths = env.build_slot_paths(version, preferred)
            slot_meta = metadata.get("slots", {}).get(preferred, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if manifest_path.exists():
                return preferred
        return env.select_inactive_slot(version)

    def _policy_defaults(self) -> PolicyDefaults:
        settings = self.ctx.settings
        return PolicyDefaults(
            timeout_seconds=settings.default_wall_time_sec,
            retry_count=1,
            telemetry_enabled=True,
            sandbox_memory_mb=settings.default_max_rss_mb,
            sandbox_cpu_seconds=settings.default_cpu_time_sec,
        )

    def _policy_overrides(self) -> Dict[str, Any]:
        settings = self.ctx.settings
        return {
            "profile": getattr(settings, "profile", None),
            "default_wall_time_sec": getattr(settings, "default_wall_time_sec", None),
            "default_cpu_time_sec": getattr(settings, "default_cpu_time_sec", None),
            "default_max_rss_mb": getattr(settings, "default_max_rss_mb", None),
        }

    def _enrich_manifest(
        self,
        *,
        manifest: Mapping[str, Any],
        slot: SkillSlotPaths,
        interpreter: Path,
        python_paths: Iterable[str],
        defaults: PolicyDefaults,
        policy_overrides: Mapping[str, Any],
        skill_dir: Path,
    ) -> Dict[str, Any]:
        tools: Dict[str, Dict[str, Any]] = {}
        tool_entries = manifest.get("tools", []) or []
        default_tool = manifest.get("default_tool")
        for item in tool_entries:
            tool_name = item.get("name")
            if not tool_name:
                continue
            module_path, attr = self._resolve_tool_entry(tool_name, item, manifest)
            tools[tool_name] = {
                "name": tool_name,
                "module": module_path,
                "callable": attr,
                "timeout_seconds": item.get("timeout", defaults.timeout_seconds),
                "retries": item.get("retries", defaults.retry_count),
                "schema": {
                    "input": item.get("input_schema"),
                    "output": item.get("output_schema"),
                },
                "permissions": item.get("permissions") or manifest.get("permissions"),
                "secrets": self._preserve_secret_placeholders(item.get("secrets", [])),
            }

        if not default_tool and len(tools) == 1:
            default_tool = next(iter(tools))

        return {
            "name": manifest.get("name", slot.skill_name),
            "version": manifest.get("version"),
            "slot": slot.slot,
            "source": str(skill_dir.resolve()),
            "runtime": {
                "type": (manifest.get("runtime") or {}).get("type", "python"),
                "interpreter": str(interpreter),
                "src": str(slot.src_dir),
                "vendor": str(slot.vendor_dir),
                "runtime_dir": str(slot.runtime_dir),
                "logs": str(slot.logs_dir),
                "tmp": str(slot.tmp_dir),
                "tests": str(slot.tests_dir),
                "python_paths": list(python_paths),
                "skill_env": str(slot.skill_env_path),
            },
            "tools": tools,
            "default_tool": default_tool,
            "policy": {
                "timeout_seconds": defaults.timeout_seconds,
                "retry_count": defaults.retry_count,
                "telemetry_enabled": defaults.telemetry_enabled,
                "sandbox_memory_mb": defaults.sandbox_memory_mb,
                "sandbox_cpu_seconds": defaults.sandbox_cpu_seconds,
            },
            "policy_overrides": dict(policy_overrides),
            "secrets": self._preserve_secret_placeholders(manifest.get("secrets", [])),
            "events": manifest.get("events"),
            "slot_root": str(slot.root),
        }

    def _resolve_tool_entry(
        self,
        tool_name: str,
        item: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> tuple[str, str]:
        runtime_cfg = manifest.get("runtime") or {}
        entry = item.get("entry")
        if entry:
            module_path, _, attr = entry.partition(":")
            module_path = module_path or runtime_cfg.get("module") or "handlers.main"
            attr = attr or tool_name
            return module_path, attr
        module_path = runtime_cfg.get("module") or "handlers.main"
        return module_path, tool_name

    def _preserve_secret_placeholders(self, values: Iterable[Any]) -> list[Any]:
        preserved: list[Any] = []
        for value in values or []:
            if isinstance(value, str) and not value.startswith("${secret:"):
                preserved.append(f"${{secret:{value}}}")
            else:
                preserved.append(value)
        return preserved

    def _write_resolved_manifest(self, slot: SkillSlotPaths, payload: Mapping[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = slot.resolved_manifest.with_suffix(".tmp")
        slot.resolved_manifest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, slot.resolved_manifest)

    def _remove_tree(self, path: Path) -> None:
        if not path.exists():
            return
        for child in path.iterdir():
            if child.is_dir():
                self._remove_tree(child)
            else:
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass
        try:
            path.rmdir()
        except OSError:
            pass

    def prepare_dev_runtime(
        self,
        name: str,
        *,
        version_override: str | None = None,
        run_tests: bool = False,
        preferred_slot: str | None = None,
    ) -> RuntimeInstallResult:
        """Prepare a runtime for a DEV skill under .adaos/dev/<subnet>/skills.

        Mirrors prepare_runtime but uses the DEV skills root as the source and runtime root.
        """
        dev_root = self.ctx.paths.dev_skills_dir()
        skill_dir = dev_root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        try:
            manifest = self._load_manifest(skill_dir)
        except FileNotFoundError:
            manifest = {}
        version = version_override or str(manifest.get("version") or "dev")

        env = SkillRuntimeEnvironment(skills_root=dev_root, skill_name=name)
        env.prepare_version(version)

        slot_name = preferred_slot or env.select_inactive_slot(version)
        slot = env.build_slot_paths(version, slot_name)

        # Ensure clean slot state before preparing runtime
        env.cleanup_slot(version, slot_name)
        env.prepare_version(version)
        slot = env.build_slot_paths(version, slot_name)

        try:
            staged_dir = self._stage_skill_sources(skill_dir, slot)
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise

        try:
            interpreter, python_paths = self._prepare_runtime_environment(
                env=env,
                slot=slot,
                manifest=manifest,
                skill_dir=staged_dir,
            )
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise
        defaults = self._policy_defaults()
        policy_overrides = self._policy_overrides()

        resolved = self._enrich_manifest(
            manifest=manifest,
            slot=slot,
            interpreter=interpreter,
            python_paths=python_paths,
            defaults=defaults,
            policy_overrides=policy_overrides,
            skill_dir=staged_dir,
        )

        tests: Dict[str, TestResult] = {}
        if run_tests:
            log_file = slot.logs_dir / "tests.log"
            tests = run_tests(
                staged_dir,
                log_path=log_file,
                interpreter=interpreter,
                python_paths=python_paths,
                skill_env_path=slot.skill_env_path,
                skill_name=name,
                skill_version=version,
                slot_current_dir=slot.root,
            )
            if any(result.status != "passed" for result in tests.values()):
                env.cleanup_slot(version, slot_name)
                raise RuntimeError("skill tests failed")

        self._write_resolved_manifest(slot, resolved)

        metadata = env.read_version_metadata(version)
        slots_meta = metadata.setdefault("slots", {})
        slots_meta[slot_name] = {
            "resolved_manifest": str(slot.resolved_manifest),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "tests": {name: result.status for name, result in tests.items()},
        }
        metadata["version"] = version
        history = metadata.setdefault("history", {})
        history["last_install_slot"] = slot_name
        history["last_install_version"] = version
        history["last_install_at"] = datetime.now(timezone.utc).isoformat()
        history["last_default_tool"] = resolved.get("default_tool")
        env.write_version_metadata(version, metadata)

        return RuntimeInstallResult(
            name=name,
            version=version,
            slot=slot_name,
            resolved_manifest=slot.resolved_manifest,
            tests=tests,
        )

    def activate_dev_runtime(self, name: str, *, version: str | None = None, slot: str | None = None) -> str:
        """Activate a prepared DEV runtime (under .adaos/dev/<subnet>/skills).

        If the requested version/slot is not prepared yet, prepare from the DEV skill sources first.
        """
        env = self._runtime_env_dev(name)
        dev_root = self.ctx.paths.dev_skills_dir()
        skill_dir = dev_root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        target_version = version or env.resolve_active_version()
        if not target_version:
            # derive version from manifest, default to 'dev'
            try:
                manifest = self._load_manifest(skill_dir)
            except FileNotFoundError:
                manifest = {}
            target_version = str(manifest.get("version") or "dev")

        # Ensure version layout exists and slot is prepared
        env.prepare_version(target_version)
        metadata = env.read_version_metadata(target_version)
        target_slot = slot or self._preferred_activation_slot(env, target_version, metadata)
        slot_paths = env.build_slot_paths(target_version, target_slot)
        slot_meta = metadata.get("slots", {}).get(target_slot, {})
        manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
        if not manifest_path.exists():
            # prepare from DEV sources when missing
            self.prepare_dev_runtime(name, version_override=target_version, run_tests=False, preferred_slot=target_slot)
            metadata = env.read_version_metadata(target_version)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} of version {target_version} is not prepared")

        env.set_active_slot(target_version, target_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
        history = metadata.setdefault("history", {})
        history["last_active_slot"] = target_slot
        history["last_active_at"] = datetime.now(timezone.utc).isoformat()
        env.write_version_metadata(target_version, metadata)
        self._smoke_import(env=env, name=name, version=target_version)
        return target_slot

    def run_dev_skill_tests(self, name: str) -> Dict[str, TestResult]:
        """Запуск тестов DEV-навыка прямо из исходников (без install/slots/.runtime).
        - Ищем тесты в <dev>/skills/<name>/tests/**/*.py (pytest discovery).
        - Логи пишем в <dev>/skills/<name>/logs/tests.dev.log.
        - Запрещаем произвольные пути; только внутри DEV root.
        """
        self.caps.require("core", "skills.manage")

        sub = name.strip()
        if not _name_re.match(sub):
            raise ValueError("invalid skill name")

        dev_root = self.ctx.paths.dev_skills_dir()
        skill_dir = (dev_root / sub).resolve()
        try:
            skill_dir.relative_to(dev_root)
        except ValueError:
            raise PermissionError("skill path escapes dev root")
        if not skill_dir.exists() or not skill_dir.is_dir():
            raise FileNotFoundError(f"skill '{name}' not found in DEV at {skill_dir}")
        # Манифест нужен только для подсказок рантайма; отсутствие не фатально
        try:
            manifest = self._load_manifest(skill_dir)
        except FileNotFoundError:
            manifest = {}

        runtime_info = manifest.get("runtime", {}) or {}
        interpreter_value = runtime_info.get("interpreter")
        interpreter = Path(interpreter_value) if interpreter_value else Path(sys.executable)

        # PYTHONPATH: из манифеста + корень пакета AdaOS (безопасно)
        python_paths: list[str] = [p for p in runtime_info.get("python_paths", []) if p]
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        package_root = Path(package_dir).resolve().parent if package_dir else None
        if package_root:
            python_paths.append(str(package_root))
        try:
            python_paths.append(str(self.ctx.paths.package_path()))
        except Exception:
            pass

        dev_dir = self.ctx.paths.dev_dir()  # ...\.adaos\dev\sn_xxxx\
        python_paths.insert(0, str(skill_dir))  # ...\.adaos\dev\sn_xxxx\skills\<name>\
        python_paths.insert(0, str(dev_dir))  # родитель 'skills' — нужен для 'import skills.*'

        extra_env = {
            "ADAOS_DEV_DIR": str(dev_dir),
            "ADAOS_DEV_SKILL_DIR": str(skill_dir),
            "ADAOS_SKILL_NAME": name,
            "ADAOS_SKILL_PACKAGE": f"skills.{name}",
        }

        # Директория логов — в корне навыка (не .runtime)
        logs_dir = skill_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "tests.dev.log"

        # skill_env: ./ .skill_env.json приоритетно, иначе — из манифеста (если задан)
        skill_env_path: Path | None = None
        local_env = skill_dir / ".skill_env.json"
        if local_env.exists():
            skill_env_path = local_env
        else:
            skill_env_raw = runtime_info.get("skill_env")
            if skill_env_raw:
                skill_env_path = Path(skill_env_raw)

        # Запускаем тесты: источник — каталог навыка; pytest сам найдёт tests/**/*.py
        return run_tests(
            skill_dir,  # skill_source
            log_path=log_path,  # <skill>/logs/tests.dev.log
            interpreter=interpreter,  # sys.executable или из манифеста
            python_paths=python_paths,  # из манифеста + package_root
            skill_env_path=skill_env_path,  # опционально
            skill_name=name,
            skill_version=manifest.get("version") or "dev",
            slot_current_dir=skill_dir,  # для совместимости сигнатуры; слотов нет
            dev_mode=True,
            extra_env=extra_env,
        )

