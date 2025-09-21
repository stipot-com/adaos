from __future__ import annotations
import re, os
from pathlib import Path
from typing import Optional

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, Capabilities
from adaos.ports.paths import PathProvider
from adaos.ports.scenarios import ScenarioRepository
from adaos.services.eventbus import emit
from adaos.services.agent_context import get_ctx
from adaos.services.fs.safe_io import remove_tree
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.agent_context import AgentContext

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


class ScenarioManager:
    """Coordinate scenario lifecycle operations via repository and registry."""

    def __init__(
        self,
        *,
        repo: ScenarioRepository,
        registry: SqliteScenarioRegistry,
        git: GitClient,
        paths: PathProvider,
        bus: EventBus,
        caps: Capabilities,  # SqliteScenarioRegistry протоколом не ограничиваем
    ):
        self.repo, self.reg, self.git, self.paths, self.bus, self.caps = repo, registry, git, paths, bus, caps
        self.ctx: AgentContext = get_ctx()

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "scenarios.manage")
        return self.reg.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "scenarios.manage")
        self.repo.ensure()
        return self.repo.list()

    def sync(self) -> None:
        self.caps.require("core", "scenarios.manage", "net.git")
        self.repo.ensure()
        root = self.paths.scenarios_dir()
        names = [r.name for r in self.reg.list()]
        self.git.sparse_init(root, cone=False)
        if names:
            self.git.sparse_set(root, names, no_cone=True)
        self.git.pull(root)
        emit(self.bus, "scenario.sync", {"count": len(names)}, "scenario.mgr")

    def install(self, name: str, *, pin: Optional[str] = None) -> SkillMeta:
        self.caps.require("core", "scenarios.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid scenario name")

        self.repo.ensure()

        self.reg.register(name, pin=pin)
        try:
            test_mode = os.getenv("ADAOS_TESTING") == "1"
            if test_mode:
                return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
            # 3) mono-only установка через репозиторий (sparse-add + pull)
            meta = self.ctx.scenarios_repo.install(name, branch=None)
            if not meta:
                raise FileNotFoundError(f"scenario '{name}' not found in monorepo")
            emit(self.bus, "scenario.installed", {"id": meta.id.value, "pin": pin}, "scenario.mgr")
            return meta
        except Exception:
            self.reg.unregister(name)
            raise

    def remove(self, name: str) -> None:
        self.caps.require("core", "scenarios.manage", "net.git")
        self.repo.ensure()
        self.reg.unregister(name)
        root = self.paths.scenarios_dir()
        names = [r.name for r in self.reg.list()]
        self.git.sparse_init(root, cone=False)
        if names:
            self.git.sparse_set(root, names, no_cone=True)
        self.git.pull(root)
        remove_tree(str(Path(root) / name), fs=self.paths.ctx.fs if hasattr(self.paths, "ctx") else get_ctx().fs)
        emit(self.bus, "scenario.removed", {"id": name}, "scenario.mgr")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.caps.require("core", "scenarios.manage", "git.write", "net.git")
        root = self.paths.scenarios_dir()
        if not (Path(root) / ".git").exists():
            raise RuntimeError("Scenarios repo is not initialized. Run `adaos scenario sync` once.")
        sub = name.strip()
        changed = self.git.changed_files(root, subpath=sub)
        if not changed:
            return "nothing-to-push"
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        msg = sanitize_message(message)
        ctx = get_ctx()
        sha = self.git.commit_subpath(root, subpath=sub, message=msg, author_name=ctx.settings.git_author_name, author_email=ctx.settings.git_author_email, signoff=signoff)
        if sha != "nothing-to-commit":
            self.git.push(root)
        return sha

    def uninstall(self, name: str) -> None:
        """Alias for :meth:`remove` to keep parity with :class:`SkillManager`."""
        self.remove(name)
