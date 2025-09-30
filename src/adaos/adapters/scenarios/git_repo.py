from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from adaos.adapters.git.workspace import SparseWorkspace, wait_for_materialized
from adaos.domain import SkillId, SkillMeta  # если есть ScenarioId/ScenarioMeta — замени здесь
from adaos.ports.git import GitClient
from adaos.ports.paths import PathProvider
from adaos.ports.scenarios import ScenarioRepository

try:
    from adaos.services.fs.safe_io import remove_tree  # мягкое удаление, если доступно
except Exception:  # pragma: no cover
    remove_tree = None

_MANIFEST_NAMES = ("scenario.yaml", "manifest.yaml", "adaos.scenario.yaml")
_CATALOG_FILE = "scenarios.yaml"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@")) or s.endswith(".git")


def _repo_basename_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "scenario"


def _safe_join(root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise ValueError("unsafe path traversal (absolute)")
    p = (root / rel_path).resolve()
    root = root.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("unsafe path traversal")
    return p


def _read_manifest(dirpath: Path) -> SkillMeta:
    for fname in _MANIFEST_NAMES:
        p = dirpath / fname
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            sid = str(data.get("id") or dirpath.name)
            name = str(data.get("name") or sid)
            ver = str(data.get("version") or "0.0.0")
            return SkillMeta(id=SkillId(sid), name=name, version=ver, path=str(dirpath.resolve()))
    sid = dirpath.name
    return SkillMeta(id=SkillId(sid), name=sid, version="0.0.0", path=str(dirpath.resolve()))


def _read_catalog(paths: PathProvider) -> list[str]:
    candidates: list[Path] = []
    base = getattr(paths, "base", None)
    if base:
        candidates.append(Path(base) / _CATALOG_FILE)
    scen_dir = Path(paths.scenarios_dir())
    candidates.extend([scen_dir.parent / _CATALOG_FILE, scen_dir / _CATALOG_FILE])
    for c in candidates:
        if c.exists():
            y = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
            items = y.get("scenarios") or []
            return [str(s).strip() for s in items if str(s).strip()]
    return []


@dataclass
class GitScenarioRepository(ScenarioRepository):
    """Scenario repository backed by the shared monorepo workspace."""

    def __init__(
        self,
        *,
        paths: PathProvider,
        git: GitClient,
        url: Optional[str] = None,
        branch: Optional[str] = None,
    ):
        self.paths = paths
        self.git = git
        self.monorepo_url = url
        self.monorepo_branch = branch

    def _candidate_roots(self) -> list[Path]:
        roots: list[Path] = []
        primary = Path(self.paths.scenarios_dir())
        roots.append(primary)
        cache_attr = getattr(self.paths, "scenarios_cache_dir", None)
        if cache_attr:
            cache_root = cache_attr() if callable(cache_attr) else cache_attr
            if cache_root:
                roots.append(Path(cache_root) / "scenarios")
        uniq: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                uniq.append(root)
        return uniq

    def _root(self) -> Path:
        cache_dir = getattr(self.paths, "scenarios_cache_dir", None)
        if cache_dir is not None:
            cache = cache_dir() if callable(cache_dir) else cache_dir
        else:
            base = getattr(self.paths, "scenarios_dir")
            cache = base() if callable(base) else base
        root = Path(cache)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _scenario_dir(self, root: Path, name: str) -> Path:
        return _safe_join(root, f"scenarios/{name}")

    def _ensure_monorepo(self) -> None:
        if os.getenv("ADAOS_TESTING") == "1":
            return
        self.git.ensure_repo(str(self.paths.workspace_dir()), self.monorepo_url, branch=self.monorepo_branch)

    def ensure(self) -> None:
        if self.monorepo_url:
            self._ensure_monorepo()
        else:
            self.paths.scenarios_dir().mkdir(parents=True, exist_ok=True)

    # --- list / get ---

    def list(self) -> list[SkillMeta]:
        self.ensure()
        items: List[SkillMeta] = []
        for root in self._candidate_roots():
            if not root.exists():
                continue
            for ch in sorted(root.iterdir()):
                if ch.is_dir() and not ch.name.startswith("."):
                    items.append(_read_manifest(ch))
        return items

    def get(self, scenario_id: str) -> Optional[SkillMeta]:
        self.ensure()
        for root in self._candidate_roots():
            p = root / scenario_id
            if p.exists():
                m = _read_manifest(p)
                if m.id.value == scenario_id:
                    return m
        for m in self.list():
            if m.id.value == scenario_id:
                return m
        return None

    # --- install ---

    def install(
        self,
        ref: str,
        *,
        branch: Optional[str] = None,
        dest_name: Optional[str] = None,
    ) -> SkillMeta:
        """Install a scenario into the workspace using sparse checkout."""

        self.ensure()
        name = ref.strip()
        if not _NAME_RE.match(name):
            raise ValueError("invalid scenario name")

        workspace_root = self.paths.workspace_dir()
        sparse = SparseWorkspace(self.git, workspace_root)
        target = f"scenarios/{name}"
        sparse.update(add=[target])
        self.git.pull(str(workspace_root))

        scenario_dir: Path = self.paths.scenarios_dir() / name
        try:
            wait_for_materialized(scenario_dir, files=_MANIFEST_NAMES)
        except FileNotFoundError as exc:  # pragma: no cover - defensive logging
            sparse.update(remove=[target])
            self.git.rm_cached(str(workspace_root), target)
            raise FileNotFoundError(f"scenario '{name}' not present after sync") from exc
        return _read_manifest(scenario_dir)

    # --- uninstall ---

    def uninstall(self, scenario_id: str) -> None:
        self.ensure()
        workspace_root = self.paths.workspace_dir()
        sparse = SparseWorkspace(self.git, workspace_root)
        target = f"scenarios/{scenario_id}"
        sparse.update(remove=[target])
        self.git.rm_cached(str(workspace_root), target)

        p = self.paths.scenarios_dir() / scenario_id
        if not p.exists():
            return

        if remove_tree:
            ctx = getattr(self.paths, "ctx", None)
            fs = getattr(ctx, "fs", None) if ctx else None
            remove_tree(str(p), fs=fs)  # type: ignore[arg-type]
        else:
            shutil.rmtree(p)
        if p.exists():  # pragma: no cover - defensive fallback
            raise FileExistsError(f"scenario '{scenario_id}' still present after uninstall")