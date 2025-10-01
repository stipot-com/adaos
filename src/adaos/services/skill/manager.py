# src\adaos\services\skill\manager.py
from __future__ import annotations

import json
import os
import re
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
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment, SkillSlotPaths
from adaos.services.skill.tests_runner import TestResult, run_tests
from adaos.services.skill.validation import SkillValidationService

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

        self._ensure_runtime_env(slot, manifest)
        interpreter = self._resolve_interpreter(slot)
        defaults = self._policy_defaults()
        policy_overrides = self._policy_overrides()

        resolved = self._enrich_manifest(
            manifest=manifest,
            slot=slot,
            interpreter=interpreter,
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
        target_version = version or env.resolve_active_version()
        if not target_version:
            raise RuntimeError("no installed versions")
        env.prepare_version(target_version)
        target_slot = slot or env.select_inactive_slot(target_version)
        env.set_active_slot(target_version, target_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
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
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        resolved_path = slot_meta.get("resolved_manifest") or str(
            env.build_slot_paths(version, active_slot).resolved_manifest
        )
        return {
            "name": name,
            "version": version,
            "active_slot": active_slot,
            "resolved_manifest": resolved_path,
            "tests": slot_meta.get("tests", {}),
            "history": metadata.get("history", {}),
        }

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

    def run_tool(self, name: str, tool: str, payload: Mapping[str, Any], *, timeout: float | None = None) -> Any:
        status = self.runtime_status(name)
        manifest_path = Path(status["resolved_manifest"])
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tool_spec = (data.get("tools") or {}).get(tool)
        if not tool_spec:
            raise KeyError(f"tool '{tool}' not found in resolved manifest")
        command = tool_spec.get("command")
        if not command:
            raise RuntimeError("resolved manifest missing command")
        import subprocess

        proc = subprocess.run(
            command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout or tool_spec.get("timeout_seconds"),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return json.loads(proc.stdout or "{}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _runtime_env(self, name: str) -> SkillRuntimeEnvironment:
        return SkillRuntimeEnvironment(
            skills_root=Path(self.ctx.paths.skills_dir()),
            skill_name=name,
        )

    def _load_manifest(self, skill_dir: Path) -> Dict[str, Any]:
        candidates = ["resolved.manifest.json", "manifest.json", "skill.json", "skill.yaml", "manifest.yaml"]
        for name in candidates:
            path = skill_dir / name
            if not path.exists():
                continue
            if path.suffix in {".yaml", ".yml"}:
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError("skill manifest not found")

    def _ensure_runtime_env(self, slot: SkillSlotPaths, manifest: Mapping[str, Any]) -> None:
        runtime = (manifest.get("runtime") or {}).get("type", "python").lower()
        if runtime == "python":
            self._ensure_python_env(slot)

    def _ensure_python_env(self, slot: SkillSlotPaths) -> None:
        if slot.venv_dir.exists() and any(slot.venv_dir.iterdir()):
            return
        builder = venv.EnvBuilder(with_pip=False, clear=True)
        builder.create(str(slot.venv_dir))

    def _resolve_interpreter(self, slot: SkillSlotPaths) -> Path:
        python = slot.venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        if python.exists():
            return python
        import sys

        return Path(sys.executable)

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
        defaults: PolicyDefaults,
        policy_overrides: Mapping[str, Any],
        skill_dir: Path,
    ) -> Dict[str, Any]:
        tools: Dict[str, Dict[str, Any]] = {}
        for item in manifest.get("tools", []) or []:
            tool_name = item.get("name")
            if not tool_name:
                continue
            shim_path = self._write_shim(slot, tool_name, manifest, skill_dir)
            tools[tool_name] = {
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
                "secrets": self._preserve_secret_placeholders(item.get("secrets", [])),
            }

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
            },
            "tools": tools,
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
        }

    def _write_shim(
        self,
        slot: SkillSlotPaths,
        tool_name: str,
        manifest: Mapping[str, Any],
        skill_dir: Path,
    ) -> Path:
        bin_dir = slot.bin_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        shim_path = bin_dir / f"{tool_name}.py"
        runtime_cfg = manifest.get("runtime") or {}
        entry = None
        for tool in manifest.get("tools", []) or []:
            if tool.get("name") == tool_name:
                entry = tool.get("entry")
                break
        module_path: str | None
        attr: str
        if entry:
            module_path, _, attr = entry.partition(":")
            if not attr:
                attr = tool_name
            if not module_path:
                module_path = runtime_cfg.get("module") or "handlers.main"
        else:
            module_path = runtime_cfg.get("module") or "handlers.main"
            attr = tool_name

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
            f"    skill_dir = pathlib.Path({repr(str(skill_dir))})\n"
            f"    return execute_tool(skill_dir, module={repr(module_path)}, attr={repr(attr)}, payload=data)\n\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        )
        shim_path.write_text(shim_template, encoding="utf-8")
        os.chmod(shim_path, 0o755)
        return shim_path

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
