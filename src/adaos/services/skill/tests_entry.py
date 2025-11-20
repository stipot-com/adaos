"""Entry point used by ``adaos skill test`` to bootstrap skill test runs."""

from __future__ import annotations

import argparse
import importlib.util
import runpy
import sys
from pathlib import Path
from typing import Sequence

from adaos.services.skill.runtime import _clear_skill_modules
from adaos.services.testing.bootstrap import (
    bootstrap_test_ctx,
    load_test_secrets,
    mount_skill_paths_for_testing,
    skill_tests_root,
    unmount_skill_paths,
)


def _install_hooks(skill_name: str, slot_dir: Path, ctx) -> None:
    hooks_path = skill_tests_root(slot_dir, skill_name) / "hooks.py"
    if not hooks_path.is_file():
        return
    spec = importlib.util.spec_from_file_location("adaos_skill_test_hooks", hooks_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    installer = getattr(module, "install_extra_caps", None)
    if callable(installer):
        installer(ctx)


def _normalise_target(args: Sequence[str]) -> list[str]:
    target = list(args)
    if target and target[0] == "--":
        target = target[1:]
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute a skill test script with runtime context.")
    parser.add_argument("--skill-name", required=True)
    parser.add_argument("--skill-version", required=True)
    parser.add_argument("--slot-dir", required=True)
    parser.add_argument("--env-prefix", default=None, help="Prefix for environment-derived secrets.")
    parser.add_argument("--pytest", action="store_true", help="Execute pytest instead of a raw script.")
    parser.add_argument("target", nargs=argparse.REMAINDER, help="Target script or pytest arguments.")

    args = parser.parse_args(list(argv) if argv is not None else None)
    target = _normalise_target(args.target)

    slot_dir = Path(args.slot_dir).resolve()
    inserted_paths = mount_skill_paths_for_testing(args.skill_name, args.skill_version, slot_dir)
    env_prefix = args.env_prefix if args.env_prefix is not None else f"{args.skill_name.upper()}_"
    secrets = load_test_secrets(slot_dir, skill_name=args.skill_name, env_prefix=env_prefix)
    ctx_handle = bootstrap_test_ctx(skill_name=args.skill_name, skill_slot_dir=slot_dir, secrets=secrets)

    exit_code = 0
    try:
        _install_hooks(args.skill_name, slot_dir, ctx_handle.ctx)
        if args.pytest:
            import pytest

            exit_code = int(pytest.main(target))
        else:
            if not target:
                return 0
            script = Path(target[0]).resolve()
            script_args = target[1:]
            old_argv = sys.argv[:]
            sys.argv = [str(script), *script_args]
            try:
                runpy.run_path(str(script), run_name="__main__")
            finally:
                sys.argv = old_argv
    finally:
        ctx_handle.teardown()
        unmount_skill_paths(inserted_paths)
        _clear_skill_modules(args.skill_name)

    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover - module behaviour
    sys.exit(main())
