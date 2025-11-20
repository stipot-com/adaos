import json
import sys
from pathlib import Path

import pytest

from adaos.sdk.core._ctx import require_ctx
from adaos.services.testing.bootstrap import (
    bootstrap_test_ctx,
    load_test_secrets,
    mount_skill_paths_for_testing,
    skill_tests_root,
    unmount_skill_paths,
)
from adaos.services.skill.tests_entry import main as run_skill_tests_entry


@pytest.fixture
def skill_slot(tmp_path: Path) -> Path:
    base = tmp_path / ".adaos"
    slot = base / "workspace" / "skills" / ".runtime" / "demo" / "1.0.0" / "slots" / "current"
    (slot / "vendor").mkdir(parents=True, exist_ok=True)
    namespace_root = slot / "src" / "skills" / "demo"
    namespace_root.mkdir(parents=True, exist_ok=True)
    (namespace_root / "tests").mkdir(parents=True, exist_ok=True)
    return slot


def test_mount_skill_paths_for_testing_prioritises_vendor(skill_slot):
    vendor = str((skill_slot / "vendor").resolve())
    src = str((skill_slot / "src").resolve())
    stale = str((skill_slot.parent / "A" / "src").resolve())

    original = list(sys.path)
    inserted: list[str] = []
    try:
        sys.path.insert(0, stale)
        inserted = mount_skill_paths_for_testing("demo", "1.0.0", skill_slot)
        assert inserted == [vendor, src]
        assert sys.path[0] == vendor
        assert sys.path[1] == src
        assert stale not in sys.path
    finally:
        unmount_skill_paths(inserted)
        sys.path[:] = original


def test_load_test_secrets_merges_sources(skill_slot, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "env")
    tests_root = skill_tests_root(skill_slot, "demo")
    (tests_root / ".env").write_text("DEMO_API_KEY=dot\nPLAIN=value\n", encoding="utf-8")
    config = {"DEMO_API_KEY": "json", "API_KEY": "cfg"}
    (tests_root / "test.config.json").write_text(json.dumps(config), encoding="utf-8")

    secrets = load_test_secrets(skill_slot, skill_name="demo", env_prefix="DEMO_")
    assert secrets["DEMO_API_KEY"] == "json"
    assert secrets["PLAIN"] == "value"
    assert secrets["API_KEY"] == "cfg"


def test_bootstrap_test_ctx_provides_runtime(monkeypatch, skill_slot):
    base = skill_slot.parents[6]
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base))
    secrets_map = {"TOKEN": "123"}

    original_ctx = require_ctx()

    handle = bootstrap_test_ctx(skill_name="demo", skill_slot_dir=skill_slot, secrets=secrets_map)
    try:
        from adaos.sdk.data import secrets as sdk_secrets

        assert sdk_secrets.get("TOKEN") == "123"
        current = handle.ctx.skill_ctx.get()
        assert current.name == "demo"
        assert Path(current.path).resolve() == skill_slot.resolve()
    finally:
        handle.teardown()

    assert require_ctx() is original_ctx


def test_tests_entry_executes_script_with_context(monkeypatch, skill_slot):
    base = skill_slot.parents[6]
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base))

    namespace_root = skill_slot / "src" / "skills" / "demo"
    handlers = namespace_root / "handlers"
    handlers.mkdir(parents=True, exist_ok=True)
    (namespace_root / "__init__.py").write_text("", encoding="utf-8")
    (handlers / "__init__.py").write_text("from .main import handle\n", encoding="utf-8")
    (handlers / "main.py").write_text(
        "def handle(topic, payload):\n    return {'status': 'ok'}\n",
        encoding="utf-8",
    )

    tests_root = skill_tests_root(skill_slot, "demo")
    (tests_root / ".env").write_text("API_KEY=from_env\n", encoding="utf-8")
    script = tests_root / "smoke" / "test_script.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "from adaos.sdk.data import secrets\n"
        "from skills.demo.handlers.main import handle\n"
        "assert secrets.get('API_KEY') == 'from_env'\n"
        "assert handle('ping', {})['status'] == 'ok'\n",
        encoding="utf-8",
    )

    original_ctx = require_ctx()
    before_paths = set(sys.path)
    exit_code = run_skill_tests_entry(
        [
            "--skill-name",
            "demo",
            "--skill-version",
            "1.0.0",
            "--slot-dir",
            str(skill_slot),
            "--",
            str(script),
        ]
    )
    assert exit_code == 0

    assert require_ctx() is original_ctx

    src_dir = str((skill_slot / "src").resolve())
    vendor_dir = str((skill_slot / "vendor").resolve())
    assert src_dir not in sys.path
    assert vendor_dir not in sys.path
    assert set(sys.path) >= before_paths
