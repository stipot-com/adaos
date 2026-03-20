from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Optional

from adaos.services.agent_context import AgentContext


@dataclass(slots=True)
class SkillUpdateResult:
    updated: bool
    version: Optional[str]


_LOCAL_OVERLAY_FILES: tuple[str, ...] = (".skill_env.json",)


@dataclass(slots=True)
class _OverlayBackup:
    path: Path
    backup: Path


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


@contextmanager
def _preserve_skill_overlays(*, repo_root: Path, skill_path: Path):
    if not (repo_root / ".git").exists():
        yield
        return

    with tempfile.TemporaryDirectory(prefix="adaos-skill-update-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        preserved: list[_OverlayBackup] = []
        for name in _LOCAL_OVERLAY_FILES:
            path = skill_path / name
            if not path.exists() or not path.is_file():
                continue
            backup = tmp_root / name
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

        root = self.ctx.paths.skills_dir()
        skill_path = Path(getattr(meta, "path", root / skill_id))
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
        repo_root = root
        if monorepo and not (repo_root / ".git").exists():
            repo_root = root.parent

        with _preserve_skill_overlays(repo_root=repo_root, skill_path=skill_path):
            if monorepo:
                if fs and hasattr(fs, "require_write"):
                    try:
                        fs.require_write(str(root))
                    except Exception as exc:
                        raise PermissionError("fs.readonly") from exc
                git.sparse_add(str(root), skill_id)
                git.pull(str(root))
            else:
                git.pull(str(skill_path))

        refreshed = repo.get(skill_id) or meta
        return SkillUpdateResult(updated=True, version=getattr(refreshed, "version", version))
