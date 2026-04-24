from __future__ import annotations

import argparse
import json
from typing import Any

from adaos.adapters.db import SqliteSkillRegistry
from adaos.apps.bootstrap import init_ctx
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare installed skill runtimes for the current core interpreter")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    parser.add_argument("--skip-tests", action="store_true", help="Skip post-activation skill tests")
    parser.add_argument("--post-commit", action="store_true", help="Run post-commit checks against active skill runtimes")
    parser.add_argument("--deactivate-on-failure", action="store_true", help="Deactivate failing skills during post-commit checks")
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


def _status_value(result: Any) -> str:
    return str(getattr(result, "status", result) or "").strip().lower()


def _tests_payload(results: dict[str, Any]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for name, result in (results or {}).items():
        payload[str(name)] = _status_value(result) or "unknown"
    return payload


def _tests_ok(results: dict[str, Any]) -> bool:
    return all(status == "passed" for status in _tests_payload(results).values())


def _lifecycle_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lifecycle_failure_stage(lifecycle: dict[str, Any]) -> str:
    data = _lifecycle_payload(lifecycle)
    for key in ("persist", "rehydrate", "healthcheck", "drain", "dispose", "before_deactivate", "rollback"):
        payload = _lifecycle_payload(data.get(key))
        if not payload:
            continue
        if bool(payload.get("skipped")):
            continue
        ok_value = payload.get("ok")
        if ok_value is False:
            return key
    return ""


def _safe_for_core_update(items: list[dict[str, Any]]) -> bool:
    failures = [item for item in items if not bool(item.get("ok"))]
    if not failures:
        return True
    for item in failures:
        stage = str(item.get("failed_stage") or item.get("stage") or "").strip().lower()
        if bool(item.get("rollback_performed")) or bool(item.get("deactivated")):
            continue
        if stage in {"prepare", "activate"}:
            continue
        return False
    return True


def _runtime_status_safe(mgr: SkillManager, name: str) -> dict[str, Any]:
    try:
        payload = mgr.runtime_status(name)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def migrate_installed_skills(*, run_tests: bool = True) -> dict[str, Any]:
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
        skill_name = str(name)
        before = _runtime_status_safe(mgr, skill_name)
        entry: dict[str, Any] = {
            "skill": skill_name,
            "ok": True,
            "failure_kind": "",
            "failed_stage": "",
            "prepared_version": None,
            "prepared_slot": None,
            "active_version_before": str(before.get("version") or ""),
            "active_slot_before": str(before.get("active_slot") or ""),
            "active_slot_after": "",
            "tests": {},
            "lifecycle": {},
            "rollback_performed": False,
            "deactivated": False,
        }
        activated = False
        try:
            entry["stage"] = "prepare"
            runtime = mgr.prepare_runtime(skill_name, run_tests=False)
            entry["prepared_version"] = getattr(runtime, "version", None)
            entry["prepared_slot"] = getattr(runtime, "slot", None)

            entry["stage"] = "activate"
            active_slot = mgr.activate_runtime(
                skill_name,
                version=getattr(runtime, "version", None),
                slot=getattr(runtime, "slot", None),
            )
            activated = True
            entry["active_slot_after"] = str(active_slot or "")
            after = _runtime_status_safe(mgr, skill_name)
            entry["lifecycle"] = dict(after.get("lifecycle") or {}) if isinstance(after, dict) else {}
            lifecycle_failed_stage = _lifecycle_failure_stage(entry["lifecycle"])
            if lifecycle_failed_stage:
                entry["stage"] = lifecycle_failed_stage
                raise RuntimeError(f"lifecycle check failed at {lifecycle_failed_stage}")

            if run_tests:
                entry["stage"] = "tests"
                tests = mgr.run_skill_tests(skill_name, source="installed")
                entry["tests"] = _tests_payload(tests)
                if not _tests_ok(tests):
                    raise RuntimeError("skill tests failed")

            entry["stage"] = "completed"
        except Exception as exc:
            stage = str(entry.get("stage") or "prepare")
            entry["ok"] = False
            entry["failed_stage"] = stage
            entry["failure_kind"] = "lifecycle" if stage in {"persist", "rehydrate", "healthcheck", "drain", "dispose", "before_deactivate", "rollback"} else "tests" if stage == "tests" else "prepare"
            entry["error"] = str(exc)
            if activated and stage in {"activate", "tests"}:
                try:
                    entry["stage"] = "rollback"
                    restored_slot = mgr.rollback_runtime(skill_name)
                    entry["rollback_performed"] = True
                    entry["rollback_slot"] = str(restored_slot or "")
                except Exception as rollback_exc:
                    entry["rollback_error"] = str(rollback_exc)
            entry["stage"] = "failed"
        items.append(entry)

    failed = [item for item in items if not bool(item.get("ok"))]
    rollback_total = sum(1 for item in items if bool(item.get("rollback_performed")))
    deactivated_total = sum(1 for item in items if bool(item.get("deactivated")))
    lifecycle_failed_total = sum(1 for item in items if str(item.get("failure_kind") or "") == "lifecycle")
    tests_failed_total = sum(1 for item in items if str(item.get("failure_kind") or "") == "tests")
    return {
        "ok": not failed,
        "total": len(items),
        "failed_total": len(failed),
        "rollback_total": rollback_total,
        "deactivated_total": deactivated_total,
        "lifecycle_failed_total": lifecycle_failed_total,
        "tests_failed_total": tests_failed_total,
        "run_tests": bool(run_tests),
        "safe_for_core_update": _safe_for_core_update(items),
        "skills": items,
    }


def post_commit_check_installed_skills(*, deactivate_on_failure: bool = False) -> dict[str, Any]:
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
        skill_name = str(name)
        status = _runtime_status_safe(mgr, skill_name)
        entry: dict[str, Any] = {
            "skill": skill_name,
            "ok": True,
            "failure_kind": "",
            "failed_stage": "",
            "active_version": str(status.get("version") or ""),
            "active_slot": str(status.get("active_slot") or ""),
            "tests": {},
            "lifecycle": dict(status.get("lifecycle") or {}) if isinstance(status, dict) else {},
            "deactivated": False,
            "skipped": False,
        }
        if bool(status.get("deactivated")):
            entry["skipped"] = True
            entry["reason"] = str((status.get("deactivation") or {}).get("reason") or "already deactivated")
            items.append(entry)
            continue
        try:
            lifecycle_failed_stage = _lifecycle_failure_stage(entry["lifecycle"])
            if lifecycle_failed_stage:
                entry["stage"] = lifecycle_failed_stage
                raise RuntimeError(f"lifecycle check failed at {lifecycle_failed_stage}")
            entry["stage"] = "tests"
            tests = mgr.run_skill_tests(skill_name, source="installed")
            entry["tests"] = _tests_payload(tests)
            if not _tests_ok(tests):
                raise RuntimeError("skill tests failed")
            entry["stage"] = "completed"
        except Exception as exc:
            entry["ok"] = False
            entry["failed_stage"] = str(entry.get("stage") or "tests")
            entry["failure_kind"] = (
                "lifecycle"
                if entry["failed_stage"] in {"persist", "rehydrate", "healthcheck", "drain", "dispose", "before_deactivate", "rollback"}
                else "tests"
            )
            entry["error"] = str(exc)
            if deactivate_on_failure:
                try:
                    deactivated = mgr.deactivate_runtime(skill_name, reason="post_commit_checks_failed")
                    entry["deactivated"] = True
                    entry["deactivation"] = deactivated
                except Exception as deactivate_exc:
                    entry["deactivate_error"] = str(deactivate_exc)
            entry["stage"] = "failed"
        items.append(entry)

    failed = [item for item in items if not bool(item.get("ok"))]
    deactivated_total = sum(1 for item in items if bool(item.get("deactivated")))
    skipped_total = sum(1 for item in items if bool(item.get("skipped")))
    lifecycle_failed_total = sum(1 for item in items if str(item.get("failure_kind") or "") == "lifecycle")
    tests_failed_total = sum(1 for item in items if str(item.get("failure_kind") or "") == "tests")
    return {
        "ok": not failed,
        "total": len(items),
        "failed_total": len(failed),
        "deactivated_total": deactivated_total,
        "skipped_total": skipped_total,
        "lifecycle_failed_total": lifecycle_failed_total,
        "tests_failed_total": tests_failed_total,
        "deactivate_on_failure": bool(deactivate_on_failure),
        "safe_for_core_update": _safe_for_core_update(items),
        "skills": items,
    }


def main() -> None:
    args = _parse_args()
    if bool(args.post_commit):
        payload = post_commit_check_installed_skills(deactivate_on_failure=bool(args.deactivate_on_failure))
    else:
        payload = migrate_installed_skills(run_tests=not bool(args.skip_tests))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
