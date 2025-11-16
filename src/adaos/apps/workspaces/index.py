from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from adaos.services.agent_context import get_ctx

from adaos.apps.yjs.y_store import ystore_path_for_webspace


@dataclass
class WorkspaceRow:
    workspace_id: str
    path: str
    created_at: int


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS y_workspaces(
            workspace_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )


def get_workspace(workspace_id: str) -> Optional[WorkspaceRow]:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            "SELECT workspace_id, path, created_at FROM y_workspaces WHERE workspace_id=?",
            (workspace_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return WorkspaceRow(workspace_id=row[0], path=row[1], created_at=int(row[2]))


def ensure_workspace(workspace_id: str) -> WorkspaceRow:
    """
    Ensure a workspace row exists and return it. The associated Yjs store
    path is derived from the current ctx paths.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            "SELECT workspace_id, path, created_at FROM y_workspaces WHERE workspace_id=?",
            (workspace_id,),
        )
        row = cur.fetchone()
        if row:
            return WorkspaceRow(workspace_id=row[0], path=row[1], created_at=int(row[2]))

        p: Path = ystore_path_for_webspace(workspace_id)
        import time as _time

        created_at = int(_time.time() * 1000)
        con.execute(
            "INSERT INTO y_workspaces(workspace_id, path, created_at) VALUES(?,?,?)",
            (workspace_id, str(p), created_at),
        )
        con.commit()
        return WorkspaceRow(workspace_id=workspace_id, path=str(p), created_at=created_at)

