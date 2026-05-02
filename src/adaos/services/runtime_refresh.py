from __future__ import annotations

import asyncio
from typing import Any

def _default_webspace_id() -> str:
    from adaos.services.yjs.webspace import default_webspace_id

    return default_webspace_id()


async def rebuild_webspace_projection(
    *,
    webspace_id: str | None = None,
    action: str,
    source_of_truth: str,
) -> dict[str, Any]:
    from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources

    target_webspace = str(webspace_id or "").strip() or _default_webspace_id()
    await rebuild_webspace_from_sources(
        target_webspace,
        action=str(action or "").strip() or "runtime_refresh",
        source_of_truth=str(source_of_truth or "").strip() or "skill_runtime",
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace,
        "action": str(action or "").strip() or "runtime_refresh",
        "source_of_truth": str(source_of_truth or "").strip() or "skill_runtime",
    }


def rebuild_webspace_projection_sync(
    *,
    webspace_id: str | None = None,
    action: str,
    source_of_truth: str,
) -> dict[str, Any]:
    return asyncio.run(
        rebuild_webspace_projection(
            webspace_id=webspace_id,
            action=action,
            source_of_truth=source_of_truth,
        )
    )


def refresh_skill_runtime(
    mgr: Any,
    skill_name: str,
    *,
    webspace_id: str | None = None,
    source_version: str | None = None,
    migrate_runtime: bool = True,
    ensure_installed: bool = False,
) -> dict[str, Any]:
    target_webspace = str(webspace_id or "").strip() or _default_webspace_id()
    payload: dict[str, Any] = {
        "skill": str(skill_name or "").strip(),
        "webspace_id": target_webspace,
        "runtime_updated": False,
        "runtime_migrated": False,
    }
    runtime_status_before: dict[str, Any] = {}
    try:
        runtime_status_before = mgr.runtime_status(skill_name)
    except Exception:
        runtime_status_before = {}
    runtime_version_before = str(runtime_status_before.get("version") or "").strip()
    try:
        runtime_result = mgr.runtime_update(skill_name, space="workspace")
        payload["runtime_updated"] = True
        payload["runtime_update_result"] = runtime_result
    except Exception as exc:
        payload["runtime_update_error"] = str(exc)
        runtime_result = {}
    should_prepare = False
    if source_version is not None:
        should_prepare = bool(str(source_version or "").strip() and str(source_version or "").strip() != runtime_version_before)
    if isinstance(runtime_result, dict) and not bool(runtime_result.get("ok", True)):
        should_prepare = True
    if migrate_runtime and should_prepare:
        if ensure_installed:
            mgr.install(skill_name, validate=False)
        runtime = mgr.prepare_runtime(skill_name, run_tests=False)
        version = getattr(runtime, "version", None)
        slot = getattr(runtime, "slot", None)
        active_slot = mgr.activate_for_space(
            skill_name,
            version=version,
            slot=slot,
            space="default",
            webspace_id=target_webspace,
        )
        payload["runtime_migrated"] = True
        payload["migrated_version"] = version
        payload["migrated_slot"] = active_slot
    return payload
