# src\adaos\services\skill\manager.py
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass
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
from adaos.services.secrets.service import SecretsService

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


class _SkillSecretsBackend:
    """Simple JSON-backed secrets store scoped to a single skill runtime."""

    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if not self._path.exists():
            return {"profile": {}, "global": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"profile": {}, "global": {}}

    def _save(self, data: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def put(self, key: str, value: str, *, scope: str = "profile", meta: Dict[str, Any] | None = None) -> None:
        data = self._load()
        bucket = data.setdefault(scope, {})
        bucket[key] = {"value": value, "meta": meta or {}}
        self._save(data)

    def get(self, key: str, *, default: str | None = None, scope: str = "profile") -> str | None:
        data = self._load()
        bucket = data.get(scope, {})
        record = bucket.get(key)
        if not isinstance(record, dict):
            return default
        return record.get("value", default)

    def delete(self, key: str, *, scope: str = "profile") -> None:
        data = self._load()
        bucket = data.get(scope, {})
        if key in bucket:
            bucket.pop(key)
            self._save(data)

    def list(self, *, scope: str = "profile") -> list[Dict[str, Any]]:
        data = self._load()
        bucket = data.get(scope, {})
        return [
            {"key": k, "meta": (rec.get("meta") if isinstance(rec, dict) else {})}
            for k, rec in sorted(bucket.items())
        ]

    def import_items(self, items: Iterable[Dict[str, Any]], *, scope: str = "profile") -> int:
        data = self._load()
        bucket = data.setdefault(scope, {})
        count = 0
        for item in items:
            key = item.get("key")
            value = item.get("value")
            if not key or value is None:
                continue
            bucket[key] = {"value": str(value), "meta": item.get("meta") or {}}
            count += 1
        self._save(data)
        return count

    def export_items(self, *, scope: str = "profile") -> list[Dict[str, Any]]:
        data = self._load()
        bucket = data.get(scope, {})
        return [
            {"key": k, "value": rec.get("value"), "meta": rec.get("meta") or {}}
            for k, rec in bucket.items()
            if isinstance(rec, dict)
        ]


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

    def validate_skill(self, name: str, *, strict: bool = True, probe_tools: bool = False) -> ValidationReport:
        """Run validation for a skill via the service layer."""

        self.caps.require("core", "skills.manage")
        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        try:
            report = SkillValidationService(ctx).validate(
                name,
                strict=strict,
                install_mode=False,
                probe_tools=probe_tools,
            )
        finally:
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
        return report

    def run_skill_tests(self, name: str) -> Dict[str, TestResult]:
        """Execute runtime tests without preparing a new slot."""

        self.caps.require("core", "skills.manage")
        skills_root = Path(self.ctx.paths.skills_dir())
        skill_dir = skills_root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=name)
        version = env.resolve_active_version()
        if version:
            env.prepare_version(version)
            slot_name = env.read_active_slot(version)
            slot_paths = env.build_slot_paths(version, slot_name)
            log_path = slot_paths.logs_dir / "tests.manual.log"
        else:
            env.ensure_base()
            log_path = env.data_root() / "files" / "tests.manual.log"

        return run_tests(skill_dir, log_path=log_path)

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
        remove_tree(
            str(root / "skills" / name),
            fs=self.ctx.paths.ctx.fs if hasattr(self.ctx.paths, "ctx") else get_ctx().fs,
        )
        self.cleanup_runtime(name, purge_data=True)
        emit(self.bus, "skill.uninstalled", {"id": name}, "skill.mgr")

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
        skills_root = Path(self.ctx.paths.skills_dir())
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
            interpreter, python_paths = self._prepare_runtime_environment(
                env=env,
                slot=slot,
                manifest=manifest,
                skill_dir=skill_dir,
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
            skill_dir=skill_dir,
        )

        tests: Dict[str, TestResult] = {}
        if run_tests:
            log_file = slot.logs_dir / "tests.log"
            tests = run_tests(skill_dir, log_path=log_file)
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
            raise RuntimeError(
                f"slot {target_slot} of version {target_version} is not prepared; run 'adaos skill install {name} --slot={target_slot}' first"
            )
        env.set_active_slot(target_version, target_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
        history = metadata.setdefault("history", {})
        history["last_active_slot"] = target_slot
        history["last_active_at"] = datetime.now(timezone.utc).isoformat()
        env.write_version_metadata(target_version, metadata)
        return target_slot

    def rollback_runtime(self, name: str) -> str:
        env = self._runtime_env(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        return env.rollback_slot(version)

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
        skills_root = Path(self.ctx.paths.skills_dir())
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
        base = Path(ctx.paths.skills_dir())
        return {
            "skill_root": str((base / name).resolve()),
            "runtime_root": str((base / ".runtime" / name).resolve()),
            "active_slot": status["active_slot"],
            "resolved_manifest": status["resolved_manifest"],
        }

    def setup_skill(self, name: str) -> Any:
        """Run the optional setup tool for a skill."""

        status = self.runtime_status(name)
        env = self._runtime_env(name)
        manifest_path = Path(status["resolved_manifest"])
        version = status.get("version")
        slot_name = status.get("active_slot")
        ready = status.get("ready", True)

        if not ready:
            slot_name = status.get("pending_slot") or slot_name
            version = status.get("pending_version") or version
            if not slot_name or not version:
                raise RuntimeError("skill has no prepared slot available for setup")
            env.prepare_version(version)
            metadata = env.read_version_metadata(version)
            slot_paths = env.build_slot_paths(version, slot_name)
            slot_meta = metadata.get("slots", {}).get(slot_name, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)

        if not manifest_path.exists():
            raise RuntimeError("skill runtime is not prepared; install the skill first")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = manifest.get("tools") or {}
        if "setup" not in tools:
            raise RuntimeError("setup not supported for this skill")

        return self.run_tool(
            name,
            "setup",
            {},
            allow_inactive=not ready,
            slot=slot_name,
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
        skill_dir = Path(data.get("source") or (Path(self.ctx.paths.skills_dir()) / name))
        slot_name = data.get("slot") or slot_name
        slot = env.build_slot_paths(version or data.get("version"), slot_name)
        runtime_info = data.get("runtime", {})
        extra_paths = [Path(p) for p in runtime_info.get("python_paths", []) if p]
        skill_env_path = Path(runtime_info.get("skill_env") or (slot.env_dir / ".skill_env.json"))

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_secrets = ctx.secrets
        ctx.secrets = SecretsService(_SkillSecretsBackend(env.data_root() / "files" / "secrets.json"), ctx.caps)
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

                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_call_tool)
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _runtime_env(self, name: str) -> SkillRuntimeEnvironment:
        return SkillRuntimeEnvironment(
            skills_root=Path(self.ctx.paths.skills_dir()),
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
            return self._prepare_python_runtime(env=env, slot=slot, manifest=manifest, runtime_cfg=runtime_cfg, skill_dir=skill_dir)
        raise NotImplementedError(f"runtime type '{runtime_type}' is not supported")

    def _prepare_python_runtime(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        manifest: Mapping[str, Any],
        runtime_cfg: Mapping[str, Any],
        skill_dir: Path,
    ) -> tuple[Path, list[str]]:
        python_spec = runtime_cfg.get("python")
        use_isolated = bool(python_spec)
        interpreter = self._ensure_python_interpreter(slot, use_isolated=use_isolated)
        python_paths = self._install_python_dependencies(
            interpreter=interpreter,
            manifest=manifest,
            slot=slot,
            use_isolated=use_isolated,
        )
        self._sync_skill_env(env=env, skill_dir=skill_dir, slot=slot)
        return interpreter, python_paths

    def _ensure_python_interpreter(self, slot: SkillSlotPaths, *, use_isolated: bool) -> Path:
        if use_isolated:
            builder = venv.EnvBuilder(with_pip=True, clear=True)
            builder.create(str(slot.venv_dir))
            suffix = "Scripts" if os.name == "nt" else "bin"
            exe = "python.exe" if os.name == "nt" else "python"
            return slot.venv_dir / suffix / exe
        return Path(sys.executable)

    def _install_python_dependencies(
        self,
        *,
        interpreter: Path,
        manifest: Mapping[str, Any],
        slot: SkillSlotPaths,
        use_isolated: bool,
    ) -> list[str]:
        dependencies = self._collect_dependencies(manifest)
        if not dependencies:
            return []

        command = [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
        ]
        python_paths: list[str] = []
        if not use_isolated:
            slot.env_dir.mkdir(parents=True, exist_ok=True)
            command.extend(["--target", str(slot.env_dir)])
            python_paths.append(str(slot.env_dir))
        command.extend(dependencies)
        try:
            subprocess.check_call(command)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"failed to install dependencies: {exc}") from exc
        if use_isolated:
            return self._python_site_packages(interpreter)
        return python_paths

    def _python_site_packages(self, interpreter: Path) -> list[str]:
        """Return site-packages directories for the provided interpreter."""

        script = """
import json
import site
paths = []
try:
    paths.extend(site.getsitepackages())
except Exception:
    pass
try:
    user = site.getusersitepackages()
except Exception:
    user = None
if user:
    paths.append(user)
print(json.dumps(list(dict.fromkeys(p for p in paths if p))))
"""
        try:
            output = subprocess.check_output(
                [str(interpreter), "-c", script],
                text=True,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"failed to resolve site-packages for {interpreter}: {exc}") from exc
        try:
            data = json.loads(output.strip() or "[]")
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"unexpected site-packages output: {output!r}") from exc
        return [str(Path(p)) for p in data if p]

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
        target = slot.env_dir / ".skill_env.json"
        for candidate in candidates:
            if candidate.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                if candidate is not store_path:
                    store_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, store_path)
                break

    def _persist_skill_env(self, env: SkillRuntimeEnvironment, slot: SkillSlotPaths) -> None:
        source = slot.env_dir / ".skill_env.json"
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
                "venv": str(slot.venv_dir),
                "env": str(slot.env_dir),
                "tmp": str(slot.tmp_dir),
                "python_paths": list(python_paths),
                "skill_env": str(slot.env_dir / ".skill_env.json"),
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
