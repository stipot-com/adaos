"""Shared type definitions for developer workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

Kind = Literal["skill", "scenario"]
Bump = Literal["patch", "minor", "major"]


@dataclass(slots=True)
class DevContext:
    """Context describing the active developer session."""

    subnet_id: str
    user: Optional[str] = None


@dataclass(slots=True)
class DevItem:
    """Representation of an artifact located inside the dev workspace."""

    kind: Kind
    name: str
    path: str
    version: Optional[str] = None
    prototype: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(slots=True)
class CreateInput:
    ctx: DevContext
    kind: Kind
    name: str
    template: Optional[str] = None


@dataclass(slots=True)
class Issue:
    path: Optional[str]
    message: str
    code: Optional[str] = None


@dataclass(slots=True)
class CreateResult:
    item: DevItem
    hints: List[str]
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class ListInput:
    ctx: DevContext
    kind: Kind


@dataclass(slots=True)
class ListResult:
    items: List[DevItem]
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class DeleteInput:
    ctx: DevContext
    kind: Kind
    name: str
    force: bool = False


@dataclass(slots=True)
class DeleteResult:
    item: DevItem
    removed: bool
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidateInput:
    ctx: DevContext
    kind: Kind
    name: str


@dataclass(slots=True)
class ValidateResult:
    item: DevItem
    ok: bool
    issues: List[Issue]
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class TestInput:
    ctx: DevContext
    kind: Kind
    name: str
    args: Optional[List[str]] = None


@dataclass(slots=True)
class TestResult:
    item: DevItem
    ok: bool
    suites: Dict[str, Dict[str, str]]
    log_path: Optional[str] = None
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class SetupInput:
    ctx: DevContext
    kind: Kind
    name: str
    args: Optional[List[str]] = None


@dataclass(slots=True)
class SetupResult:
    item: DevItem
    ok: bool
    detail: Optional[str] = None
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class RunInput:
    ctx: DevContext
    kind: Kind
    name: str
    args: Optional[List[str]] = None


@dataclass(slots=True)
class RunResult:
    item: DevItem
    exit_code: int
    output_path: Optional[str] = None
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class PushInput:
    ctx: DevContext
    kind: Kind
    name: str
    bump: Bump = "patch"
    dry_run: bool = False


@dataclass(slots=True)
class PushResult:
    item: DevItem
    ok: bool
    version: str
    previous_version: Optional[str]
    dry_run: bool
    upload_metadata: Optional[Dict[str, str]] = None
    emitted: List[str] = field(default_factory=list)


@dataclass(slots=True)
class PublishInput:
    ctx: DevContext
    kind: Kind
    name: str
    bump: Bump = "patch"
    force: bool = False
    dry_run: bool = False


@dataclass(slots=True)
class PublishResult:
    item: DevItem
    ok: bool
    version: str
    previous_version: Optional[str]
    dry_run: bool
    emitted: List[str] = field(default_factory=list)
