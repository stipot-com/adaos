from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict
from urllib.request import Request, urlopen

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.interpreter.workspace import InterpreterWorkspace
from adaos.services.nlu.data_registry import sync_from_scenarios_and_skills
from adaos.services.skill.service_supervisor import get_service_supervisor

_log = logging.getLogger("adaos.nlu.rasa.train")


def _http_post_json(url: str, payload: dict, *, timeout_ms: int = 600_000) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


def _train_sync(ctx) -> dict:
    # 1) Sync NLU data into interpreter workspace files (pure-Python).
    sync_from_scenarios_and_skills(ctx)
    ws = InterpreterWorkspace(ctx)
    project = ws.build_rasa_project()

    models_dir = Path(ctx.paths.models_dir()) / "interpreter"
    models_dir.mkdir(parents=True, exist_ok=True)
    return {"project_dir": str(project), "out_dir": str(models_dir)}


async def _train_if_enabled(reason: str) -> None:
    if os.getenv("ADAOS_NLU_AUTOTRAIN") != "1":
        return

    ctx = get_ctx()
    supervisor = get_service_supervisor()
    base_url = supervisor.resolve_base_url("rasa_nlu_service_skill")
    if not base_url:
        _log.warning("rasa service is not configured/installed; skip train")
        return

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _train_sync, ctx)
    try:
        resp = await loop.run_in_executor(None, _http_post_json, f"{base_url}/train", payload)
    except Exception:
        _log.warning("rasa training request failed reason=%s", reason, exc_info=True)
        return
    if not isinstance(resp, dict) or not resp.get("ok"):
        _log.warning("rasa training failed reason=%s resp=%r", reason, resp)
        return
    _log.info("rasa trained reason=%s", reason)


@subscribe("scenarios.synced")
async def _on_scenarios_synced(_: Dict[str, Any]) -> None:
    await _train_if_enabled("scenarios.synced")


@subscribe("skills.activated")
async def _on_skills_activated(_: Dict[str, Any]) -> None:
    await _train_if_enabled("skills.activated")


@subscribe("skills.rolledback")
async def _on_skills_rolledback(_: Dict[str, Any]) -> None:
    await _train_if_enabled("skills.rolledback")


@subscribe("desktop.webspace.reload")
async def _on_webspace_reload(_: Dict[str, Any]) -> None:
    await _train_if_enabled("desktop.webspace.reload")


@subscribe("nlp.rasa.train")
async def _on_manual_train(_: Any) -> None:
    await _train_if_enabled("manual")

