from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Optional

from adaos.services.agent_context import AgentContext
from adaos.services.workspace_registry import rebuild_workspace_registry, write_workspace_registry


@dataclass(slots=True)
class SkillUpdateResult:
    updated: bool
    version: Optional[str]


_LOCAL_OVERLAY_FILES: tuple[str, ...] = (".skill_env.json",)


@dataclass(slots=True)
class _OverlayBackup:
    path: Path
    backup: Path


def _iter_overlay_paths(*, repo_root: Path, skill_path: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            return
        seen.add(resolved)
        paths.append(path)

    for name in _LOCAL_OVERLAY_FILES:
        _add(skill_path / name)

    for base_name in ("skills", "scenarios"):
        base = repo_root / base_name
        if not base.exists() or not base.is_dir():
            continue
        for name in _LOCAL_OVERLAY_FILES:
            for path in base.rglob(name):
                if ".runtime" in path.parts:
                    continue
                _add(path)

    return paths


def _git_path_is_tracked(repo_root: Path, relpath: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", relpath.as_posix()],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _git_restore_head(repo_root: Path, relpath: Path) -> None:
    rel = relpath.as_posix()
    attempts = (
        ["git", "-C", str(repo_root), "restore", "--source=HEAD", "--worktree", "--", rel],
        ["git", "-C", str(repo_root), "checkout", "--", rel],
    )
    errors: list[str] = []
    for cmd in attempts:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        detail = proc.stderr.strip() or proc.stdout.strip()
        if detail:
            errors.append(detail)
    joined = "; ".join(errors) or "unknown git restore error"
    raise RuntimeError(f"failed to restore overlay path {rel}: {joined}")


def _reset_workspace_registry_for_pull(repo_root: Path) -> bool:
    registry_path = repo_root / "registry.json"
    if not registry_path.exists() or not registry_path.is_file():
        return False
    relpath = registry_path.relative_to(repo_root)
    if _git_path_is_tracked(repo_root, relpath):
        _git_restore_head(repo_root, relpath)
    else:
        registry_path.unlink()
    return True


def _rebuild_workspace_registry_after_pull(workspace_root: Path) -> None:
    payload = rebuild_workspace_registry(workspace_root)
    write_workspace_registry(workspace_root, payload)


@contextmanager
def _preserve_skill_overlays(*, repo_root: Path, skill_path: Path):
    if not (repo_root / ".git").exists():
        yield
        return

    with tempfile.TemporaryDirectory(prefix="adaos-skill-update-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        preserved: list[_OverlayBackup] = []
        for path in _iter_overlay_paths(repo_root=repo_root, skill_path=skill_path):
            if not path.exists() or not path.is_file():
                continue
            try:
                rel = path.relative_to(repo_root)
            except Exception:
                rel = Path(path.name)
            backup = tmp_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            relpath = path.relative_to(repo_root)
            if _git_path_is_tracked(repo_root, relpath):
                _git_restore_head(repo_root, relpath)
            else:
                path.unlink()
            preserved.append(_OverlayBackup(path=path, backup=backup))
        try:
            yield
        finally:
            for item in preserved:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.backup, item.path)


@dataclass(slots=True)
class SkillUpdateService:
    ctx: AgentContext

    def request_update(self, skill_id: str, *, dry_run: bool = False) -> SkillUpdateResult:
        repo = self.ctx.skills_repo
        meta = repo.get(skill_id)
        if meta is None:
            raise FileNotFoundError(f"skill '{skill_id}' is not installed")

        workspace_root = self.ctx.paths.workspace_dir()
        skills_root = self.ctx.paths.skills_workspace_dir()
        skill_path = Path(getattr(meta, "path", skills_root / skill_id))
        version = getattr(meta, "version", None)

        if dry_run:
            return SkillUpdateResult(updated=False, version=version)

        fs = getattr(self.ctx, "fs", None)
        if fs and hasattr(fs, "require_write"):
            try:
                fs.require_write(str(skill_path))
            except Exception as exc:
                raise PermissionError("fs.readonly") from exc
        settings = getattr(self.ctx, "settings", None)
        git = getattr(self.ctx, "git", None)
        if git is None:
            raise RuntimeError("Git client is not configured")

        monorepo = bool(settings and getattr(settings, "skills_monorepo_url", None))
        repo_root = skill_path
        sparse_target = ""
        pull_root = skill_path
        if monorepo:
            # Monorepo lives at workspace root; skills live under workspace_root/skills/<id>.
            repo_root = workspace_root
            pull_root = workspace_root
            sparse_target = f"skills/{skill_id}"

        with _preserve_skill_overlays(repo_root=repo_root, skill_path=skill_path):
            if monorepo:
                _reset_workspace_registry_for_pull(workspace_root)
                if fs and hasattr(fs, "require_write"):
                    try:
                        fs.require_write(str(workspace_root))
                        fs.require_write(str(skills_root))
                    except Exception as exc:
                        raise PermissionError("fs.readonly") from exc
                # Ensure the requested skill path is present in sparse patterns before pulling.
                git.sparse_add(str(workspace_root), sparse_target)
                git.pull(str(pull_root))
                _rebuild_workspace_registry_after_pull(workspace_root)
            else:
                git.pull(str(skill_path))

        refreshed = repo.get(skill_id) or meta
        return SkillUpdateResult(updated=True, version=getattr(refreshed, "version", version))
