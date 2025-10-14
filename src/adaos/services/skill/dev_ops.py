"""Developer-facing skill lifecycle helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from adaos.services.agent_context import get_ctx
from adaos.services.fs.safe_io import remove_tree

from ..dev_core import events, manifest, paths, semver, templates
from ..dev_core.errors import EDevNotFound
from ..dev_core.types import (
    CreateInput,
    CreateResult,
    DeleteResult,
    DevContext,
    DevItem,
    Issue,
    ListResult,
    PublishResult,
    PushInput,
    PushResult,
    RunResult,
    SetupResult,
    TestResult,
    ValidateResult,
)
from ..dev_core.types import Kind
from ..dev_core.types import PublishInput, RunInput, SetupInput, TestInput, ValidateInput
from ..dev_core.types import DeleteInput, ListInput
from adaos.services.skill.tests_runner import run_tests
from adaos.services.skill.validation import SkillValidationService


def _item_root(ctx: DevContext, kind: Kind, name: str) -> Path:
    root = Path(paths.dev_root(ctx)) / f"{kind}s" / name
    return root


def _ensure_item(ctx: DevContext, kind: Kind, name: str) -> Path:
    root = _item_root(ctx, kind, name)
    if not root.exists():
        raise EDevNotFound(f"{kind} '{name}' not found in dev workspace")
    return root


def _build_item(ctx: DevContext, kind: Kind, name: str, root: Path) -> DevItem:
    data = manifest.read(str(root), kind)
    manifest_path = manifest.manifest_path(str(root), kind)
    updated_at: Optional[str] = None
    try:
        ts = max(manifest_path.stat().st_mtime, root.stat().st_mtime)
        updated_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except FileNotFoundError:
        pass
    return DevItem(
        kind=kind,
        name=name,
        path=str(root),
        version=str(data.get("version")) if data.get("version") is not None else None,
        prototype=str(data.get("prototype")) if data.get("prototype") is not None else None,
        updated_at=updated_at,
    )


def create(input: CreateInput) -> CreateResult:
    ctx = input.ctx
    kind = input.kind
    target = _item_root(ctx, kind, input.name)
    if target.exists():
        raise FileExistsError(f"{kind} '{input.name}' already exists")
    source = templates.resolve_source(kind, input.template)
    templates.copy_to(source["path"], str(target))
    prototype = None
    if source["source"] == "installed":
        version = source.get("version")
        if version:
            prototype = f"installed:{source['name']}@{version}"
        else:
            prototype = f"installed:{source['name']}"
    else:
        prototype = f"builtin:{source['name']}"
    manifest.update_name_and_prototype(str(target), kind, input.name, prototype)
    item = _build_item(ctx, kind, input.name, target)
    payload = {
        "kind": kind,
        "name": input.name,
        "path": str(target),
        "version": item.version,
        "prototype": item.prototype,
        "subnet_id": ctx.subnet_id,
        "user": ctx.user,
    }
    event_id = events.emit_created(payload)
    hints = [
        f"Run 'adaos dev {kind} validate {input.name}'",
        f"Run 'adaos dev {kind} test {input.name}'",
        f"Run 'adaos dev {kind} push {input.name}'",
    ]
    return CreateResult(item=item, hints=hints, emitted=[event_id])


def list(input: ListInput) -> ListResult:
    ctx = input.ctx
    kind = input.kind
    base = Path(paths.dev_root(ctx)) / f"{kind}s"
    items: List[DevItem] = []
    if base.exists():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            items.append(_build_item(ctx, kind, entry.name, entry))
    return ListResult(items=items)


def delete(input: DeleteInput) -> DeleteResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    item = _build_item(ctx, kind, input.name, root)
    remove_tree(str(root), get_ctx().fs)
    payload = {
        "kind": kind,
        "name": input.name,
        "path": str(root),
        "version": item.version,
        "prototype": item.prototype,
        "subnet_id": ctx.subnet_id,
        "user": ctx.user,
    }
    event_id = events.emit_deleted(payload)
    return DeleteResult(item=item, removed=True, emitted=[event_id])


def validate(input: ValidateInput) -> ValidateResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    agent_ctx = get_ctx()
    skill_ctx = agent_ctx.skill_ctx
    previous = skill_ctx.get()
    skill_ctx.set(input.name, root)
    try:
        report = SkillValidationService(agent_ctx).validate()
    finally:
        if previous is None:
            skill_ctx.clear()
        else:
            skill_ctx.set(previous.name, Path(previous.path))
    issues: List[Issue] = []
    for issue in report.issues:
        issues.append(Issue(path=getattr(issue, "where", None), message=issue.message, code=issue.code))
    item = _build_item(ctx, kind, input.name, root)
    return ValidateResult(item=item, ok=report.ok, issues=issues)


def test(input: TestInput) -> TestResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    logs_dir = root / ".adaos"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "tests.log"
    suites = run_tests(
        root,
        log_path=log_path,
        extra_env={"ADAOS_DEV_MODE": "1"},
        python_paths=[str(root)],
        skill_name=input.name,
    )
    summary: Dict[str, Dict[str, str]] = {}
    ok = True
    for key, value in suites.items():
        summary[key] = {"status": value.status, "detail": value.detail or ""}
        if value.status == "failed":
            ok = False
    item = _build_item(ctx, kind, input.name, root)
    return TestResult(item=item, ok=ok, suites=summary, log_path=str(log_path))


def setup(input: SetupInput) -> SetupResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    prep = root / "prep" / "prepare.py"
    if not prep.exists():
        item = _build_item(ctx, kind, input.name, root)
        return SetupResult(item=item, ok=False, detail="prep/prepare.py not found")
    spec = None
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"adaos_dev_prep_{input.name}", prep)
    if spec is None or spec.loader is None:
        item = _build_item(ctx, kind, input.name, root)
        return SetupResult(item=item, ok=False, detail="unable to load preparation script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    run_prep = getattr(module, "run_prep", None)
    if run_prep is None:
        item = _build_item(ctx, kind, input.name, root)
        return SetupResult(item=item, ok=False, detail="run_prep() not defined")
    result = run_prep(root)
    detail = json.dumps(result, ensure_ascii=False, default=str) if result is not None else None
    item = _build_item(ctx, kind, input.name, root)
    return SetupResult(item=item, ok=True, detail=detail)


def run(input: RunInput) -> RunResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    args = input.args or []
    if args:
        command = args
    else:
        command = [sys.executable, "handlers/main.py"]
    proc = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_path = root / ".adaos" / "run.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(proc.stdout or "", encoding="utf-8")
    item = _build_item(ctx, kind, input.name, root)
    return RunResult(item=item, exit_code=proc.returncode, output_path=str(output_path))


def push(input: PushInput) -> PushResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    data = manifest.read(str(root), kind)
    previous = data.get("version")
    new_version = semver.bump(previous, input.bump)
    data["version"] = new_version
    if not input.dry_run:
        manifest.write(str(root), kind, data)
    item = _build_item(ctx, kind, input.name, root)
    payload = {
        "kind": kind,
        "name": input.name,
        "path": str(root),
        "version": new_version,
        "previous_version": previous,
        "prototype": item.prototype,
        "subnet_id": ctx.subnet_id,
        "user": ctx.user,
        "dry_run": input.dry_run,
    }
    event_id = events.emit_pushed(payload)
    return PushResult(
        item=item,
        ok=True,
        version=new_version,
        previous_version=previous if previous is not None else None,
        dry_run=input.dry_run,
        upload_metadata=None,
        emitted=[event_id],
    )


def publish(input: PublishInput) -> PublishResult:
    ctx = input.ctx
    kind = input.kind
    root = _ensure_item(ctx, kind, input.name)
    data = manifest.read(str(root), kind)
    previous = data.get("version")
    new_version = semver.bump(previous, input.bump) if input.bump else previous
    if new_version:
        data["version"] = new_version
        if not input.dry_run:
            manifest.write(str(root), kind, data)
    item = _build_item(ctx, kind, input.name, root)
    payload = {
        "kind": kind,
        "name": input.name,
        "path": str(root),
        "version": new_version,
        "previous_version": previous,
        "prototype": item.prototype,
        "subnet_id": ctx.subnet_id,
        "user": ctx.user,
        "dry_run": input.dry_run,
        "force": input.force,
    }
    event_id = events.emit_registry_published(payload)
    return PublishResult(
        item=item,
        ok=True,
        version=str(new_version) if new_version is not None else "",
        previous_version=previous if previous is not None else None,
        dry_run=input.dry_run,
        emitted=[event_id],
    )
