from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional
from adaos.services.scenarios.loader import read_manifest


ProjectionBackend = Literal["yjs", "kv", "sql"]


@dataclass(slots=True)
class ProjectionTarget:
    """
    Single physical projection target for a (scope, slot) pair.

    backend:
      - "yjs"  — project into a YDoc path,
      - "kv"   — project into a KV key,
      - "sql"  — project into a SQL table/column (reserved for future use).
    """

    backend: ProjectionBackend
    webspace_id: Optional[str] = None
    path: Optional[str] = None
    table: Optional[str] = None
    column: Optional[str] = None


@dataclass(slots=True)
class ProjectionRule:
    scope: str
    slot: str
    targets: List[ProjectionTarget]


class ProjectionRegistry:
    """
    Registry that maps (scope, slot) pairs used by ctx.*.set/get to
    concrete storage targets (Yjs paths, KV keys, SQL rows, ...).

    For the MVP this is a lightweight, read-only facade over scenario
    manifests: if a scenario.yaml defines a `data_projections` section,
    entries from there are loaded into this registry.
    """

    def __init__(self) -> None:
        self._rules: Dict[tuple[str, str], ProjectionRule] = {}

    def load_from_scenario(self, scenario_id: str) -> None:
        """
        Load projection rules from scenario.yaml for the given scenario id.

        Expected shape (optional) inside scenario.yaml:

        data_projections:
          - scope: subnet
            slot: weather.snapshot
            targets:
              - backend: yjs
                webspace_id: desktop
                path: data/skills/weather/global/snapshot
        """
        manifest = read_manifest(scenario_id)
        raw = manifest.get("data_projections") or []
        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, dict):
                continue

            scope = str(item.get("scope") or "").strip()
            slot = str(item.get("slot") or "").strip()
            if not scope or not slot:
                continue

            targets_raw = item.get("targets") or []
            if not isinstance(targets_raw, list):
                continue

            targets: List[ProjectionTarget] = []
            for t in targets_raw:
                if not isinstance(t, dict):
                    continue
                backend = str(t.get("backend") or "").strip().lower()
                if backend not in ("yjs", "kv", "sql"):
                    continue
                targets.append(
                    ProjectionTarget(
                        backend=backend,  # type: ignore[arg-type]
                        webspace_id=str(t.get("webspace_id") or "") or None,
                        path=str(t.get("path") or "") or None,
                        table=str(t.get("table") or "") or None,
                        column=str(t.get("column") or "") or None,
                    )
                )
            key = (scope, slot)
            if targets:
                self._rules[key] = ProjectionRule(scope=scope, slot=slot, targets=targets)

    def resolve(self, scope: str, slot: str) -> List[ProjectionTarget]:
        """
        Resolve a (scope, slot) pair to a list of projection targets.

        If no rule is present, returns an empty list; callers should treat
        this as "no projections configured".
        """
        key = (str(scope).strip(), str(slot).strip())
        rule = self._rules.get(key)
        return list(rule.targets) if rule else []


__all__ = ["ProjectionBackend", "ProjectionTarget", "ProjectionRule", "ProjectionRegistry"]
