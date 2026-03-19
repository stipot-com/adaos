from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from adaos.apps.bootstrap import init_ctx
from adaos.services.core_slots import write_slot_manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare inactive AdaOS core slot")
    parser.add_argument("--target-rev", default="")
    parser.add_argument("--target-version", default="")
    parser.add_argument("--slot", required=True)
    parser.add_argument("--slot-dir", required=True)
    parser.add_argument("--base-dir", default="")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--source-repo-root", default="")
    parser.add_argument("--shared-dotenv-path", default="")
    parser.add_argument("--repo-url", default=os.getenv("ADAOS_CORE_UPDATE_REPO_URL", "https://github.com/stipot-com/adaos.git"))
    return parser.parse_args()


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    completed = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed rc={completed.returncode}: {' '.join(cmd)}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _clone_repo(repo_url: str, target_rev: str, checkout_dir: Path) -> None:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required for core updates but is not installed")
    cmd = [git, "clone", "--depth", "1"]
    if target_rev:
        cmd.extend(["--branch", target_rev])
    cmd.extend([repo_url, str(checkout_dir)])
    _run(cmd)


def _clone_local_repo(source_repo_root: Path, target_rev: str, checkout_dir: Path) -> None:
    git = shutil.which("git")
    git_dir = source_repo_root / ".git"
    if git and git_dir.exists():
        try:
            _run([git, "clone", str(source_repo_root), str(checkout_dir)])
            if target_rev:
                _run([git, "checkout", target_rev], cwd=checkout_dir)
            return
        except Exception:
            pass
    shutil.copytree(
        source_repo_root,
        checkout_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".adaos",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".coverage",
            "node_modules",
        ),
    )


def _is_git_repo(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return (path / ".git").exists()
    except Exception:
        return False


def prepare_slot(
    *,
    slot: str,
    slot_dir_path: str | os.PathLike[str],
    base_dir: str | os.PathLike[str] = "",
    repo_root: str | os.PathLike[str] = "",
    source_repo_root: str | os.PathLike[str] = "",
    shared_dotenv_path: str | os.PathLike[str] = "",
    target_rev: str = "",
    target_version: str = "",
    repo_url: str | None = None,
) -> dict[str, object]:
    slot_name = str(slot).strip().upper()
    slot_dir = Path(slot_dir_path).expanduser().resolve()
    slot_dir.mkdir(parents=True, exist_ok=True)
    target_rev = str(target_rev or "").strip()
    repo_url = str(repo_url or os.getenv("ADAOS_CORE_UPDATE_REPO_URL", "https://github.com/stipot-com/adaos.git")).strip()
    source_repo_dir = Path(str(source_repo_root or "")).expanduser().resolve() if str(source_repo_root or "").strip() else None
    shared_dotenv = str(shared_dotenv_path or "").strip()
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"adaos-core-{slot_name.lower()}-", dir=str(slot_dir.parent)))
    prepared_slot = tmp_dir / slot_name
    prepared_slot.mkdir(parents=True, exist_ok=True)
    try:
        checkout_tmp = prepared_slot / "repo"
        source_is_git = _is_git_repo(source_repo_dir)
        git_available = bool(shutil.which("git"))
        if source_repo_dir is not None and source_repo_dir.exists() and source_is_git:
            _clone_local_repo(source_repo_dir, target_rev, checkout_tmp)
        elif git_available and repo_url:
            _clone_repo(repo_url, target_rev, checkout_tmp)
        elif source_repo_dir is not None and source_repo_dir.exists():
            _clone_local_repo(source_repo_dir, target_rev, checkout_tmp)
        else:
            _clone_repo(repo_url, target_rev, checkout_tmp)
        venv_tmp = prepared_slot / "venv"
        _run([sys.executable, "-m", "venv", str(venv_tmp)])
        py = _venv_python(venv_tmp)
        _run([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        _run([str(py), "-m", "pip", "install", str(checkout_tmp)])

        final_repo_dir = slot_dir / "repo"
        final_venv_dir = slot_dir / "venv"
        final_py = _venv_python(final_venv_dir)
        manifest = {
            "slot": slot_name,
            "created_at": time.time(),
            "target_rev": target_rev,
            "target_version": str(target_version or "").strip(),
            "repo_url": repo_url,
            "repo_dir": str(final_repo_dir),
            "venv_dir": str(final_venv_dir),
            "cwd": str(final_repo_dir),
            "argv": [
                str(final_py),
                "-m",
                "adaos.apps.autostart_runner",
                "--host",
                "{host}",
                "--port",
                "{port}",
            ],
            "env": {
                "ADAOS_BASE_DIR": str(base_dir or ""),
                "ADAOS_SLOT_REPO_ROOT": str(final_repo_dir),
                "ADAOS_SHARED_DOTENV_PATH": shared_dotenv,
                "PYTHONPATH": str(final_repo_dir / "src"),
                "PYTHONUNBUFFERED": "1",
            },
        }
        if os.path.exists(str(slot_dir)):
            shutil.rmtree(slot_dir, ignore_errors=True)
        shutil.move(str(prepared_slot), str(slot_dir))
        write_slot_manifest(slot_name, manifest)
        return manifest
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _prepare_slot(args: argparse.Namespace) -> dict[str, object]:
    return prepare_slot(
        slot=args.slot,
        slot_dir_path=args.slot_dir,
        base_dir=args.base_dir,
        repo_root=args.repo_root,
        source_repo_root=args.source_repo_root,
        shared_dotenv_path=args.shared_dotenv_path,
        target_rev=args.target_rev,
        target_version=args.target_version,
        repo_url=args.repo_url,
    )


def main() -> None:
    try:
        init_ctx()
    except Exception:
        pass
    args = _parse_args()
    manifest = _prepare_slot(args)
    print(json.dumps({"ok": True, "slot": args.slot, "manifest": manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
