from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import requests


_GITHUB_HTTPS_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)")
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/#?]+)")


@dataclass(frozen=True, slots=True)
class GithubRepoRef:
    owner: str
    repo: str

    @property
    def repo_name(self) -> str:
        name = (self.repo or "").strip()
        if name.endswith(".git"):
            name = name[:-4]
        return name or "repo"


def _parse_github_repo(url: str) -> GithubRepoRef | None:
    s = (url or "").strip()
    if not s:
        return None
    m = _GITHUB_HTTPS_RE.match(s)
    if not m:
        m = _GITHUB_SSH_RE.match(s)
    if not m:
        return None
    owner = m.group("owner").strip()
    repo = m.group("repo").strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return GithubRepoRef(owner=owner, repo=repo)


def github_zip_url(url: str, *, branch: str) -> str:
    ref = _parse_github_repo(url)
    if not ref:
        raise ValueError("Only github.com repositories are supported for zip fallback.")
    b = (branch or "").strip()
    if not b:
        raise ValueError("branch is required for zip fallback")
    return f"https://github.com/{ref.owner}/{ref.repo}/archive/refs/heads/{b}.zip"


def _safe_relpath(rel: str) -> str:
    p = Path(rel)
    if p.is_absolute():
        raise ValueError("unsafe path (absolute)")
    norm = str(p).replace("\\", "/")
    if norm.startswith("../") or "/../" in norm or norm == "..":
        raise ValueError("unsafe path traversal")
    return norm.strip("/")


def _rm_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    try:
        path.unlink()
    except Exception:
        pass


def _write_file(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def extract_github_zip_subpaths(
    *,
    repo_url: str,
    branch: str,
    dest_root: Path,
    subpaths: Sequence[str],
    timeout: float = 60.0,
) -> None:
    """
    Download a GitHub repo zip archive for the given branch and extract only the requested subpaths.

    `subpaths` are paths *inside the repo root* (e.g. "skills/foo", "scenarios/bar").
    """
    url = github_zip_url(repo_url, branch=branch)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    zdata = io.BytesIO(resp.content)
    with zipfile.ZipFile(zdata) as zf:
        # Find the top-level prefix "<repo>-<branch>/".
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        if not names:
            raise RuntimeError("empty zip archive")
        first = names[0]
        prefix = first.split("/", 1)[0].strip("/") + "/"

        wanted: list[str] = []
        for sp in subpaths:
            rel = _safe_relpath(sp)
            wanted.append(prefix + rel + "/")
            wanted.append(prefix + rel)

        for name in zf.namelist():
            if not name or name.endswith("/"):
                continue
            if not name.startswith(prefix):
                continue
            rel_in_repo = name[len(prefix) :]
            # Filter to requested subpaths.
            ok = False
            for w in wanted:
                if name == w or name.startswith(w.rstrip("/") + "/"):
                    ok = True
                    break
            if not ok:
                continue
            out_path = dest_root / _safe_relpath(rel_in_repo)
            _write_file(out_path, zf.read(name))


def materialize_subpath_from_github_zip(
    *,
    repo_url: str,
    branch: str,
    dest_root: Path,
    subpath: str,
    clean: bool = True,
) -> Path:
    """
    Ensure a single repo subpath is materialized at `dest_root/subpath`.
    """
    rel = _safe_relpath(subpath)
    target = dest_root / rel
    if clean:
        _rm_tree(target)
    extract_github_zip_subpaths(repo_url=repo_url, branch=branch, dest_root=dest_root, subpaths=[rel])
    return target

