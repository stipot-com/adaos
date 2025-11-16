from __future__ import annotations

from pathlib import Path

from adaos.services.agent_context import get_ctx


def ystores_root() -> Path:
    """
    Return the root directory for Yjs stores, ensuring it exists.
    """
    ctx = get_ctx()
    root = ctx.paths.state_dir() / "ystores"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ystore_path_for_webspace(webspace_id: str) -> Path:
    """
    Map a webspace id to a filesystem path for its SQLite-backed Yjs store.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in webspace_id)
    return ystores_root() / f"{safe}.sqlite3"

