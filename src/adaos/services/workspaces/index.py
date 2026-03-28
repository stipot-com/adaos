from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Iterable, List, Any
import sqlite3
import json

from adaos.services.agent_context import get_ctx
from adaos.services.yjs.store import ystore_path_for_webspace
from adaos.services.yjs.webspace import default_webspace_id, dev_webspace_id

DEFAULT_HOME_SCENARIO = "web_desktop"
KIND_WORKSPACE = "workspace"
KIND_DEV = "dev"
SOURCE_MODE_WORKSPACE = "workspace"
SOURCE_MODE_DEV = "dev"

_ROW_SELECT = (
    "workspace_id, path, created_at, display_name, "
    "kind, home_scenario, source_mode, owner_scope, profile_scope, device_binding, ui_overlay_json"
)

_UNSET = object()


def _is_dev_display_name(value: Optional[str]) -> bool:
    if not value:
        return False
    return str(value).lstrip().upper().startswith("DEV:")


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dedupe_text_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _normalize_ui_overlay_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    installed_raw = value.get("installed") if isinstance(value.get("installed"), dict) else {}
    installed = {
        "apps": _dedupe_text_list(installed_raw.get("apps")),
        "widgets": _dedupe_text_list(installed_raw.get("widgets")),
    }
    overlay: dict[str, Any] = {}
    if installed["apps"] or installed["widgets"]:
        overlay["installed"] = installed
    return overlay


def _encode_ui_overlay_json(value: Any) -> Optional[str]:
    overlay = _normalize_ui_overlay_payload(value)
    if not overlay:
        return None
    return json.dumps(overlay, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _decode_ui_overlay_json(value: Any) -> dict[str, Any]:
    token = _normalize_optional_text(value)
    if not token:
        return {}
    try:
        payload = json.loads(token)
    except Exception:
        return {}
    return _normalize_ui_overlay_payload(payload)


def _normalize_kind(value: Any) -> Optional[str]:
    token = str(value or "").strip().lower()
    if token in (KIND_WORKSPACE, KIND_DEV):
        return token
    return None


def _infer_kind(workspace_id: str, display_name: Optional[str], kind: Optional[str]) -> str:
    explicit = _normalize_kind(kind)
    if explicit:
        return explicit
    if str(workspace_id or "").strip() == dev_webspace_id():
        return KIND_DEV
    if _is_dev_display_name(display_name):
        return KIND_DEV
    return KIND_WORKSPACE


def _normalize_source_mode(value: Any) -> Optional[str]:
    token = str(value or "").strip().lower()
    if token in (SOURCE_MODE_WORKSPACE, SOURCE_MODE_DEV):
        return token
    return None


def _infer_source_mode(source_mode: Optional[str], *, kind: str) -> str:
    explicit = _normalize_source_mode(source_mode)
    if explicit:
        return explicit
    return SOURCE_MODE_DEV if kind == KIND_DEV else SOURCE_MODE_WORKSPACE


def _default_display_name(workspace_id: str, *, kind: str) -> str:
    token = str(workspace_id or "").strip() or default_webspace_id()
    if kind == KIND_DEV:
        return f"DEV: {token}"
    return token


@dataclass(slots=True)
class WebspaceManifest:
    workspace_id: str
    path: str
    created_at: int
    display_name: Optional[str] = None
    kind: Optional[str] = None
    home_scenario: Optional[str] = None
    source_mode: Optional[str] = None
    owner_scope: Optional[str] = None
    profile_scope: Optional[str] = None
    device_binding: Optional[str] = None
    ui_overlay_json: Optional[str] = None

    @property
    def effective_kind(self) -> str:
        return _infer_kind(self.workspace_id, self.display_name, self.kind)

    @property
    def is_dev(self) -> bool:
        return self.effective_kind == KIND_DEV

    @property
    def effective_source_mode(self) -> str:
        return _infer_source_mode(self.source_mode, kind=self.effective_kind)

    @property
    def effective_home_scenario(self) -> str:
        token = _normalize_optional_text(self.home_scenario)
        return token or DEFAULT_HOME_SCENARIO

    @property
    def title(self) -> str:
        token = _normalize_optional_text(self.display_name)
        if token:
            return token
        return _default_display_name(self.workspace_id, kind=self.effective_kind)

    def with_defaults(self) -> "WebspaceManifest":
        return WebspaceManifest(
            workspace_id=self.workspace_id,
            path=self.path,
            created_at=self.created_at,
            display_name=self.title,
            kind=self.effective_kind,
            home_scenario=self.home_scenario,
            source_mode=self.effective_source_mode,
            owner_scope=_normalize_optional_text(self.owner_scope),
            profile_scope=_normalize_optional_text(self.profile_scope),
            device_binding=_normalize_optional_text(self.device_binding),
            ui_overlay_json=_encode_ui_overlay_json(_decode_ui_overlay_json(self.ui_overlay_json)),
        )

    @property
    def ui_overlay(self) -> dict[str, Any]:
        return _decode_ui_overlay_json(self.ui_overlay_json)

    @property
    def installed_overlay(self) -> dict[str, list[str]]:
        installed = self.ui_overlay.get("installed") if isinstance(self.ui_overlay.get("installed"), dict) else {}
        return {
            "apps": _dedupe_text_list(installed.get("apps")),
            "widgets": _dedupe_text_list(installed.get("widgets")),
        }

    @property
    def has_ui_overlay(self) -> bool:
        return self.ui_overlay_json is not None


# Backward-compatible name used by current callers.
WorkspaceRow = WebspaceManifest


def _row_from_db(row: tuple[Any, ...], *, apply_defaults: bool = True) -> WebspaceManifest:
    manifest = WebspaceManifest(
        workspace_id=str(row[0]),
        path=str(row[1]),
        created_at=int(row[2]),
        display_name=_normalize_optional_text(row[3]),
        kind=_normalize_kind(row[4]),
        home_scenario=_normalize_optional_text(row[5]),
        source_mode=_normalize_source_mode(row[6]),
        owner_scope=_normalize_optional_text(row[7]),
        profile_scope=_normalize_optional_text(row[8]),
        device_binding=_normalize_optional_text(row[9]),
        ui_overlay_json=_encode_ui_overlay_json(_decode_ui_overlay_json(row[10])),
    )
    return manifest.with_defaults() if apply_defaults else manifest


def _manifest_needs_persisted_defaults(manifest: WebspaceManifest) -> bool:
    normalized = manifest.with_defaults()
    return any(
        (
            manifest.display_name != normalized.display_name,
            manifest.kind != normalized.kind,
            manifest.source_mode != normalized.source_mode,
            manifest.owner_scope != normalized.owner_scope,
            manifest.profile_scope != normalized.profile_scope,
            manifest.device_binding != normalized.device_binding,
            manifest.ui_overlay_json != normalized.ui_overlay_json,
        )
    )


def _persist_manifest_defaults(con, manifest: WebspaceManifest) -> WebspaceManifest:
    normalized = manifest.with_defaults()
    if not _manifest_needs_persisted_defaults(manifest):
        return normalized
    con.execute(
        """
        UPDATE y_workspaces
        SET display_name=?, kind=?, home_scenario=?, source_mode=?,
            owner_scope=?, profile_scope=?, device_binding=?, ui_overlay_json=?
        WHERE workspace_id=?
        """,
        (
            normalized.display_name,
            normalized.kind,
            manifest.home_scenario,
            normalized.source_mode,
            normalized.owner_scope,
            normalized.profile_scope,
            normalized.device_binding,
            normalized.ui_overlay_json,
            manifest.workspace_id,
        ),
    )
    return normalized


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS y_workspaces(
            workspace_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            display_name TEXT,
            kind TEXT,
            home_scenario TEXT,
            source_mode TEXT,
            owner_scope TEXT,
            profile_scope TEXT,
            device_binding TEXT,
            ui_overlay_json TEXT
        )
        """
    )
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(y_workspaces)")}
    except sqlite3.Error:
        cols = set()
    for name, ddl in (
        ("display_name", "TEXT"),
        ("kind", "TEXT"),
        ("home_scenario", "TEXT"),
        ("source_mode", "TEXT"),
        ("owner_scope", "TEXT"),
        ("profile_scope", "TEXT"),
        ("device_binding", "TEXT"),
        ("ui_overlay_json", "TEXT"),
    ):
        if name in cols:
            continue
        try:
            con.execute(f"ALTER TABLE y_workspaces ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError:
            pass


def get_workspace(workspace_id: str) -> Optional[WebspaceManifest]:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            f"SELECT {_ROW_SELECT} FROM y_workspaces WHERE workspace_id=?",
            (workspace_id,),
        )
        row = cur.fetchone()
        manifest = None
        if row:
            raw_manifest = _row_from_db(row, apply_defaults=False)
            dirty = _manifest_needs_persisted_defaults(raw_manifest)
            manifest = _persist_manifest_defaults(con, raw_manifest)
            if dirty:
                con.commit()
    if not row:
        return None
    return manifest


def list_workspaces() -> List[WebspaceManifest]:
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            f"SELECT {_ROW_SELECT} FROM y_workspaces ORDER BY created_at"
        )
        rows = []
        dirty = False
        for db_row in cur.fetchall():
            manifest = _row_from_db(db_row, apply_defaults=False)
            if _manifest_needs_persisted_defaults(manifest):
                dirty = True
            rows.append(_persist_manifest_defaults(con, manifest))
        if dirty:
            con.commit()
    if not rows:
        rows = [ensure_workspace(default_webspace_id())]
    return rows


def normalize_workspaces() -> int:
    """
    Persist inferred manifest defaults for existing rows without changing
    legacy ``home_scenario`` semantics.

    Returns the number of rows that required normalization.
    """
    sql = get_ctx().sql
    updated = 0
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(f"SELECT {_ROW_SELECT} FROM y_workspaces ORDER BY created_at")
        for db_row in cur.fetchall():
            manifest = _row_from_db(db_row, apply_defaults=False)
            if not _manifest_needs_persisted_defaults(manifest):
                continue
            _persist_manifest_defaults(con, manifest)
            updated += 1
        if updated:
            con.commit()
    return updated


def ensure_workspace(workspace_id: str) -> WebspaceManifest:
    """
    Ensure a workspace row exists and return it. The associated Yjs store
    path is derived from the current ctx paths.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        cur = con.execute(
            f"SELECT {_ROW_SELECT} FROM y_workspaces WHERE workspace_id=?",
            (workspace_id,),
        )
        row = cur.fetchone()
        if row:
            return _row_from_db(row)

        p: Path = ystore_path_for_webspace(workspace_id)
        import time as _time

        created_at = int(_time.time() * 1000)
        inferred_kind = _infer_kind(workspace_id, None, None)
        display_name = _default_display_name(workspace_id, kind=inferred_kind)
        con.execute(
            """
            INSERT INTO y_workspaces(
                workspace_id, path, created_at, display_name,
                kind, home_scenario, source_mode, owner_scope, profile_scope, device_binding, ui_overlay_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                workspace_id,
                str(p),
                created_at,
                display_name,
                inferred_kind,
                DEFAULT_HOME_SCENARIO,
                _infer_source_mode(None, kind=inferred_kind),
                None,
                None,
                None,
                None,
            ),
        )
        con.commit()
        return WebspaceManifest(
            workspace_id=workspace_id,
            path=str(p),
            created_at=created_at,
            display_name=display_name,
            kind=inferred_kind,
            home_scenario=DEFAULT_HOME_SCENARIO,
            source_mode=_infer_source_mode(None, kind=inferred_kind),
            owner_scope=None,
            profile_scope=None,
            device_binding=None,
            ui_overlay_json=None,
        )


def set_workspace_manifest(
    workspace_id: str,
    *,
    display_name: Any = _UNSET,
    kind: Any = _UNSET,
    home_scenario: Any = _UNSET,
    source_mode: Any = _UNSET,
    owner_scope: Any = _UNSET,
    profile_scope: Any = _UNSET,
    device_binding: Any = _UNSET,
    ui_overlay_json: Any = _UNSET,
) -> WebspaceManifest:
    current = ensure_workspace(workspace_id)
    next_display_name = current.display_name if display_name is _UNSET else _normalize_optional_text(display_name)
    next_kind_raw = current.kind if kind is _UNSET else _normalize_kind(kind)
    resolved_kind = _infer_kind(workspace_id, next_display_name, next_kind_raw)
    next_source_mode_raw = current.source_mode if source_mode is _UNSET else _normalize_source_mode(source_mode)
    resolved_source_mode = _infer_source_mode(next_source_mode_raw, kind=resolved_kind)
    next_home_scenario = current.home_scenario if home_scenario is _UNSET else _normalize_optional_text(home_scenario)
    next_owner_scope = current.owner_scope if owner_scope is _UNSET else _normalize_optional_text(owner_scope)
    next_profile_scope = current.profile_scope if profile_scope is _UNSET else _normalize_optional_text(profile_scope)
    next_device_binding = current.device_binding if device_binding is _UNSET else _normalize_optional_text(device_binding)
    next_ui_overlay_json = current.ui_overlay_json if ui_overlay_json is _UNSET else _encode_ui_overlay_json(ui_overlay_json)

    sql = get_ctx().sql
    with sql.connect() as con:
        _ensure_schema(con)
        con.execute(
            """
            UPDATE y_workspaces
            SET display_name=?, kind=?, home_scenario=?, source_mode=?,
                owner_scope=?, profile_scope=?, device_binding=?, ui_overlay_json=?
            WHERE workspace_id=?
            """,
            (
                next_display_name,
                resolved_kind,
                next_home_scenario,
                resolved_source_mode,
                next_owner_scope,
                next_profile_scope,
                next_device_binding,
                next_ui_overlay_json,
                workspace_id,
            ),
        )
        con.commit()
    row = get_workspace(workspace_id)
    if not row:
        raise KeyError(f"workspace {workspace_id} not found")
    return row


def set_display_name(workspace_id: str, display_name: Optional[str]) -> WebspaceManifest:
    return set_workspace_manifest(workspace_id, display_name=display_name)


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
            """
            INSERT INTO y_workspaces(
                workspace_id, path, created_at, display_name,
                kind, home_scenario, source_mode, owner_scope, profile_scope, device_binding, ui_overlay_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row.workspace_id,
                    row.path,
                    row.created_at,
                    row.display_name,
                    row.kind,
                    row.home_scenario,
                    row.source_mode,
                    row.owner_scope,
                    row.profile_scope,
                    row.device_binding,
                    row.ui_overlay_json,
                )
                for row in rows
            ],
        )
        con.commit()


def get_workspace_overlay(workspace_id: str) -> dict[str, Any]:
    row = get_workspace(workspace_id)
    if row is None:
        return {}
    return row.ui_overlay


def has_workspace_overlay(workspace_id: str) -> bool:
    row = get_workspace(workspace_id)
    return bool(row and row.has_ui_overlay)


def get_workspace_installed_overlay(workspace_id: str) -> dict[str, list[str]]:
    row = get_workspace(workspace_id)
    if row is None:
        return {"apps": [], "widgets": []}
    return row.installed_overlay


def set_workspace_overlay(workspace_id: str, overlay: Any) -> WebspaceManifest:
    return set_workspace_manifest(workspace_id, ui_overlay_json=overlay)


def set_workspace_installed_overlay(workspace_id: str, installed: Any) -> WebspaceManifest:
    current = get_workspace_overlay(workspace_id)
    overlay = dict(current) if isinstance(current, dict) else {}
    overlay["installed"] = {
        "apps": _dedupe_text_list((installed or {}).get("apps") if isinstance(installed, dict) else []),
        "widgets": _dedupe_text_list((installed or {}).get("widgets") if isinstance(installed, dict) else []),
    }
    return set_workspace_overlay(workspace_id, overlay)
