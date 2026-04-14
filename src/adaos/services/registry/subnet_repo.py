from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from adaos.adapters.db.sqlite_store import SQLite


def _now() -> float:
    return time.time()


def _normalize_runtime_projection_payload(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else data
    snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
    build = snapshot.get("build") if isinstance(snapshot.get("build"), dict) else {}
    update_status = snapshot.get("update_status") if isinstance(snapshot.get("update_status"), dict) else {}
    last_result = snapshot.get("last_result") if isinstance(snapshot.get("last_result"), dict) else {}
    hub_control_request = (
        snapshot.get("hub_control_request")
        if isinstance(snapshot.get("hub_control_request"), dict)
        else {}
    )
    node_names = [
        str(item or "").strip()
        for item in list(snapshot.get("node_names") or [])
        if str(item or "").strip()
    ]
    captured_at = snapshot.get("captured_at")
    try:
        captured_at_value = float(captured_at) if captured_at is not None else None
    except Exception:
        captured_at_value = None
    ready = snapshot.get("ready")
    connected_to_hub = snapshot.get("connected_to_hub")
    return {
        "captured_at": captured_at_value,
        "node_names": node_names,
        "primary_node_name": str(snapshot.get("primary_node_name") or "").strip(),
        "ready": bool(ready) if isinstance(ready, bool) else None,
        "node_state": str(snapshot.get("node_state") or "").strip(),
        "route_mode": str(snapshot.get("route_mode") or "").strip(),
        "connected_to_hub": (
            bool(connected_to_hub)
            if isinstance(connected_to_hub, bool)
            else None
        ),
        "build": dict(build),
        "update_status": dict(update_status),
        "last_result": dict(last_result),
        "hub_control_request": dict(hub_control_request),
        "snapshot": dict(snapshot),
    }


class SubnetRepo:
    """SQLite-backed repository for subnet nodes and their capacity.

    Persists directory data across hub restarts. Capacity is stored long-term;
    liveness stays in-memory at the directory layer.
    """

    def __init__(self, sql: SQLite) -> None:
        self.sql = sql
        self._ensure_schema()

    # -------------------- schema --------------------
    def _ensure_schema(self) -> None:
        with self.sql.connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS subnet_nodes (
                  node_id     TEXT PRIMARY KEY,
                  subnet_id   TEXT NOT NULL,
                  roles_json  TEXT NOT NULL,
                  hostname    TEXT,
                  base_url    TEXT,
                  node_state  TEXT NOT NULL DEFAULT 'ready',
                  last_seen   REAL,
                  created_at  REAL NOT NULL,
                  updated_at  REAL NOT NULL
                )
                """
            )
            try:
                con.execute("ALTER TABLE subnet_nodes ADD COLUMN node_state TEXT NOT NULL DEFAULT 'ready'")
            except Exception:
                pass
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS subnet_capacity_io (
                  node_id     TEXT NOT NULL,
                  io_type     TEXT NOT NULL,
                  capabilities_json TEXT NOT NULL,
                  priority    INTEGER NOT NULL DEFAULT 50,
                  id_hint     TEXT NOT NULL DEFAULT '',
                  updated_at  REAL NOT NULL,
                  PRIMARY KEY (node_id, io_type, id_hint)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS subnet_capacity_skills (
                  node_id     TEXT NOT NULL,
                  name        TEXT NOT NULL,
                  version     TEXT NOT NULL,
                  active      INTEGER NOT NULL,
                  updated_at  REAL NOT NULL,
                  PRIMARY KEY (node_id, name)
                )
                """
            )
            # best-effort add dev flag for existing DBs
            try:
                con.execute("ALTER TABLE subnet_capacity_skills ADD COLUMN dev INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            # scenarios capacity table
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS subnet_capacity_scenarios (
                  node_id     TEXT NOT NULL,
                  name        TEXT NOT NULL,
                  version     TEXT NOT NULL,
                  active      INTEGER NOT NULL,
                  updated_at  REAL NOT NULL,
                  dev         INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (node_id, name)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS subnet_runtime_projection (
                  node_id                  TEXT PRIMARY KEY,
                  captured_at              REAL,
                  node_names_json          TEXT NOT NULL DEFAULT '[]',
                  primary_node_name        TEXT,
                  ready                    INTEGER,
                  node_state               TEXT,
                  route_mode               TEXT,
                  connected_to_hub         INTEGER,
                  build_json               TEXT NOT NULL DEFAULT '{}',
                  update_status_json       TEXT NOT NULL DEFAULT '{}',
                  last_result_json         TEXT NOT NULL DEFAULT '{}',
                  hub_control_request_json TEXT NOT NULL DEFAULT '{}',
                  snapshot_json            TEXT NOT NULL DEFAULT '{}',
                  updated_at               REAL NOT NULL
                )
                """
            )
            con.commit()

    # -------------------- nodes --------------------
    def upsert_node(self, node: Dict[str, Any]) -> None:
        node_id = str(node.get("node_id"))
        subnet_id = str(node.get("subnet_id") or "")
        roles = json.dumps(list(node.get("roles") or []), ensure_ascii=False)
        hostname = node.get("hostname")
        base_url = node.get("base_url")
        node_state = str(node.get("node_state") or "ready")
        last_seen = float(node.get("last_seen") or 0.0)
        now = _now()
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO subnet_nodes(node_id, subnet_id, roles_json, hostname, base_url, node_state, last_seen, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                  subnet_id=excluded.subnet_id,
                  roles_json=excluded.roles_json,
                  hostname=excluded.hostname,
                  base_url=excluded.base_url,
                  node_state=excluded.node_state,
                  last_seen=excluded.last_seen,
                  updated_at=excluded.updated_at
                """,
                (node_id, subnet_id, roles, hostname, base_url, node_state, last_seen, now, now),
            )
            con.commit()

    def touch_heartbeat(self, node_id: str, last_seen: float, capacity: Optional[Dict[str, Any]] = None, *, node_state: str | None = None) -> None:
        with self.sql.connect() as con:
            if node_state is not None:
                con.execute(
                    "UPDATE subnet_nodes SET last_seen=?, node_state=?, updated_at=? WHERE node_id=?",
                    (float(last_seen), str(node_state or "ready"), _now(), node_id),
                )
            else:
                con.execute(
                    "UPDATE subnet_nodes SET last_seen=?, updated_at=? WHERE node_id=?",
                    (float(last_seen), _now(), node_id),
                )
            con.commit()
        if capacity:
            if "io" in capacity:
                self.replace_io_capacity(node_id, capacity.get("io") or [])
            if "skills" in capacity:
                self.replace_skill_capacity(node_id, capacity.get("skills") or [])
            if "scenarios" in capacity:
                self.replace_scenario_capacity(node_id, capacity.get("scenarios") or [])

    def list_nodes(self) -> List[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT node_id, subnet_id, roles_json, hostname, base_url, node_state, last_seen, created_at, updated_at FROM subnet_nodes"
            )
            rows = []
            for r in cur.fetchall():
                rows.append(
                    {
                        "node_id": r[0],
                        "subnet_id": r[1],
                        "roles": json.loads(r[2] or "[]"),
                        "hostname": r[3],
                        "base_url": r[4],
                        "node_state": r[5] or "ready",
                        "last_seen": r[6],
                        "created_at": r[7],
                        "updated_at": r[8],
                    }
                )
            return rows

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT node_id, subnet_id, roles_json, hostname, base_url, node_state, last_seen, created_at, updated_at FROM subnet_nodes WHERE node_id=?",
                (node_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "node_id": r[0],
                "subnet_id": r[1],
                "roles": json.loads(r[2] or "[]"),
                "hostname": r[3],
                "base_url": r[4],
                "node_state": r[5] or "ready",
                "last_seen": r[6],
                "created_at": r[7],
                "updated_at": r[8],
            }

    def upsert_runtime_projection(self, node_id: str, payload: Dict[str, Any] | None) -> None:
        projection = _normalize_runtime_projection_payload(payload)
        now = _now()
        ready = projection.get("ready")
        connected_to_hub = projection.get("connected_to_hub")
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO subnet_runtime_projection(
                  node_id,
                  captured_at,
                  node_names_json,
                  primary_node_name,
                  ready,
                  node_state,
                  route_mode,
                  connected_to_hub,
                  build_json,
                  update_status_json,
                  last_result_json,
                  hub_control_request_json,
                  snapshot_json,
                  updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                  captured_at=excluded.captured_at,
                  node_names_json=excluded.node_names_json,
                  primary_node_name=excluded.primary_node_name,
                  ready=excluded.ready,
                  node_state=excluded.node_state,
                  route_mode=excluded.route_mode,
                  connected_to_hub=excluded.connected_to_hub,
                  build_json=excluded.build_json,
                  update_status_json=excluded.update_status_json,
                  last_result_json=excluded.last_result_json,
                  hub_control_request_json=excluded.hub_control_request_json,
                  snapshot_json=excluded.snapshot_json,
                  updated_at=excluded.updated_at
                """,
                (
                    node_id,
                    projection.get("captured_at"),
                    json.dumps(list(projection.get("node_names") or []), ensure_ascii=False),
                    projection.get("primary_node_name") or None,
                    (1 if ready is True else 0 if ready is False else None),
                    projection.get("node_state") or None,
                    projection.get("route_mode") or None,
                    (
                        1
                        if connected_to_hub is True
                        else 0
                        if connected_to_hub is False
                        else None
                    ),
                    json.dumps(projection.get("build") or {}, ensure_ascii=False),
                    json.dumps(projection.get("update_status") or {}, ensure_ascii=False),
                    json.dumps(projection.get("last_result") or {}, ensure_ascii=False),
                    json.dumps(projection.get("hub_control_request") or {}, ensure_ascii=False),
                    json.dumps(projection.get("snapshot") or {}, ensure_ascii=False),
                    now,
                ),
            )
            con.commit()

    def runtime_projection_for_node(self, node_id: str) -> Dict[str, Any]:
        with self.sql.connect() as con:
            cur = con.execute(
                """
                SELECT
                  captured_at,
                  node_names_json,
                  primary_node_name,
                  ready,
                  node_state,
                  route_mode,
                  connected_to_hub,
                  build_json,
                  update_status_json,
                  last_result_json,
                  hub_control_request_json,
                  snapshot_json,
                  updated_at
                FROM subnet_runtime_projection
                WHERE node_id=?
                """,
                (node_id,),
            )
            row = cur.fetchone()
        if not row:
            return {}
        return {
            "captured_at": row[0],
            "node_names": json.loads(row[1] or "[]"),
            "primary_node_name": row[2] or "",
            "ready": (bool(row[3]) if row[3] is not None else None),
            "node_state": row[4] or "",
            "route_mode": row[5] or "",
            "connected_to_hub": (bool(row[6]) if row[6] is not None else None),
            "build": json.loads(row[7] or "{}"),
            "update_status": json.loads(row[8] or "{}"),
            "last_result": json.loads(row[9] or "{}"),
            "hub_control_request": json.loads(row[10] or "{}"),
            "snapshot": json.loads(row[11] or "{}"),
            "updated_at": row[12],
        }

    # -------------------- capacity --------------------
    def replace_io_capacity(self, node_id: str, io_list: List[Dict[str, Any]]) -> None:
        now = _now()
        with self.sql.connect() as con:
            con.execute("DELETE FROM subnet_capacity_io WHERE node_id=?", (node_id,))
            for item in io_list or []:
                io_type = str(item.get("io_type") or item.get("type") or "stdout")
                caps = json.dumps(list(item.get("capabilities") or []), ensure_ascii=False)
                prio = int(item.get("priority") or 50)
                id_hint = item.get("id_hint") or ""
                con.execute(
                    """
                    INSERT INTO subnet_capacity_io(node_id, io_type, capabilities_json, priority, id_hint, updated_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (node_id, io_type, caps, prio, id_hint, now),
                )
            con.commit()

    def replace_skill_capacity(self, node_id: str, skills: List[Dict[str, Any]]) -> None:
        now = _now()
        with self.sql.connect() as con:
            con.execute("DELETE FROM subnet_capacity_skills WHERE node_id=?", (node_id,))
            for s in skills or []:
                name = str(s.get("name") or s.get("id") or "").strip()
                if not name:
                    continue
                version = str(s.get("version") or "").strip() or "unknown"
                active = 1 if bool(s.get("active", True)) else 0
                dev = 1 if bool(s.get("dev", False)) else 0
                con.execute(
                    """
                    INSERT INTO subnet_capacity_skills(node_id, name, version, active, updated_at, dev)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (node_id, name, version, active, now, dev),
                )
            con.commit()

    def nodes_with_skill(self, name: str) -> List[Dict[str, Any]]:
        q = (
            "SELECT n.node_id, n.subnet_id, n.roles_json, n.hostname, n.base_url, n.node_state, n.last_seen, s.version, s.active "
            "FROM subnet_nodes n JOIN subnet_capacity_skills s ON n.node_id=s.node_id WHERE s.name=?"
        )
        with self.sql.connect() as con:
            cur = con.execute(q, (name,))
            out: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                out.append(
                    {
                        "node_id": r[0],
                        "subnet_id": r[1],
                        "roles": json.loads(r[2] or "[]"),
                        "hostname": r[3],
                        "base_url": r[4],
                        "node_state": r[5] or "ready",
                        "last_seen": r[6],
                        "version": r[7],
                        "active": bool(r[8]),
                    }
                )
            return out

    def io_for_node(self, node_id: str) -> List[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT io_type, capabilities_json, priority, id_hint, updated_at FROM subnet_capacity_io WHERE node_id=?",
                (node_id,),
            )
            out: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                out.append(
                    {
                        "io_type": r[0],
                        "capabilities": json.loads(r[1] or "[]"),
                        "priority": int(r[2] or 50),
                        "id_hint": r[3],
                        "updated_at": r[4],
                    }
                )
            return out

    def skills_for_node(self, node_id: str) -> List[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, version, active, updated_at, dev FROM subnet_capacity_skills WHERE node_id=?",
                (node_id,),
            )
            out: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                out.append(
                    {
                        "name": r[0],
                        "version": r[1],
                        "active": bool(r[2]),
                        "updated_at": r[3],
                        "dev": bool(r[4]) if len(r) > 4 else False,
                    }
                )
            return out

    def replace_scenario_capacity(self, node_id: str, scenarios: List[Dict[str, Any]]) -> None:
        now = _now()
        with self.sql.connect() as con:
            con.execute("DELETE FROM subnet_capacity_scenarios WHERE node_id=?", (node_id,))
            for s in scenarios or []:
                name = str(s.get("name") or s.get("id") or "").strip()
                if not name:
                    continue
                version = str(s.get("version") or "").strip() or "unknown"
                active = 1 if bool(s.get("active", True)) else 0
                dev = 1 if bool(s.get("dev", False)) else 0
                con.execute(
                    """
                    INSERT INTO subnet_capacity_scenarios(node_id, name, version, active, updated_at, dev)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (node_id, name, version, active, now, dev),
                )
            con.commit()

    def scenarios_for_node(self, node_id: str) -> List[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, version, active, updated_at, dev FROM subnet_capacity_scenarios WHERE node_id=?",
                (node_id,),
            )
            out: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                out.append(
                    {
                        "name": r[0],
                        "version": r[1],
                        "active": bool(r[2]),
                        "updated_at": r[3],
                        "dev": bool(r[4]) if len(r) > 4 else False,
                    }
                )
            return out
