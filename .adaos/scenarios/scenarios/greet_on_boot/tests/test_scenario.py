from __future__ import annotations

from __future__ import annotations

import shutil
from io import StringIO
from pathlib import Path
import sys

from adaos.sdk.data import memory
from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context

SCENARIO_DIR = Path(__file__).resolve().parents[1]
SCENARIO_PATH = SCENARIO_DIR / "scenario.yaml"
REPO_ROOT = Path(__file__).resolve().parents[4]
BASE_DIR = REPO_ROOT / ".adaos"
SKILLS_SOURCE = REPO_ROOT / ".adaos" / "skills" / "weather_skill"


def run_scenario() -> dict:
    ctx = ensure_runtime_context(BASE_DIR)
    skills_dir = Path(ctx.paths.skills_dir())
    target = skills_dir / "weather_skill"
    if not target.exists() and SKILLS_SOURCE.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SKILLS_SOURCE, target)
    runtime = ScenarioRuntime()
    return runtime.run_from_file(str(SCENARIO_PATH))


def test_greet_with_name():
    ensure_runtime_context(BASE_DIR)
    memory.put("user.name", "Ада")

    result = run_scenario()
    msg = result.get("msg")

    assert isinstance(msg, str)
    assert "Ада" in msg

    memory.delete("user.name")


def test_greet_without_name():
    ensure_runtime_context(BASE_DIR)
    memory.delete("user.name")

    backup = sys.stdin
    try:
        sys.stdin = StringIO("ТестИмя\n")
        result = run_scenario()
    finally:
        sys.stdin = backup

    msg = result.get("msg")
    assert isinstance(msg, str)
    assert "ТестИмя" in msg
    assert memory.get("user.name") == "ТестИмя"
