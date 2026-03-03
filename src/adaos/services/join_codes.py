from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adaos.services.agent_context import AgentContext, get_ctx


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class JoinCodeError(RuntimeError):
    pass


class JoinCodeNotFound(JoinCodeError):
    pass


class JoinCodeExpired(JoinCodeError):
    pass


class JoinCodeConsumed(JoinCodeError):
    pass


@dataclass(frozen=True, slots=True)
class JoinCodeInfo:
    code: str
    expires_at: float


def _store_path(ctx: AgentContext | None = None) -> Path:
    base = (ctx or get_ctx()).paths.base_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "join_codes.json"


def _normalize(code: str) -> str:
    text = str(code or "").strip().upper()
    text = text.replace("-", "").replace(" ", "")
    return text


def _hash(code: str) -> str:
    norm = _normalize(code)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def generate_code(*, length: int = 8) -> str:
    if length not in (8, 10, 12):
        raise ValueError("length must be 8, 10, or 12")
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    if length == 8:
        return f"{raw[:4]}-{raw[4:]}"
    if length == 10:
        return f"{raw[:5]}-{raw[5:]}"
    return f"{raw[:6]}-{raw[6:]}"


def _load(ctx: AgentContext | None = None) -> dict[str, Any]:
    path = _store_path(ctx)
    if not path.exists():
        return {"v": 1, "codes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {"v": 1, "codes": {}}
    if not isinstance(data, dict):
        return {"v": 1, "codes": {}}
    if not isinstance(data.get("codes"), dict):
        data["codes"] = {}
    data.setdefault("v", 1)
    return data


def _save(data: dict[str, Any], ctx: AgentContext | None = None) -> None:
    path = _store_path(ctx)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def cleanup_expired(*, ctx: AgentContext | None = None) -> int:
    data = _load(ctx)
    now = time.time()
    codes = data.get("codes") or {}
    if not isinstance(codes, dict):
        return 0
    removed = 0
    for k in list(codes.keys()):
        rec = codes.get(k)
        if not isinstance(rec, dict):
            codes.pop(k, None)
            removed += 1
            continue
        exp = float(rec.get("expires_at") or 0.0)
        consumed = rec.get("consumed_at")
        if consumed is not None:
            continue
        if exp and exp < now:
            codes.pop(k, None)
            removed += 1
    if removed:
        data["codes"] = codes
        _save(data, ctx)
    return removed


def create(
    *,
    subnet_id: str,
    ttl_seconds: int = 15 * 60,
    length: int = 8,
    meta: dict[str, Any] | None = None,
    ctx: AgentContext | None = None,
) -> JoinCodeInfo:
    if ttl_seconds < 60 or ttl_seconds > 60 * 60:
        raise ValueError("ttl_seconds must be between 60 and 3600")
    subnet_id = str(subnet_id or "").strip()
    if not subnet_id:
        raise ValueError("subnet_id is required")

    cleanup_expired(ctx=ctx)
    data = _load(ctx)
    codes = data.get("codes") or {}
    if not isinstance(codes, dict):
        codes = {}

    code = generate_code(length=length)
    code_hash = _hash(code)
    now = time.time()
    expires_at = now + int(ttl_seconds)

    codes[code_hash] = {
        "created_at": now,
        "expires_at": expires_at,
        "consumed_at": None,
        "subnet_id": subnet_id,
        "meta": meta or {},
    }
    data["codes"] = codes
    _save(data, ctx)
    return JoinCodeInfo(code=code, expires_at=expires_at)


def consume(
    *,
    code: str,
    subnet_id: str,
    ctx: AgentContext | None = None,
) -> dict[str, Any]:
    subnet_id = str(subnet_id or "").strip()
    if not subnet_id:
        raise ValueError("subnet_id is required")

    code_hash = _hash(code)
    data = _load(ctx)
    codes = data.get("codes") or {}
    if not isinstance(codes, dict):
        raise JoinCodeNotFound("join-code not found")

    rec = codes.get(code_hash)
    if not isinstance(rec, dict):
        raise JoinCodeNotFound("join-code not found")
    if str(rec.get("subnet_id") or "").strip() != subnet_id:
        raise JoinCodeNotFound("join-code not found")

    now = time.time()
    exp = float(rec.get("expires_at") or 0.0)
    if exp and exp < now:
        codes.pop(code_hash, None)
        data["codes"] = codes
        _save(data, ctx)
        raise JoinCodeExpired("join-code expired")
    if rec.get("consumed_at") is not None:
        raise JoinCodeConsumed("join-code already consumed")

    rec["consumed_at"] = now
    codes[code_hash] = rec
    data["codes"] = codes
    _save(data, ctx)
    return rec
