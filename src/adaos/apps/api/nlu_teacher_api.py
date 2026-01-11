# src/adaos/apps/api/nlu_teacher_api.py
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

router = APIRouter(tags=["nlu-teacher"])


def _resolve_webspace_id(token: Optional[str]) -> str:
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _teacher_obj(data_map: Any) -> dict:
    current = data_map.get("nlu_teacher")
    return dict(current) if isinstance(current, dict) else {}


class ApplyRevisionRequest(BaseModel):
    revision_id: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)
    examples: list[str] = Field(default_factory=list)
    slots: Dict[str, Any] = Field(default_factory=dict)


@router.get("/nlu/teacher/{webspace_id}", dependencies=[Depends(require_token)])
async def get_teacher_state(webspace_id: str):
    ws = _resolve_webspace_id(webspace_id)
    async with async_get_ydoc(ws) as ydoc:
        data_map = ydoc.get_map("data")
        return {"webspace_id": ws, "nlu_teacher": _teacher_obj(data_map)}


@router.post("/nlu/teacher/{webspace_id}/revision/apply", dependencies=[Depends(require_token)])
async def apply_revision(webspace_id: str, body: ApplyRevisionRequest):
    ws = _resolve_webspace_id(webspace_id)
    ctx = get_ctx()

    examples = [x.strip() for x in (body.examples or []) if isinstance(x, str) and x.strip()]
    payload = {
        "webspace_id": ws,
        "revision_id": body.revision_id.strip(),
        "intent": body.intent.strip(),
        "examples": examples,
        "slots": dict(body.slots or {}),
        "_meta": {"webspace_id": ws},
    }

    try:
        bus_emit(ctx.bus, "nlp.teacher.revision.apply", payload, source="api.nlu.teacher")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to emit apply event: {exc}")

    return {"ok": True, "webspace_id": ws, "revision_id": body.revision_id, "intent": body.intent}

