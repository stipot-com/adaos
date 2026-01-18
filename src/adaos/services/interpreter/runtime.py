from __future__ import annotations

"""
Runtime helpers for executing the trained interpreter model (Rasa-based).

This module mirrors environment/layout assumptions of RasaTrainer, but prefers
to load the trained model into the main AdaOS process (using the root venv)
and only falls back to the dedicated `.rasa-venv` via subprocess when
in-process loading is not possible.
"""

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from adaos.services.interpreter.workspace import InterpreterWorkspace


_log = logging.getLogger("adaos.interpreter.runtime")


class RasaNLURuntime:
    """
    Thin wrapper around a Rasa NLU model trained via RasaTrainer.

    It reuses the same virtualenv layout and models directory but loads the
    trained model into the current process and keeps it cached between calls.
    """

    def __init__(self, workspace: InterpreterWorkspace, *, python_spec: str = "3.10") -> None:
        self.ws = workspace
        self.python_spec = python_spec
        # Keep layout consistent with RasaTrainer (used for training and
        # subprocess-based fallback). The in-process path expects Rasa to be
        # installed into the root AdaOS venv.
        self.env_dir = self.ws.root / ".rasa-venv"
        self.models_dir = Path(self.ws.context.paths.models_dir()) / "interpreter"
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # Cached Rasa objects
        self._interpreter: Any | None = None
        self._agent: Any | None = None
        self._loaded_model_path: Optional[Path] = None

    # ------------------------------------------------------------------ helpers
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

    def _venv_python(self) -> Path:
        return self.env_dir / "Scripts" / "python.exe"

    def _ensure_model_loaded(self) -> None:
        """
        Lazily import Rasa from the *root* AdaOS environment and load the
        interpreter/agent model once.

        This assumes that `rasa` is installed into the same venv where the
        main AdaOS process is running. If that is not the case, this method
        will raise and the caller should fall back to subprocess-based
        execution via `_parse_via_subprocess`.
        """
        model_path = self._pick_model_path()
        if self._loaded_model_path == model_path and (self._interpreter or self._agent):
            return

        # Try legacy NLU Interpreter API first (Rasa 2.x style)
        try:
            from rasa.nlu.model import Interpreter  # type: ignore[import]

            self._interpreter = Interpreter.load(str(model_path))
            self._agent = None
            self._loaded_model_path = model_path
            _log.info("Rasa Interpreter model loaded from %s", model_path)
            return
        except Exception as exc:
            _log.debug("Failed to load Rasa Interpreter model: %s", exc, exc_info=True)

        # Fallback to Agent-based API (Rasa 3.x)
        try:
            from rasa.core.agent import Agent  # type: ignore[import]

            self._agent = Agent.load(str(model_path))
            self._interpreter = None
            self._loaded_model_path = model_path
            _log.info("Rasa Agent model loaded from %s", model_path)
            return
        except Exception as exc:
            _log.error("Failed to load Rasa model from %s: %s", model_path, exc, exc_info=True)
            raise RuntimeError(f"Failed to load Rasa model from {model_path}: {exc}") from exc

    def _parse_via_subprocess(self, text: str) -> Dict[str, Any]:
        """
        Fallback path: run parsing inside the interpreter venv via a short
        helper script. Kept for environments where importing Rasa into the
        main process is not possible (e.g. Python version mismatch).
        """
        model_path = self._pick_model_path()
        python = sys.executable

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

        output = stdout_bytes.decode("utf-8", errors="ignore").strip()
        if not output:
            err_snippet = stderr_bytes.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"Interpreter returned empty output; stderr={err_snippet[:400]}")

        json_line: str | None = None
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if candidate.startswith("{") or candidate.startswith("["):
                json_line = candidate
                break

        if not json_line:
            raise RuntimeError(f"Interpreter output did not contain JSON payload: {output[:400]}")

        return json.loads(json_line)

    # --------------------------------------------------------------------- API
    def parse(self, text: str) -> Dict[str, Any]:
        """
        Run Rasa's Interpreter/Agent parse on the given text and return JSON.

        Heavy imports and model loading are cached; callers are expected to
        run this in a worker thread (see router_runtime).
        """
        if not text or not isinstance(text, str):
            raise ValueError("text must be a non-empty string")

        try:
            self._ensure_model_loaded()
            if self._interpreter is not None:
                result = self._interpreter.parse(text)
            elif self._agent is not None:
                # Agent.parse_message is async in modern Rasa versions.
                async def _run_agent() -> Dict[str, Any]:
                    return await self._agent.parse_message(text)  # type: ignore[no-untyped-call]

                result = asyncio.run(_run_agent())
            else:  # pragma: no cover - defensive guard
                raise RuntimeError("Rasa model is not loaded")
        except Exception as exc:
            _log.warning("In-process Rasa parse failed, falling back to subprocess: %s", exc, exc_info=True)
            result = self._parse_via_subprocess(text)

        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected interpreter result type: {type(result)!r}")

        return result
