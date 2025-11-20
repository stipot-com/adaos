from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from adaos.adapters.db.sqlite_store import SQLite


def _now() -> float:
    return time.time()


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
                  last_seen   REAL,
                  created_at  REAL NOT NULL,
                  updated_at  REAL NOT NULL
                )
                """
            )
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
            con.commit()

    # -------------------- nodes --------------------
    def upsert_node(self, node: Dict[str, Any]) -> None:
        node_id = str(node.get("node_id"))
        subnet_id = str(node.get("subnet_id") or "")
        roles = json.dumps(list(node.get("roles") or []), ensure_ascii=False)
        hostname = node.get("hostname")
        base_url = node.get("base_url")
        last_seen = float(node.get("last_seen") or 0.0)
        now = _now()
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO subnet_nodes(node_id, subnet_id, roles_json, hostname, base_url, last_seen, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                  subnet_id=excluded.subnet_id,
                  roles_json=excluded.roles_json,
                  hostname=excluded.hostname,
                  base_url=excluded.base_url,
                  last_seen=excluded.last_seen,
                  updated_at=excluded.updated_at
                """,
                (node_id, subnet_id, roles, hostname, base_url, last_seen, now, now),
            )
            con.commit()

    def touch_heartbeat(self, node_id: str, last_seen: float, capacity: Optional[Dict[str, Any]] = None) -> None:
        with self.sql.connect() as con:
            con.execute(
                "UPDATE subnet_nodes SET last_seen=?, updated_at=? WHERE node_id=?",
                (float(last_seen), _now(), node_id),
            )
            con.commit()
        if capacity:
            self.replace_io_capacity(node_id, capacity.get("io") or [])
            self.replace_skill_capacity(node_id, capacity.get("skills") or [])

    def list_nodes(self) -> List[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT node_id, subnet_id, roles_json, hostname, base_url, last_seen, created_at, updated_at FROM subnet_nodes"
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
                        "last_seen": r[5],
                        "created_at": r[6],
                        "updated_at": r[7],
                    }
                )
            return rows

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT node_id, subnet_id, roles_json, hostname, base_url, last_seen, created_at, updated_at FROM subnet_nodes WHERE node_id=?",
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
                "last_seen": r[5],
                "created_at": r[6],
                "updated_at": r[7],
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
            "SELECT n.node_id, n.subnet_id, n.roles_json, n.hostname, n.base_url, n.last_seen, s.version, s.active "
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
                        "last_seen": r[5],
                        "version": r[6],
                        "active": bool(r[7]),
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
