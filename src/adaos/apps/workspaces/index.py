from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Iterable, List
import sqlite3

from adaos.services.agent_context import get_ctx

from adaos.apps.yjs.y_store import ystore_path_for_webspace
from adaos.apps.yjs.webspace import default_webspace_id


@dataclass
class WorkspaceRow:
    workspace_id: str
    path: str
    created_at: int
    display_name: Optional[str] = None


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS y_workspaces(
            workspace_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            display_name TEXT
        )
        """
    )
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(y_workspaces)")}
    except sqlite3.Error:
        cols = set()
    if "display_name" not in cols:
        try:
            con.execute("ALTER TABLE y_workspaces ADD COLUMN display_name TEXT")
        except sqlite3.OperationalError:
            pass


def get_workspace(workspace_id: str) -> Optional[WorkspaceRow]:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            "SELECT workspace_id, path, created_at, display_name FROM y_workspaces WHERE workspace_id=?",
            (workspace_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return WorkspaceRow(workspace_id=row[0], path=row[1], created_at=int(row[2]), display_name=row[3])


def list_workspaces() -> List[WorkspaceRow]:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            "SELECT workspace_id, path, created_at, display_name FROM y_workspaces ORDER BY created_at"
        )
        rows = [
            WorkspaceRow(workspace_id=row[0], path=row[1], created_at=int(row[2]), display_name=row[3])
            for row in cur.fetchall()
        ]
    if not rows:
        rows = [ensure_workspace(default_webspace_id())]
    return rows


def ensure_workspace(workspace_id: str) -> WorkspaceRow:
    """
    Ensure a workspace row exists and return it. The associated Yjs store
    path is derived from the current ctx paths.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            "SELECT workspace_id, path, created_at, display_name FROM y_workspaces WHERE workspace_id=?",
            (workspace_id,),
        )
        row = cur.fetchone()
        if row:
            return WorkspaceRow(workspace_id=row[0], path=row[1], created_at=int(row[2]), display_name=row[3])

        p: Path = ystore_path_for_webspace(workspace_id)
        import time as _time

        created_at = int(_time.time() * 1000)
        con.execute(
            "INSERT INTO y_workspaces(workspace_id, path, created_at, display_name) VALUES(?,?,?,?)",
            (workspace_id, str(p), created_at, workspace_id),
        )
        con.commit()
        return WorkspaceRow(workspace_id=workspace_id, path=str(p), created_at=created_at, display_name=workspace_id)


def set_display_name(workspace_id: str, display_name: Optional[str]) -> WorkspaceRow:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        con.execute(
            "UPDATE y_workspaces SET display_name=? WHERE workspace_id=?",
            (display_name, workspace_id),
        )
        con.commit()
    row = get_workspace(workspace_id)
    if not row:
        raise KeyError(f"workspace {workspace_id} not found")
    return row


def delete_workspace(workspace_id: str) -> None:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        con.execute("DELETE FROM y_workspaces WHERE workspace_id=?", (workspace_id,))
        con.commit()
    try:
        path = ystore_path_for_webspace(workspace_id)
        if path.exists():
            path.unlink()
    except Exception:
        pass


def reset_webspaces(rows: Iterable[WorkspaceRow]) -> None:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        con.execute("DELETE FROM y_workspaces")
        con.executemany(
            "INSERT INTO y_workspaces(workspace_id, path, created_at, display_name) VALUES(?,?,?,?)",
            [(row.workspace_id, row.path, row.created_at, row.display_name) for row in rows],
        )
        con.commit()

