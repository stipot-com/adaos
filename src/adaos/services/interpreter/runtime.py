from __future__ import annotations

"""
Runtime helpers for executing the trained interpreter model (Rasa-based).

This module mirrors environment/layout assumptions of RasaTrainer but only
performs lightweight parsing of text into intents/slots.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from adaos.services.interpreter.workspace import InterpreterWorkspace


class RasaNLURuntime:
    """
    Thin wrapper around a Rasa NLU model trained via RasaTrainer.

    It reuses the same virtualenv layout and models directory but only invokes
    Rasa inside the venv to perform `Interpreter.parse(text)` and returns the
    decoded JSON result.
    """

    def __init__(self, workspace: InterpreterWorkspace, *, python_spec: str = "3.10") -> None:
        self.ws = workspace
        self.python_spec = python_spec
        # Keep layout consistent with RasaTrainer
        self.env_dir = self.ws.root / ".rasa-venv"
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = Path(self.ws.context.paths.models_dir()) / "interpreter"
        self.models_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    def _venv_python(self) -> Path:
        return self.env_dir / "Scripts" / "python.exe"

    def _ensure_env_exists(self) -> None:
        """
        Best-effort guard to make sure the venv exists.
        Does not install Rasa; that is done by RasaTrainer.
        """
        python = self._venv_python()
        if python.exists():
            return
        # Create venv if it was never created; Rasa installation is expected
        # to be handled by training. If Rasa is missing, the parse() call
        # will raise a clear error from the helper script.
        subprocess.run(["py", f"-{self.python_spec}", "-m", "venv", str(self.env_dir)], check=True)

    def _pick_model_path(self) -> Path:
        """
        Resolve the latest trained interpreter model.
        """
        candidate = self.models_dir / "interpreter_latest.tar.gz"
        if candidate.exists():
            return candidate

        # Fallback: pick the most recently modified *.tar.gz in models_dir.
        best: Optional[Path] = None
        for child in self.models_dir.glob("*.tar.gz"):
            if best is None or child.stat().st_mtime > best.stat().st_mtime:
                best = child
        if not best:
            raise RuntimeError("No interpreter model found in models/interpreter; train the model first.")
        return best

    # --------------------------------------------------------------------- API
    def parse(self, text: str) -> Dict[str, Any]:
        """
        Run Rasa's Interpreter.parse(text) inside the venv and return JSON.
        """
        if not text or not isinstance(text, str):
            raise ValueError("text must be a non-empty string")

        self._ensure_env_exists()
        model_path = self._pick_model_path()
        python = self._venv_python()

        helper_code = r"""
import asyncio
import json
import sys

def _parse_with_legacy_interpreter(model_path: str, text: str):
    from rasa.nlu.model import Interpreter  # type: ignore[import]
    interpreter = Interpreter.load(model_path)
    return interpreter.parse(text)

async def _parse_with_agent_async(model_path: str, text: str):
    from rasa.core.agent import Agent  # type: ignore[import]
    agent = Agent.load(model_path)
    # Agent.parse_message is async in modern Rasa versions.
    result = await agent.parse_message(text)
    return result

def _parse(model_path: str, text: str):
    # Try legacy NLU Interpreter API first (Rasa 2.x style).
    try:
        return _parse_with_legacy_interpreter(model_path, text)
    except Exception:
        # Fallback to Agent-based API (Rasa 3.x).
        return asyncio.run(_parse_with_agent_async(model_path, text))

if len(sys.argv) < 3:
    raise SystemExit("Usage: helper.py <model_path> <text>")

model_path = sys.argv[1]
text = sys.argv[2]

result = _parse(model_path, text)
print(json.dumps(result, ensure_ascii=False))
"""

        proc = subprocess.run(
            [str(python), "-c", helper_code, str(model_path), text],
            check=True,
            capture_output=True,
        )
        stdout_bytes = proc.stdout or b""
        stderr_bytes = proc.stderr or b""

        # Rasa / underlying libs can emit non-UTF8 sequences in warnings.
        # Decode defensively and ignore errors in stdout; stderr kept only
        # for diagnostics when JSON is missing.
        output = stdout_bytes.decode("utf-8", errors="ignore").strip()
        if not output:
            # Attach a trimmed stderr snippet to help with debugging.
            err_snippet = stderr_bytes.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"Interpreter returned empty output; stderr={err_snippet[:400]}")

        # Rasa often logs debug lines before the final JSON payload.
        # Take the last line that looks like a JSON object/array.
        json_line: str | None = None
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if candidate.startswith("{") or candidate.startswith("["):
                json_line = candidate
                break

        if not json_line:
            raise RuntimeError(f"Interpreter output did not contain JSON payload: {output[:400]}")

        return json.loads(json_line)
