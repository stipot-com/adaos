from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

import yaml

from adaos.domain import SkillId, SkillMeta  # если есть ScenarioId/ScenarioMeta — замени здесь
from adaos.ports.paths import PathProvider
from adaos.ports.git import GitClient
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
    """
    Унифицированный адаптер сценариев:
      - monorepo mode: если задан monorepo_url (и, опционально, monorepo_branch)
      - fs mode (multi-repo): если monorepo_url не задан — каждый сценарий отдельным git-репо
    """

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
        root = self.paths.workspace_dir()
        scenarios_root = self.paths.scenarios_dir()
        if not scenarios_root.exists():
            return items
        for ch in sorted(scenarios_root.iterdir()):
            if ch.is_dir() and not ch.name.startswith("."):
                items.append(_read_manifest(ch))
        return items

    def get(self, scenario_id: str) -> Optional[SkillMeta]:
        self.ensure()
        p = self.paths.scenarios_dir() / scenario_id
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
        """
        monorepo mode: ref = имя сценария (подкаталог монорепо); URL запрещён.
        fs mode:      ref = полный git URL; dest_name опционален.
        """
        self.ensure()
        name = ref.strip()
        p: Path = self.paths.scenarios_dir() / name
        name = ref.strip()
        if not _NAME_RE.match(name):
            raise ValueError("invalid scenario name")
        self.git.sparse_init(str(self.paths.workspace_dir()), cone=False)
        self.git.sparse_add(str(self.paths.workspace_dir()), f"scenarios/{name}")
        self.git.pull(str(self.paths.workspace_dir()))
            
        if not p.exists():
            raise FileNotFoundError(f"scenario '{name}' not present after sync")
        return _read_manifest(p)

    # --- uninstall ---

    def uninstall(self, scenario_id: str) -> None:
        self.ensure()
        p = self.paths.scenarios_dir() / scenario_id
        if not p.exists():
            raise FileNotFoundError(f"scenario '{scenario_id}' not found")
        if remove_tree:
            ctx = getattr(self.paths, "ctx", None)
            fs = getattr(ctx, "fs", None) if ctx else None
            remove_tree(str(p), fs=fs)  # type: ignore[arg-type]
        else:
            shutil.rmtree(p)