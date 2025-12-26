# src/adaos/adapters/db/sqlite_scenario_registry.py
from __future__ import annotations
import datetime
import json
import uuid
from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.domain import SkillRecord
from adaos.ports import SQL

class SqliteScenarioRegistry:
    """Реестр сценариев на таблицах `scenarios`/`scenario_versions`."""

    def __init__(self, sql: SQL):
        self.sql = sql
        ensure_schema(self.sql)

    def list(self) -> list[SkillRecord]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, active_version, repo_url, installed, " "strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP)) " "FROM scenarios WHERE installed = 1 ORDER BY name"
            )
            rows = cur.fetchall()
        return [
            SkillRecord(
                name=row[0],
                installed=bool(row[3]),
                active_version=row[1],
                repo_url=row[2],
                last_updated=float(row[4]) if row[4] is not None else None,
            )
            for row in rows
        ]

    def get(self, name: str) -> SkillRecord | None:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, active_version, repo_url, installed, " "strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP)) " "FROM scenarios WHERE name = ?", (name,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return SkillRecord(
            name=row[0],
            installed=bool(row[3]),
            active_version=row[1],
            repo_url=row[2],
            last_updated=float(row[4]) if row[4] is not None else None,
        )

    def register(self, name: str, *, pin: str | None = None, active_version: str | None = None, repo_url: str | None = None) -> SkillRecord:
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO scenarios(name, active_version, repo_url, installed, last_updated)
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET
                    active_version = COALESCE(?, scenarios.active_version),
                    repo_url       = COALESCE(?, scenarios.repo_url),
                    installed      = 1,
                    last_updated   = CURRENT_TIMESTAMP
                """,
                (name, active_version, repo_url, active_version, repo_url),
            )
            con.commit()
        rec = self.get(name)
        return SkillRecord(
            name=name,
            installed=True,
            active_version=rec.active_version if rec else active_version,
            repo_url=rec.repo_url if rec else repo_url,
            pin=pin,
            last_updated=rec.last_updated if rec else None,
        )

    def unregister(self, name: str) -> None:
        with self.sql.connect() as con:
            con.execute(
                "UPDATE scenarios SET installed = 0, last_updated = CURRENT_TIMESTAMP WHERE name = ?",
                (name,),
            )
            con.commit()

    def set_all(self, records: list[SkillRecord]) -> None:
        names = [(r.name,) for r in records]
        with self.sql.connect() as con:
            con.execute("UPDATE scenarios SET installed = 0, last_updated = CURRENT_TIMESTAMP WHERE installed = 1")
            if names:
                con.executemany(
                    "INSERT INTO scenarios(name, installed, last_updated) VALUES(?, 1, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(name) DO UPDATE SET installed = 1, last_updated = CURRENT_TIMESTAMP",
                    names,
                )
            con.commit()

    def create_task(
            self,
            scenario_id: str,
            priority: str,
            run_state: str,
            ctx = None,
            trace_id: str = None
        ) -> str:
        run_id = str(uuid.uuid4())
        ctx_json = json.dumps(ctx or {}, ensure_ascii=False)
        
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO scenario_runs (
                    run_id,
                    scenario_id,
                    ctx,
                    priority,
                    state,
                    trace_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (run_id, scenario_id, ctx_json, priority, run_state, trace_id),
            )
            con.commit()
        
        return run_id
    
    def get_running_count(self) -> int:
        """Получает количество запущенных (RUNNING) сценариев"""
        with self.sql.connect() as con:
            cursor = con.execute(
                "SELECT COUNT(*) as count FROM scenario_runs WHERE state = 'running'"
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    
    def get_task(self, run_id: str):
        with self.sql.connect() as con:     
            cursor = con.execute("SELECT * FROM scenario_runs WHERE run_id = ?", (run_id, ))
            record = cursor.fetchone()
            print(record)
            return record
        
    def get_next_task(self) -> list:
        with self.sql.connect() as con:
            query = """
            SELECT * FROM scenario_runs 
            WHERE state = 'pending' ORDER BY 
                CASE priority 
                    WHEN 'HIGH' THEN 1
                    WHEN 'NORM' THEN 2
                    WHEN 'LOW' THEN 3
                    ELSE 4
                END,
                created_at ASC LIMIT 1
            """
            
            cursor = con.execute(query)
            record = cursor.fetchone()
            if record:
                return {'run_id': record[0],'scenario_id': record[1], 'ctx': json.loads(record[2]),
                        'priority': record[3]}
                
            

    def update_state(
            self,
            run_id: str,
            state: str = None,
            current_step: str = None,
            started_at: datetime.timestap = None,
            finished_at: datetime.timestap = None,
            cancel_token: bool = None
        ) -> None:
       
        set_parts = []
        params = []

        fields = {'state': state, 'current_step': current_step, 'started_at': started_at, 
                  'finished_at': finished_at, 'cancel_token': cancel_token}

        for key, value in fields.items():
            if value:
                set_parts.append(f'{key} = ?')
                params.append(value)

        if not set_parts:
            return 
        
        params.append(run_id)
        
        with self.sql.connect() as con:
            sql = f"UPDATE scenario_runs SET {', '.join(set_parts)} WHERE run_id = ?"
            print(sql, params)
            cursor = con.execute(sql, params)
            con.commit()

            


    def cancel(self, run_id: str) -> bool:
        with self.sql.connect() as con:
            cursor = con.execute(
                """
                UPDATE scenario_runs 
                SET state = 'cancelled', finished_at = CURRENT_TIMESTAMP
                WHERE run_id = ? AND state IN ('pending', 'running')
                """,
                (run_id,)
            )
            con.commit()
            return cursor.rowcount > 0

