from __future__ import annotations

import importlib.util

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_ROOT = REPO_ROOT / ".adaos" / "scenarios"


def _collect_test_dirs(root: Path) -> List[Path]:
    paths: List[Path] = []
    if root.is_dir():
        for candidate in root.iterdir():
            tests_dir = candidate / "tests"
            if tests_dir.is_dir():
                if any(tests_dir.glob("test_*.py")):
                    paths.append(tests_dir)
    return paths


def _load_module(path: Path) -> ModuleType:
    module_name = f"adaos.scenario_tests.{path.parent.name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scenario test module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _iter_tests(module: ModuleType) -> Iterable[Callable[[], None]]:
    for name, obj in inspect.getmembers(module):
        if name.startswith("test_") and callable(obj):
            yield obj


_SCENARIO_TEST_MODULES = [
    file
    for tests_dir in _collect_test_dirs(SCENARIOS_ROOT)
    for file in sorted(tests_dir.glob("test_*.py"))
]


@pytest.mark.parametrize("module_path", _SCENARIO_TEST_MODULES, ids=lambda p: p.stem)
def test_scenario_modules(module_path: Path) -> None:
    module = _load_module(module_path)
    for test_fn in _iter_tests(module):
        test_fn()
