from __future__ import annotations
import os
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Request
import hashlib
import json
import time

from adaos.services.agent_context import get_ctx
from adaos.integrations.telegram.webhook import validate_secret
from adaos.integrations.telegram.normalize import to_input_event
from adaos.integrations.telegram.files import get_file_path, download_file, convert_opus_to_wav16k
from adaos.adapters.db import sqlite as sqlite_db
from adaos.services.chat_io import pairing as pairing_svc  # generic pairing
from adaos.services.chat_io.router import resolve_hub_id
from adaos.services.chat_io import telemetry as tm
from dataclasses import asdict
from uuid import uuid4
from datetime import datetime

router = APIRouter()


@router.post("/io/tg/{bot_id}/webhook")
async def telegram_webhook(
    request: Request,
    bot_id: str,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # Settings-based secret (ENV/.env fallback via Settings)
    expected = get_ctx().settings.tg_secret_token or os.getenv("TG_SECRET_TOKEN")
    if not validate_secret(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(status_code=401, detail="invalid secret")

    update = await request.json()
    evt = to_input_event(bot_id, update, hub_id=None)
    tm.record_event("updates_total", {"type": evt.type})

    # Idempotency: dedup by {bot_id, update_id}
    idem_key = f"tg:{bot_id}:{evt.update_id}"
    raw_body = json.dumps(update, ensure_ascii=False, separators=(",", ":"))
    body_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    path = f"/io/tg/{bot_id}/webhook"
    cached = sqlite_db.idem_get(idem_key, "POST", path, bot_id, body_hash)
    response: dict | None = None
    status_code: int | None = None
    if cached:
        try:
            response = json.loads(cached["body_json"]) if cached.get("body_json") else {"ok": True, "cached": True}
        except Exception:
            response = {"ok": True, "cached": True}
        try:
            status_code = int(cached.get("status_code") or 200)
        except Exception:
            status_code = 200

    # Enrich payload with downloaded media paths
    settings = get_ctx().settings
    token = settings.tg_bot_token or os.getenv("TG_BOT_TOKEN")
    files_root = settings.files_tmp_dir or (str(get_ctx().paths.tmp_dir()))
    dest_root = os.path.join(files_root, "telegram", bot_id)

    if response is None:  # proceed only if not cached
      try:
        if evt.type == "audio" and token and evt.payload.get("file_id"):
            fpath = get_file_path(token, evt.payload["file_id"])  # type: ignore
            if fpath:
                local = download_file(token, fpath, dest_root)
                # try convert to wav16k
                wav_path = local.with_suffix(".wav")
                if convert_opus_to_wav16k(local, wav_path):
                    evt.payload["audio_path"] = str(wav_path)
                else:
                    evt.payload["audio_path"] = str(local)
        elif evt.type == "photo" and token and evt.payload.get("file_id"):
            fpath = get_file_path(token, evt.payload["file_id"])  # type: ignore
            if fpath:
                local = download_file(token, fpath, dest_root)
                evt.payload["image_path"] = str(local)
        elif evt.type == "document" and token and evt.payload.get("file_id"):
            fpath = get_file_path(token, evt.payload["file_id"])  # type: ignore
            if fpath:
                local = download_file(token, fpath, dest_root)
                evt.payload["document_path"] = str(local)
      except Exception:
          # Non-fatal; continue without media enrichment
          pass

    # Handle /start <code> pairing in text updates
    if response is None and evt.type == "text":
        txt = (evt.payload.get("text") or "").strip()
        if txt.lower().startswith("/start "):
            code = txt.split(" ", 1)[1].strip()
            await pairing_svc.confirm_pair_code(code=code, platform_user={"platform": "telegram", "user_id": evt.user_id, "bot_id": bot_id})

    # Resolve hub and publish to IO bus
    lang = (evt.payload.get("meta") or {}).get("lang") if isinstance(evt.payload, dict) else None
    hub = resolve_hub_id(platform="telegram", user_id=evt.user_id, bot_id=bot_id, locale=lang)
    if not hub:
        hub = get_ctx().settings.default_hub
    evt.hub_id = hub

    if response is None:
        if not hub:
            # no route; 202 Accepted but not queued
            response = {"ok": True, "routed": False}
            status_code = 202
        else:
            envelope = {
                "event_id": uuid4().hex,
                "kind": "io.input",
                "ts": datetime.utcnow().isoformat() + "Z",
                "dedup_key": f"{bot_id}:{evt.update_id}",
                "payload": asdict(evt),
                "meta": {"bot_id": bot_id, "hub_id": hub, "trace_id": uuid4().hex, "retries": 0},
            }
            # Publish via IO bus
            try:
                bus = getattr(request.app.state, "bus", None)
                if bus and hasattr(bus, "publish_input"):
                    await bus.publish_input(hub, envelope)
                    tm.record_event("enqueue_total", {"hub_id": hub})
                response = {"ok": True, "routed": True}
                status_code = 200
            except Exception:
                # DLQ on intake failure (optional)
                try:
                    if bus and hasattr(bus, "publish_dlq"):
                        await bus.publish_dlq("input", {"error": "publish_failed", "envelope": envelope})
                except Exception:
                    pass
                response = {"ok": True, "routed": False}
                status_code = 202
        # store idempotency only for non-cached path
        sqlite_db.idem_put(
            idem_key,
            "POST",
            path,
            bot_id,
            body_hash,
            status_code,
            json.dumps(response, ensure_ascii=False),
            event_id=evt.update_id,
            server_time_utc=str(int(time.time())),
            ttl=86400,
        )
    from fastapi import Response
    return Response(content=json.dumps(response, ensure_ascii=False), media_type="application/json", status_code=status_code)


@router.post("/io/tg/pair/create")
async def tg_pair_create(hub: Optional[str] = None, ttl: Optional[int] = None, bot: Optional[str] = None):
    ttl_sec = int(ttl or 600)
    res = await pairing_svc.issue_pair_code(bot_id=bot or "main-bot", hub_id=hub, ttl_sec=ttl_sec)
    return {"ok": True, **res}


@router.post("/io/tg/pair/confirm")
async def tg_pair_confirm(code: str, user_id: Optional[str] = None, bot_id: Optional[str] = None):
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    # platform_user: минимально user_id/bot_id
    res = await pairing_svc.confirm_pair_code(code=code, platform_user={"user_id": user_id, "bot_id": bot_id})
    return res


@router.get("/io/tg/pair/status")
async def tg_pair_status(code: str):
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    res = await pairing_svc.pair_status(code=code)
    return {"ok": True, **res}


@router.post("/io/tg/pair/revoke")
async def tg_pair_revoke(code: str):
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    res = await pairing_svc.revoke_pair_code(code=code)
    return res
