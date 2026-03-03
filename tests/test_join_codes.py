from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from adaos.services.agent_context import get_ctx
from adaos.services.join_codes import (
    JoinCodeConsumed,
    JoinCodeExpired,
    JoinCodeNotFound,
    create,
    consume,
)


def _store_path() -> Path:
    return get_ctx().paths.base_dir() / "join_codes.json"


def test_join_code_one_time() -> None:
    info = create(subnet_id="subnet-1", ttl_seconds=60, length=8, ctx=get_ctx())
    rec = consume(code=info.code, subnet_id="subnet-1", ctx=get_ctx())
    assert rec["consumed_at"] is not None
    with pytest.raises(JoinCodeConsumed):
        consume(code=info.code, subnet_id="subnet-1", ctx=get_ctx())


def test_join_code_wrong_subnet() -> None:
    info = create(subnet_id="subnet-1", ttl_seconds=60, length=8, ctx=get_ctx())
    with pytest.raises(JoinCodeNotFound):
        consume(code=info.code, subnet_id="subnet-2", ctx=get_ctx())


def test_join_code_expired() -> None:
    info = create(subnet_id="subnet-1", ttl_seconds=60, length=8, ctx=get_ctx())
    path = _store_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    codes = data["codes"]
    # Force expiry for this code by expiring everything older than now.
    now = time.time()
    for rec in codes.values():
        if isinstance(rec, dict) and rec.get("subnet_id") == "subnet-1":
            rec["expires_at"] = now - 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(JoinCodeExpired):
        consume(code=info.code, subnet_id="subnet-1", ctx=get_ctx())

