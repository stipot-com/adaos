from __future__ import annotations

from pathlib import Path

from adaos.apps.autostart_runner import _slot_launch_spec
from adaos.services.autostart import default_spec


class _FakePaths:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def base_dir(self) -> Path:
        return self._base_dir


class _FakeSettings:
    profile = "default"


class _FakeCtx:
    def __init__(self, base_dir: Path) -> None:
        self.paths = _FakePaths(base_dir)
        self.settings = _FakeSettings()


def test_default_autostart_spec_uses_runner(tmp_path: Path) -> None:
    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8779, token="t1")
    assert spec.argv[:3] == (spec.argv[0], "-m", "adaos.apps.autostart_runner")
    assert "--host" in spec.argv
    assert "--port" in spec.argv
    assert spec.env["ADAOS_BASE_DIR"] == str(tmp_path)
    assert spec.env["ADAOS_PROFILE"] == "default"
    assert spec.env["ADAOS_TOKEN"] == "t1"


def test_slot_launch_spec_formats_placeholders() -> None:
    argv, command = _slot_launch_spec(
        {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
        },
        host="127.0.0.1",
        port=8777,
        token="tok",
    )
    assert command is None
    assert argv is not None
    assert argv[-1] == "8777"
