# src/adaos/services/interpreter/trainer.py
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from adaos.services.interpreter.workspace import InterpreterWorkspace


class RasaTrainer:
    """
    Handles Rasa training in an isolated Python 3.10 virtualenv.
    """

    def __init__(self, workspace: InterpreterWorkspace, *, python_spec: str = "3.10", rasa_version: str = "3.6.20"):
        self.ws = workspace
        self.python_spec = python_spec
        self.rasa_version = rasa_version
        self.env_dir = self.ws.root / ".rasa-venv"
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = Path(self.ws.context.paths.models_dir()) / "interpreter"
        self.models_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- helpers
    def _venv_python(self) -> Path:
        return self.env_dir / "Scripts" / "python.exe"

    def _venv_pip(self) -> Path:
        return self.env_dir / "Scripts" / "pip.exe"

    def _venv_rasa(self) -> Path:
        # Windows installs rasa.exe; fallback to python -m rasa otherwise
        exe = self.env_dir / "Scripts" / "rasa.exe"
        if exe.exists():
            return exe
        return self._venv_python()

    def _run(self, cmd: list[str], *, cwd: Optional[Path] = None) -> None:
        subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)

    def _ensure_env(self) -> None:
        if not self._venv_python().exists():
            self._run(["py", f"-{self.python_spec}", "-m", "venv", str(self.env_dir)])

    def _ensure_rasa_installed(self) -> None:
        self._ensure_env()
        python = self._venv_python()
        self._run([str(python), "-m", "pip", "install", "-U", "pip"])
        # Check whether rasa already installed with desired version
        try:
            subprocess.run(
                [str(python), "-m", "pip", "show", "rasa"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            pass
        self._run([str(python), "-m", "pip", "install", f"rasa=={self.rasa_version}"])

    # ---------------------------------------------------------------- training
    def train(self, *, note: Optional[str] = None) -> dict:
        project = self.ws.build_rasa_project()
        self._ensure_rasa_installed()
        rasa_exec = self._venv_rasa()
        cmd = [str(rasa_exec)]
        if rasa_exec.name == "python.exe":
            cmd += ["-m", "rasa"]
        cmd += [
            "train",
            "nlu",
            "--fixed-model-name",
            "interpreter_latest",
            "--out",
            str(self.models_dir),
        ]
        self._run(cmd, cwd=project)
        model_path = self.models_dir / "interpreter_latest.tar.gz"
        meta = self.ws.record_training(note=note or "rasa-train", extra={"model_path": str(model_path)})
        return meta
