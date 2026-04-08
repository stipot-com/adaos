from __future__ import annotations

import argparse
import json
from typing import Any

from adaos.adapters.db import SqliteSkillRegistry
from adaos.apps.bootstrap import init_ctx
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and activate installed skill runtimes in current interpreter")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    return parser.parse_args()


def _manager() -> SkillManager:
    ctx = get_ctx()
    return SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
    )


def migrate_installed_skills() -> dict[str, Any]:
    init_ctx()
    ctx = get_ctx()
    mgr = _manager()
    reg = SqliteSkillRegistry(ctx.sql)
    items: list[dict[str, Any]] = []
    rows = reg.list()
    for row in rows:
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if not name or not bool(getattr(row, "installed", True)):
            continue
        entry: dict[str, Any] = {"skill": str(name), "ok": True}
        try:
            runtime = mgr.prepare_runtime(str(name), run_tests=False)
            entry["prepared_version"] = getattr(runtime, "version", None)
            entry["prepared_slot"] = getattr(runtime, "slot", None)
            active_slot = mgr.activate_runtime(
                str(name),
                version=getattr(runtime, "version", None),
                slot=getattr(runtime, "slot", None),
            )
            entry["active_slot"] = active_slot
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
        items.append(entry)
    ok = all(bool(item.get("ok")) for item in items)
    return {"ok": ok, "skills": items}


def main() -> None:
    args = _parse_args()
    payload = migrate_installed_skills()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
