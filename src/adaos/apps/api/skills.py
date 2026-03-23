from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from adaos.adapters.db import SqliteSkillRegistry
from adaos.apps.api.auth import require_token
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.update import SkillUpdateService
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.webspace import default_webspace_id

import yaml
from packaging.version import Version, InvalidVersion


router = APIRouter(tags=["skills"], dependencies=[Depends(require_token)])


def _get_manager(ctx: AgentContext = Depends(get_ctx)) -> SkillManager:
    repo = ctx.skills_repo
    registry = SqliteSkillRegistry(ctx.sql)
    return SkillManager(
        repo=repo,
        registry=registry,
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )


def _to_mapping(obj: Any) -> Dict[str, Any]:
    try:
        return dict(obj)
    except Exception:
        pass
    try:
        return obj._asdict()  # type: ignore[attr-defined]
    except Exception:
        pass
    data: Dict[str, Any] = {}
    for key in ("name", "pin", "last_updated", "id", "path", "version", "active_version"):
        if hasattr(obj, key):
            value = getattr(obj, key)
            if key == "id" and hasattr(value, "value"):
                value = getattr(value, "value")
            data[key] = value
    return data or {"repr": repr(obj)}


class UpdateReq(BaseModel):
    name: str
    dry_run: bool = False


def _safe_version(v: Any) -> Version | None:
    if v is None:
        return None
    raw = str(v).strip()
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _read_remote_manifest_version(ctx: AgentContext, *, skill_id: str) -> str | None:
    """
    Best-effort resolve remote version for a skill without modifying worktree.
    - monorepo: `origin/<branch>:skills/<id>/skill.yaml`
    - standalone: `origin/HEAD:skill.yaml`
    """
    settings = getattr(ctx, "settings", None)
    git = getattr(ctx, "git", None)
    repo = getattr(ctx, "skills_repo", None)
    if git is None or repo is None:
        return None

    meta = repo.get(skill_id)
    if meta is None:
        return None
    local_path = Path(getattr(meta, "path", Path(ctx.paths.skills_dir()) / skill_id))

    monorepo_url = getattr(settings, "skills_monorepo_url", None) if settings else None
    monorepo_branch = (getattr(settings, "skills_monorepo_branch", None) if settings else None) or "main"

    if monorepo_url:
        skills_root = Path(ctx.paths.skills_dir())
        if (skills_root / ".git").exists():
            repo_root = skills_root
        elif (skills_root.parent / ".git").exists():
            repo_root = skills_root.parent
        else:
            return None
        try:
            git.fetch(str(repo_root), remote="origin", branch=monorepo_branch)
        except Exception:
            pass
        candidates = [
            f"origin/{monorepo_branch}:skills/{skill_id}/skill.yaml",
            f"origin/{monorepo_branch}:skills/{skill_id}/manifest.yaml",
            f"origin/{monorepo_branch}:skills/{skill_id}/adaos.skill.yaml",
        ]
    else:
        repo_root = local_path
        if not (repo_root / ".git").exists():
            return None
        try:
            git.fetch(str(repo_root), remote="origin")
        except Exception:
            pass
        candidates = [
            "origin/HEAD:skill.yaml",
            "origin/HEAD:manifest.yaml",
            "origin/HEAD:adaos.skill.yaml",
        ]

    for spec in candidates:
        try:
            raw = git.show(str(repo_root), spec)
        except Exception:
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except Exception:
            continue
        ver = data.get("version")
        if ver is None:
            continue
        s = str(ver).strip()
        if s:
            return s
    return None


class InstallReq(BaseModel):
    name: str
    pin: Optional[str] = None
    perform_validation: bool = False
    strict: bool = True
    probe_tools: bool = False


class PushReq(BaseModel):
    name: str
    message: str
    signoff: bool = False


# --- Runtime management API ---
class RuntimePrepareReq(BaseModel):
    name: str
    run_tests: bool = False
    slot: str | None = None


class RuntimeActivateReq(BaseModel):
    name: str
    slot: str | None = None
    version: str | None = None
    auto_prepare: bool = True
    webspace_id: str | None = "default"


class RuntimeNotifyActivatedReq(BaseModel):
    name: str
    space: str | None = "default"
    webspace_id: str | None = None


class RuntimeSetupReq(BaseModel):
    name: str


@router.get("/list")
async def list_skills(fs: bool = False, mgr: SkillManager = Depends(_get_manager)):
    rows = mgr.list_installed()
    items = [_to_mapping(r) for r in (rows or []) if bool(getattr(r, "installed", True))]
    result: Dict[str, Any] = {"items": items}
    if fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {(i.get("name") or i.get("id") or i.get("repr")) for i in items}
        missing = sorted(desired - present)
        extra = sorted(present - desired)
        result["fs"] = {
            "present": sorted(present),
            "missing": missing,
            "extra": extra,
        }
    return result


@router.get("/installed-status")
async def installed_status(mgr: SkillManager = Depends(_get_manager), ctx: AgentContext = Depends(get_ctx)):
    """
    Installed skills with runtime slot and update hint (remote version > local version).
    """
    rows = mgr.list_installed()
    items: list[dict[str, Any]] = []

    for row in (rows or []):
        if not bool(getattr(row, "installed", True)):
            continue
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue

        meta = mgr.get(name)
        local_version = (getattr(meta, "version", None) if meta else None) or getattr(row, "active_version", None)
        local_version_s = str(local_version).strip() if local_version is not None else ""

        slot = ""
        try:
            st = mgr.runtime_status(name)
            slot = str(st.get("active_slot") or "").strip()
        except Exception:
            slot = ""

        remote_version_s = _read_remote_manifest_version(ctx, skill_id=name) or ""

        update_available = False
        lv = _safe_version(local_version_s)
        rv = _safe_version(remote_version_s)
        if lv is not None and rv is not None and rv > lv:
            update_available = True

        items.append(
            {
                "name": name,
                "version": local_version_s,
                "slot": slot,
                "remote_version": remote_version_s,
                "update_available": update_available,
            }
        )

    return {"ok": True, "items": items}


@router.post("/sync")
async def sync(mgr: SkillManager = Depends(_get_manager)):
    mgr.sync()
    return {"ok": True}


@router.post("/install")
async def install(body: InstallReq, mgr: SkillManager = Depends(_get_manager)):
    # Best-effort sync to ensure monorepo workspace exists
    try:
        mgr.sync()
    except Exception:
        pass
    try:
        result = mgr.install(
            body.name,
            pin=body.pin,
            validate=body.perform_validation,
            strict=body.strict,
            probe_tools=body.probe_tools,
        )
    except FileNotFoundError:
        # Retry once after an explicit sync in case the repo was missing
        mgr.sync()
        result = mgr.install(
            body.name,
            pin=body.pin,
            validate=body.perform_validation,
            strict=body.strict,
            probe_tools=body.probe_tools,
        )
    if isinstance(result, tuple):
        meta, report = result
    else:
        meta, report = result, None
    payload: Dict[str, Any] = {
        "ok": True,
        "skill": {
            "id": getattr(meta, "id", None).value if getattr(meta, "id", None) else body.name,
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
        },
    }
    if report is not None:
        if hasattr(report, "to_dict"):
            payload["report"] = report.to_dict()  # type: ignore[call-arg]
        else:
            payload["report"] = repr(report)
    return payload


@router.post("/uninstall")
async def uninstall(body: InstallReq, mgr: SkillManager = Depends(_get_manager)):
    mgr.uninstall(
        body.name,
    )
    return {"ok": True}


@router.get("/{name}")
async def get_skill(name: str, mgr: SkillManager = Depends(_get_manager)):
    meta = mgr.get(name)
    if not meta:
        return {"ok": False, "reason": "not-found"}
    return {"ok": True, "skill": _to_mapping(meta)}


@router.delete("/{name}")
async def remove(name: str, mgr: SkillManager = Depends(_get_manager)):
    mgr.uninstall(name)
    return {"ok": True}


@router.post("/push")
async def push(body: PushReq, mgr: SkillManager = Depends(_get_manager)):
    revision = mgr.push(body.name, body.message, signoff=body.signoff)
    return {"ok": True, "revision": revision}


# --- Runtime management endpoints ---


@router.post("/runtime/prepare")
async def runtime_prepare(body: RuntimePrepareReq, mgr: SkillManager = Depends(_get_manager)):
    result = mgr.prepare_runtime(body.name, run_tests=body.run_tests, preferred_slot=body.slot)
    payload = {
        "ok": True,
        "name": result.name,
        "version": result.version,
        "slot": result.slot,
        "resolved_manifest": str(result.resolved_manifest),
        "tests": {k: v.status for k, v in (result.tests or {}).items()},
    }
    return payload


@router.post("/runtime/activate")
async def runtime_activate(body: RuntimeActivateReq, mgr: SkillManager = Depends(_get_manager)):
    webspace_id = body.webspace_id or "default"
    try:
        slot = mgr.activate_for_space(body.name, version=body.version, slot=body.slot, space="default", webspace_id=webspace_id)
        return {"ok": True, "slot": slot}
    except RuntimeError as exc:
        msg = str(exc).lower()
        if not body.auto_prepare or ("is not prepared" not in msg and "no installed versions" not in msg):
            # expose as 422 Unprocessable if activation cannot proceed
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=str(exc))
        # auto-prepare then retry
        pref_slot = body.slot
        prep = mgr.prepare_runtime(body.name, run_tests=False, preferred_slot=pref_slot)
        slot = mgr.activate_for_space(body.name, version=prep.version, slot=prep.slot, space="default", webspace_id=webspace_id)
        return {"ok": True, "slot": slot, "prepared": prep.slot}


@router.post("/runtime/notify-activated")
async def runtime_notify_activated(body: RuntimeNotifyActivatedReq):
    """
    Lightweight hook to broadcast a skills.activated event on the hub bus
    without touching runtime slots (used by CLI after local activation).
    """
    ctx = get_ctx()
    bus = getattr(ctx, "bus", None)
    if bus is None:
        return {"ok": False, "reason": "bus-unavailable"}
    space = (body.space or "default").strip() or "default"
    webspace_id = body.webspace_id or default_webspace_id()
    payload: Dict[str, Any] = {"skill_name": body.name, "space": space, "webspace_id": webspace_id}
    bus_emit(bus, "skills.activated", payload, "api.skills")
    return {"ok": True}


@router.get("/runtime/status/{name}")
async def runtime_status(name: str, mgr: SkillManager = Depends(_get_manager)):
    state = mgr.runtime_status(name)
    return {"ok": True, "state": state}


@router.post("/runtime/setup")
async def runtime_setup(body: RuntimeSetupReq, mgr: SkillManager = Depends(_get_manager)):
    result = mgr.setup_skill(body.name)
    if isinstance(result, dict):
        return {"ok": bool(result.get("ok", True)), **result}
    return {"ok": True, "result": result}


@router.post("/update")
async def update_skill(body: UpdateReq, ctx: AgentContext = Depends(get_ctx)):
    service = SkillUpdateService(ctx)
    result = service.request_update(body.name, dry_run=body.dry_run)
    return {"ok": True, "updated": result.updated, "version": result.version}
