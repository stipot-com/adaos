from __future__ import annotations

import asyncio
import builtins
import sys
import types
from pathlib import Path

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.services.skills_loader_importlib import ImportlibSkillsLoader


def test_importlib_loader_loads_skill_data_projections(tmp_path, monkeypatch) -> None:
    skill_dir = tmp_path / "infrastate_skill"
    handlers_dir = skill_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    (handlers_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        """
name: infrastate_skill
version: "0.1.0"
data_projections:
  - scope: subnet
    slot: infrastate.snapshot
    targets:
      - backend: yjs
        path: data/infrastate
""".strip(),
        encoding="utf-8",
    )

    loaded_entries: list[list[dict]] = []

    class _Projections:
        def load_entries(self, entries):
            loaded_entries.append(list(entries))

    class _Ctx:
        projections = _Projections()

    loader = ImportlibSkillsLoader()
    monkeypatch.setattr(loader, "_sync_runtime_from_workspace_if_debug", lambda root: None)
    monkeypatch.setattr(loader, "_load_handler", lambda handler: None)
    monkeypatch.setattr("adaos.services.skills_loader_importlib.get_ctx", lambda: _Ctx())

    asyncio.run(loader.import_all_handlers(tmp_path))

    assert loaded_entries
    assert loaded_entries[0][0]["slot"] == "infrastate.snapshot"


def test_importlib_loader_does_not_reexecute_same_handler_module(tmp_path: Path) -> None:
    skill_dir = tmp_path / "repeat_skill"
    handlers_dir = skill_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    handler = handlers_dir / "main.py"
    handler.write_text(
        "\n".join(
            [
                "import builtins",
                "builtins._adaos_repeat_import_counter = getattr(builtins, '_adaos_repeat_import_counter', 0) + 1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    loader = ImportlibSkillsLoader()
    mod_name = "adaos_skill_" + handler.parent.as_posix().replace("/", "_")
    sys.modules.pop(mod_name, None)
    if hasattr(builtins, "_adaos_repeat_import_counter"):
        delattr(builtins, "_adaos_repeat_import_counter")
    try:
        loader._load_handler(handler)
        loader._load_handler(handler)
        assert getattr(builtins, "_adaos_repeat_import_counter", 0) == 1
    finally:
        sys.modules.pop(mod_name, None)
        if hasattr(builtins, "_adaos_repeat_import_counter"):
            delattr(builtins, "_adaos_repeat_import_counter")
