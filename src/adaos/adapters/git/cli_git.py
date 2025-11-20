# src\adaos\adapters\git\cli_git.py
from __future__ import annotations
import subprocess, os
from pathlib import Path
from typing import Optional, Final, Sequence, Union
from adaos.ports.git import GitClient


class GitError(RuntimeError): ...


StrOrPath = Union[str, Path]


def _run_git(args: list[str], cwd: Optional[StrOrPath] = None) -> str:
    if cwd is not None:
        cwd = str(Path(cwd))  # единая точка приведения к str
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    # TODO Проверить, git нет, но папка не пустая. Вместо операции c git даем дружественную ошибку
    # destination path 'C:\git\MUIV\adaos_test\adaos\.adaos_1\workspace' already exists and is not an empty directory
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def _append_exclude(dir: str, lines: list[str]) -> None:
    p = Path(dir) / ".git" / "info" / "exclude"
    existing = set()
    if p.exists():
        existing = set(p.read_text(encoding="utf-8").splitlines())
    merged = existing.union(lines)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(sorted(merged)) + "\n", encoding="utf-8")


class CliGitClient(GitClient):
    def __init__(self, depth: int = 1) -> None:
        self._depth: Final[int] = depth

    def ensure_repo(self, dir: StrOrPath, url: str, branch: Optional[str] = None) -> None:
        d = Path(dir)
        d.mkdir(parents=True, exist_ok=True)
        git_dir = d / ".git"
        if not git_dir.exists():
            # Prefer clone into empty directory; if directory is non-empty, fall back to init+fetch
            try:
                args = ["clone", url, str(d)]
                if self._depth > 0:
                    args += [f"--depth={self._depth}"]
                if branch:
                    args += ["--branch", branch]
                _run_git(args, cwd=None)
                try:
                    _run_git(["sparse-checkout", "init", "--cone"], cwd=str(d))
                except Exception:
                    pass
            except GitError:
                # Non-empty destination — initialize in place and attach remote
                _run_git(["init"], cwd=str(d))
                try:
                    _run_git(["remote", "add", "origin", url], cwd=str(d))
                except GitError:
                    # remote may already exist — continue
                    pass
                # Fetch and checkout the desired branch (or main)
                target_branch = branch or "main"
                try:
                    fetch_args = ["fetch", "--prune", "origin"]
                    if self._depth > 0:
                        fetch_args += [f"--depth={self._depth}"]
                    fetch_args += [target_branch]
                    _run_git(fetch_args, cwd=str(d))
                except GitError:
                    # try fetching all if branch-specific fetch failed
                    _run_git(["fetch", "--prune", "origin"], cwd=str(d))
                try:
                    _run_git(["checkout", "-B", target_branch, f"origin/{target_branch}"], cwd=str(d))
                except GitError:
                    # Last resort: checkout whatever HEAD points to
                    _run_git(["checkout", target_branch], cwd=str(d))
                try:
                    _run_git(["sparse-checkout", "init", "--cone"], cwd=str(d))
                except Exception:
                    pass
        _append_exclude(
            dir,
            [
                "*.pyc",
                "__pycache__/",
                ".venv/",
                "state/",
                "cache/",
                "logs/",
            ],
        )

    def pull(self, dir: StrOrPath) -> None:
        _run_git(["pull", "--ff-only"], cwd=dir)

    def current_commit(self, dir: StrOrPath) -> str:
        return _run_git(["rev-parse", "HEAD"], cwd=dir)

    # --- sparse ---
    def sparse_init(self, dir: StrOrPath, cone: bool = True) -> None:
        args = ["sparse-checkout", "init"]
        if cone:
            args.append("--cone")
        _run_git(args, cwd=dir)

    def sparse_set(self, dir: StrOrPath, paths: Sequence[str], no_cone: bool = True) -> None:
        args = ["sparse-checkout", "set"]
        if no_cone:
            args.append("--no-cone")
        _run_git([*args, *paths], cwd=dir)

    def sparse_add(self, dir: StrOrPath, path: str) -> None:
        try:
            _run_git(["sparse-checkout", "add", path], cwd=dir)
        except GitError:
            # fallback: перечитать и расширить вручную (как в твоей логике)
            info = Path(dir) / ".git" / "info"
            sp = info / "sparse-checkout"
            lines = sp.read_text(encoding="utf-8").splitlines() if sp.exists() else []
            if path not in lines:
                info.mkdir(parents=True, exist_ok=True)
                lines.append(path)
                sp.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def sparse_reapply(self, dir: StrOrPath) -> None:
        try:
            _run_git(["sparse-checkout", "reapply"], cwd=dir)
        except GitError:
            # Non sparse worktrees raise an error — ignore silently to keep idempotent.
            pass

    def rm_cached(self, dir: StrOrPath, path: str) -> None:
        try:
            _run_git(["rm", "--cached", "-r", "--ignore-unmatch", path], cwd=dir)
        except GitError:
            # Nothing tracked for the path — ignore.
            pass

    def changed_files(self, dir: StrOrPath, subpath: Optional[str] = None) -> list[str]:
        # untracked (-o) + modified (-m), исключая игнор по .gitignore
        args = ["ls-files", "-m", "-o", "--exclude-standard"]
        if subpath:
            args += ["--", subpath]
        out = _run_git(args, cwd=dir)
        files = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return files

    def _current_branch(self, dir: StrOrPath) -> str:
        out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dir).strip()
        return out or "main"

    def commit_subpath(self, dir: StrOrPath, subpath: str, message: str, author_name: str, author_email: str, signoff: bool = False) -> str:
        # stage только подпуть
        _run_git(["add", "--", subpath], cwd=dir)
        # пустой ли индекс?
        status = _run_git(["diff", "--cached", "--name-only"], cwd=dir)
        if not status.strip():
            return "nothing-to-commit"
        # автор в -c для изоляции от глобальных конфигов
        args = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", message]
        if signoff:
            args.append("--signoff")
        _run_git(args, cwd=dir)
        return _run_git(["rev-parse", "HEAD"], cwd=dir).strip()

    def push(self, dir: StrOrPath, remote: str = "origin", branch: Optional[str] = None) -> None:
        branch = branch or self._current_branch(dir)
        # 1) сначала пробуем обычный fast-forward pull (быстро и дёшево)
        try:
            _run_git(["pull", "--ff-only", remote, branch], cwd=dir)
        except GitError:
            # 2) если не вышло (non-ff), делаем rebase с автосбросом стэша
            #    но shallow-репо могут не иметь базовой истории → разшалловим и повторим
            try:
                _run_git(["-c", "rebase.autoStash=true", "pull", "--rebase", remote, branch], cwd=dir)
            except GitError:
                # попытка «расшалловить» историю и снова rebase
                try:
                    _run_git(["fetch", "--prune", "--unshallow", remote], cwd=dir)
                except GitError:
                    # если git старый и не знает --unshallow, просто увеличим глубину
                    _run_git(["fetch", "--prune", "--depth=50", remote], cwd=dir)
                _run_git(["-c", "rebase.autoStash=true", "pull", "--rebase", remote, branch], cwd=dir)
        # 3) когда локальная ветка на вершине origin/<branch> — пушим
        _run_git(["push", remote, branch], cwd=dir)
