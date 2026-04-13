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

from adaos.services.bootstrap_update import BOOTSTRAP_CRITICAL_PATHS
from adaos.services.core_slots import write_slot_manifest


def _is_probably_git_sha(value: str) -> bool:
    token = str(value or "").strip()
    if len(token) < 7 or len(token) > 40:
        return False
    for ch in token:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _checkout_target_version(repo_dir: Path, *, target_rev: str, target_version: str) -> None:
    """
    Ensure the checkout is at the requested git commit-ish when target_version looks like a SHA.

    This prevents "partial update" situations where a branch tip moves (or is different
    from what the update coordinator expects) while the update runner still prepares a slot.
    """
    target_version = str(target_version or "").strip()
    if not _is_probably_git_sha(target_version):
        return
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required for core updates but is not installed")
    try:
        _run([git, "checkout", target_version], cwd=repo_dir)
        return
    except Exception:
        # Shallow clones may not contain the commit object even if the branch was specified.
        # Fetch more history for the target branch and retry.
        if target_rev:
            _run([git, "fetch", "--depth", "50", "origin", target_rev], cwd=repo_dir)
        else:
            _run([git, "fetch", "--depth", "50", "origin"], cwd=repo_dir)
        _run([git, "checkout", target_version], cwd=repo_dir)


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


def _run_json(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, object]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed rc={completed.returncode}: {' '.join(cmd)}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"command returned invalid JSON: {' '.join(cmd)}\nstdout:\n{completed.stdout[-4000:]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"command returned non-object JSON: {' '.join(cmd)}")
    return payload


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _rewrite_text_file(path: Path, *, old: str, new: str) -> bool:
    try:
        raw = path.read_bytes()
    except Exception:
        return False
    if b"\x00" in raw:
        return False
    try:
        text = raw.decode("utf-8")
    except Exception:
        return False
    if old not in text:
        return False
    path.write_text(text.replace(old, new), encoding="utf-8")
    return True


def _repair_moved_venv(venv_dir: Path, *, original_venv_dir: Path) -> dict[str, object]:
    repaired: list[str] = []
    if os.name == "nt":
        scripts_dir = venv_dir / "Scripts"
    else:
        scripts_dir = venv_dir / "bin"
    old_text = str(original_venv_dir)
    new_text = str(venv_dir)
    if scripts_dir.exists():
        for child in scripts_dir.iterdir():
            if not child.is_file():
                continue
            if _rewrite_text_file(child, old=old_text, new=new_text):
                repaired.append(str(child))
    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    if pyvenv_cfg.exists() and _rewrite_text_file(pyvenv_cfg, old=old_text, new=new_text):
        repaired.append(str(pyvenv_cfg))
    return {
        "ok": True,
        "venv_dir": str(venv_dir),
        "original_venv_dir": str(original_venv_dir),
        "repaired_files": repaired,
    }


def _replace_slot_dir(prepared_slot: Path, slot_dir: Path) -> None:
    if slot_dir.exists():
        shutil.rmtree(slot_dir, ignore_errors=True)
    if slot_dir.exists():
        raise RuntimeError(
            f"slot directory cleanup failed; refusing nested move into existing path: {slot_dir}"
        )
    shutil.move(str(prepared_slot), str(slot_dir))


def _migrate_installed_skill_runtimes(
    python_executable: Path,
    *,
    repo_root: str | os.PathLike[str] = "",
    base_dir: str | os.PathLike[str] = "",
    shared_dotenv_path: str | os.PathLike[str] = "",
    run_tests: bool = True,
) -> dict[str, object]:
    env = dict(os.environ)
    repo_root_path = Path(str(repo_root or "")).expanduser().resolve() if str(repo_root or "").strip() else None
    if str(base_dir or "").strip():
        env["ADAOS_BASE_DIR"] = str(base_dir)
    if str(shared_dotenv_path or "").strip():
        env["ADAOS_SHARED_DOTENV_PATH"] = str(shared_dotenv_path)
    if repo_root_path is not None:
        env["ADAOS_SLOT_REPO_ROOT"] = str(repo_root_path)
        python_entries = [str(repo_root_path / "src")]
        existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
        if existing_pythonpath:
            python_entries.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(entry for entry in python_entries if str(entry).strip()))
    migrate_script = repo_root_path / "src" / "adaos" / "apps" / "skill_runtime_migrate.py" if repo_root_path is not None else None
    if migrate_script is not None:
        if not migrate_script.exists():
            apps_dir = migrate_script.parent
            visible = []
            if apps_dir.exists():
                try:
                    visible = sorted(child.name for child in apps_dir.iterdir() if child.is_file())[:20]
                except Exception:
                    visible = []
            raise RuntimeError(
                "prepared slot repo is missing skill runtime migration entrypoint: "
                f"{migrate_script} "
                f"(repo_root={repo_root_path}, apps_dir_exists={apps_dir.exists()}, visible_files={visible})"
            )
        cmd = [str(python_executable), str(migrate_script), "--json"]
    else:
        cmd = [str(python_executable), "-m", "adaos.apps.skill_runtime_migrate", "--json"]
    if not run_tests:
        cmd.append("--skip-tests")
    return _run_json(
        cmd,
        cwd=repo_root_path,
        env=env,
    )


def _clone_repo(repo_url: str, target_rev: str, target_version: str, checkout_dir: Path) -> None:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required for core updates but is not installed")
    cmd = [git, "clone", "--depth", "1"]
    if target_rev:
        cmd.extend(["--branch", target_rev])
    cmd.extend([repo_url, str(checkout_dir)])
    _run(cmd)
    _checkout_target_version(checkout_dir, target_rev=target_rev, target_version=target_version)


def _clone_local_repo(source_repo_root: Path, target_rev: str, target_version: str, checkout_dir: Path) -> None:
    git = shutil.which("git")
    git_dir = source_repo_root / ".git"
    if git and git_dir.exists():
        try:
            _run([git, "clone", str(source_repo_root), str(checkout_dir)])
            if target_rev:
                _run([git, "checkout", target_rev], cwd=checkout_dir)
            _checkout_target_version(checkout_dir, target_rev=target_rev, target_version=target_version)
            return
        except Exception:
            pass
    shutil.copytree(
        source_repo_root,
        checkout_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
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


def _validate_checkout_target_version(repo_dir: Path, *, target_version: str, source_label: str) -> None:
    target_version = str(target_version or "").strip()
    if not _is_probably_git_sha(target_version):
        return
    actual = _git_text(repo_dir, "rev-parse", "HEAD")
    if not actual:
        raise RuntimeError(
            f"{source_label} did not produce a verifiable git checkout for requested target_version {target_version}"
        )
    if actual.lower() != target_version.lower():
        raise RuntimeError(
            f"{source_label} resolved to git commit {actual} instead of requested target_version {target_version}"
        )


def _prepare_checkout_repo(
    *,
    checkout_dir: Path,
    source_repo_dir: Path | None,
    repo_url: str,
    target_rev: str,
    target_version: str,
) -> str:
    git_available = bool(shutil.which("git"))
    source_exists = source_repo_dir is not None and source_repo_dir.exists()
    source_is_git = _is_git_repo(source_repo_dir)
    local_error: Exception | None = None

    if source_exists and source_is_git and source_repo_dir is not None:
        try:
            _clone_local_repo(source_repo_dir, target_rev, target_version, checkout_dir)
            _validate_checkout_target_version(
                checkout_dir,
                target_version=target_version,
                source_label="local source repo",
            )
            return "local_source_tree"
        except Exception as exc:
            local_error = exc
            shutil.rmtree(checkout_dir, ignore_errors=True)

    if git_available and repo_url:
        try:
            _clone_repo(repo_url, target_rev, target_version, checkout_dir)
            _validate_checkout_target_version(
                checkout_dir,
                target_version=target_version,
                source_label="remote repo clone",
            )
            return "remote_git_clone"
        except Exception as exc:
            if local_error is not None:
                raise RuntimeError(
                    f"failed to prepare requested target_version {target_version or '<unspecified>'}: "
                    f"local source repo failed ({local_error}); remote repo clone failed ({exc})"
                ) from exc
            raise

    if source_exists and source_repo_dir is not None:
        _clone_local_repo(source_repo_dir, target_rev, target_version, checkout_dir)
        _validate_checkout_target_version(
            checkout_dir,
            target_version=target_version,
            source_label="copied local source tree",
        )
        return "local_source_tree"

    _clone_repo(repo_url, target_rev, target_version, checkout_dir)
    _validate_checkout_target_version(
        checkout_dir,
        target_version=target_version,
        source_label="remote repo clone",
    )
    return "remote_git_clone"


def _strip_repo_vcs_metadata(repo_dir: Path) -> None:
    git_dir = repo_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)


def _path_content_differs(left: Path, right: Path) -> bool:
    left_exists = left.exists()
    right_exists = right.exists()
    if left_exists != right_exists:
        return True
    if not left_exists:
        return False
    if left.is_dir() or right.is_dir():
        return left.is_dir() != right.is_dir()
    try:
        return left.read_bytes() != right.read_bytes()
    except Exception:
        return True


def _detect_bootstrap_promotion_requirement(candidate_repo_dir: Path, repo_root: Path | None) -> dict[str, object]:
    checked_paths = list(BOOTSTRAP_CRITICAL_PATHS)
    if repo_root is None or not repo_root.exists():
        return {
            "required": False,
            "basis": "repo_root_unavailable",
            "checked_paths": checked_paths,
            "changed_paths": [],
        }
    changed_paths: list[str] = []
    for rel_path in checked_paths:
        if _path_content_differs(candidate_repo_dir / rel_path, repo_root / rel_path):
            changed_paths.append(rel_path)
    return {
        "required": bool(changed_paths),
        "basis": "path_compare",
        "checked_paths": checked_paths,
        "changed_paths": changed_paths,
    }


def _is_git_repo(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return (path / ".git").exists()
    except Exception:
        return False


def _git_text(repo_dir: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git or not _is_git_repo(repo_dir):
        return ""
    try:
        completed = subprocess.run(
            [git, "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


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
    migrate_skill_runtimes: bool = True,
) -> dict[str, object]:
    slot_name = str(slot).strip().upper()
    slot_dir = Path(slot_dir_path).expanduser().resolve()
    slot_dir.mkdir(parents=True, exist_ok=True)
    repo_root_dir = Path(str(repo_root or "")).expanduser().resolve() if str(repo_root or "").strip() else None
    target_rev = str(target_rev or "").strip()
    target_version = str(target_version or "").strip()
    repo_url = str(repo_url or os.getenv("ADAOS_CORE_UPDATE_REPO_URL", "https://github.com/stipot-com/adaos.git")).strip()
    source_repo_dir = Path(str(source_repo_root or "")).expanduser().resolve() if str(source_repo_root or "").strip() else None
    shared_dotenv = str(shared_dotenv_path or "").strip()
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"adaos-core-{slot_name.lower()}-", dir=str(slot_dir.parent)))
    prepared_slot = tmp_dir / slot_name
    prepared_slot.mkdir(parents=True, exist_ok=True)
    try:
        checkout_tmp = prepared_slot / "repo"
        source_kind = _prepare_checkout_repo(
            checkout_dir=checkout_tmp,
            source_repo_dir=source_repo_dir,
            repo_url=repo_url,
            target_rev=target_rev,
            target_version=target_version,
        )
        venv_tmp = prepared_slot / "venv"
        _run([sys.executable, "-m", "venv", str(venv_tmp)])
        py = _venv_python(venv_tmp)
        _run([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        _run([str(py), "-m", "pip", "install", str(checkout_tmp)])

        final_repo_dir = slot_dir / "repo"
        final_venv_dir = slot_dir / "venv"
        original_venv_dir = venv_tmp.resolve()
        final_py = _venv_python(final_venv_dir)
        git_commit = _git_text(checkout_tmp, "rev-parse", "HEAD")
        git_short_commit = _git_text(checkout_tmp, "rev-parse", "--short", "HEAD")
        git_branch = _git_text(checkout_tmp, "rev-parse", "--abbrev-ref", "HEAD")
        git_subject = _git_text(checkout_tmp, "show", "-s", "--format=%s", "HEAD")
        bootstrap_update = _detect_bootstrap_promotion_requirement(checkout_tmp, repo_root_dir)
        _strip_repo_vcs_metadata(checkout_tmp)
        manifest = {
            "slot": slot_name,
            "created_at": time.time(),
            "target_rev": target_rev,
            "target_version": str(target_version or "").strip(),
            "root_repo_root": str(repo_root_dir) if repo_root_dir is not None else "",
            "source_kind": source_kind,
            "source_repo_root": str(source_repo_dir) if source_repo_dir is not None else "",
            "repo_url": repo_url,
            "repo_dir": str(final_repo_dir),
            "venv_dir": str(final_venv_dir),
            "git_commit": git_commit,
            "git_short_commit": git_short_commit,
            "git_branch": git_branch,
            "git_subject": git_subject,
            "bootstrap_update": bootstrap_update,
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
        _replace_slot_dir(prepared_slot, slot_dir)
        repair = _repair_moved_venv(final_venv_dir, original_venv_dir=original_venv_dir)
        manifest["venv_repair"] = repair
        if migrate_skill_runtimes:
            skill_runtime_migration = _migrate_installed_skill_runtimes(
                final_py,
                repo_root=str(final_repo_dir),
                base_dir=str(base_dir or ""),
                shared_dotenv_path=shared_dotenv,
                run_tests=True,
            )
            if not bool(skill_runtime_migration.get("ok")):
                failed = []
                for item in skill_runtime_migration.get("skills") or []:
                    if not isinstance(item, dict) or bool(item.get("ok")):
                        continue
                    failed.append(
                        f"{item.get('skill') or 'skill'}:{item.get('failed_stage') or 'failed'}"
                    )
                suffix = ", ".join(failed[:5])
                if len(failed) > 5:
                    suffix += f" (+{len(failed) - 5} more)"
                if suffix:
                    raise RuntimeError(f"installed skill runtime migration failed: {suffix}")
                raise RuntimeError(
                    f"installed skill runtime migration failed: {json.dumps(skill_runtime_migration, ensure_ascii=False)}"
                )
        else:
            skill_runtime_migration = {
                "ok": True,
                "total": 0,
                "failed_total": 0,
                "rollback_total": 0,
                "deactivated_total": 0,
                "run_tests": False,
                "deferred": True,
                "skills": [],
            }
        manifest["skill_runtime_migration"] = skill_runtime_migration
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
    args = _parse_args()
    manifest = _prepare_slot(args)
    print(json.dumps({"ok": True, "slot": args.slot, "manifest": manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
