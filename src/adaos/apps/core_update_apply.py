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

from adaos.services.core_slots import write_slot_manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare inactive AdaOS core slot")
    parser.add_argument("--target-rev", default="")
    parser.add_argument("--target-version", default="")
    parser.add_argument("--slot", required=True)
    parser.add_argument("--slot-dir", required=True)
    parser.add_argument("--base-dir", default="")
    parser.add_argument("--repo-root", default="")
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


def _prepare_slot(args: argparse.Namespace) -> dict[str, object]:
    slot_dir = Path(args.slot_dir).expanduser().resolve()
    slot_dir.mkdir(parents=True, exist_ok=True)
    target_rev = str(args.target_rev or "").strip()
    repo_url = str(args.repo_url or "").strip()
    checkout_dir = slot_dir / "repo"
    venv_dir = slot_dir / "venv"
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"adaos-core-{args.slot.lower()}-", dir=str(slot_dir.parent)))
    prepared_slot = tmp_dir / args.slot.upper()
    prepared_slot.mkdir(parents=True, exist_ok=True)
    try:
        checkout_tmp = prepared_slot / "repo"
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
            "slot": str(args.slot).upper(),
            "created_at": time.time(),
            "target_rev": target_rev,
            "target_version": str(args.target_version or "").strip(),
            "repo_url": repo_url,
            "repo_dir": str(final_repo_dir),
            "venv_dir": str(final_venv_dir),
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
                "ADAOS_BASE_DIR": str(args.base_dir or ""),
                "ADAOS_SLOT_REPO_ROOT": str(final_repo_dir),
                "PYTHONUNBUFFERED": "1",
            },
        }
        if os.path.exists(str(slot_dir)):
            shutil.rmtree(slot_dir, ignore_errors=True)
        shutil.move(str(prepared_slot), str(slot_dir))
        write_slot_manifest(str(args.slot), manifest)
        return manifest
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    args = _parse_args()
    manifest = _prepare_slot(args)
    print(json.dumps({"ok": True, "slot": args.slot, "manifest": manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
