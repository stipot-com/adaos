"""File-backed secrets backend scoped to a single skill runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable

from adaos.ports.secrets import SecretScope, Secrets


class SkillSecretsBackend(Secrets):
    """Persist secrets for a skill inside the runtime data directory."""

    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if not self._path.exists():
            return {"profile": {}, "global": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"profile": {}, "global": {}}

    def _save(self, payload: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def put(self, key: str, value: str, *, scope: SecretScope = "profile", meta: Dict[str, Any] | None = None) -> None:
        data = self._load()
        bucket = data.setdefault(scope, {})
        bucket[key] = {"value": value, "meta": meta or {}}
        self._save(data)

    def get(self, key: str, *, default: str | None = None, scope: SecretScope = "profile") -> str | None:
        data = self._load()
        bucket = data.get(scope, {})
        record = bucket.get(key)
        if not isinstance(record, dict):
            return default
        return record.get("value", default)

    def delete(self, key: str, *, scope: SecretScope = "profile") -> None:
        data = self._load()
        bucket = data.get(scope, {})
        if key in bucket:
            bucket.pop(key)
            self._save(data)

    def list(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        data = self._load()
        bucket = data.get(scope, {})
        return [
            {"key": name, "meta": (entry.get("meta") if isinstance(entry, dict) else {})}
            for name, entry in sorted(bucket.items())
        ]

    def import_items(self, items: Iterable[Dict[str, Any]], *, scope: SecretScope = "profile") -> int:
        data = self._load()
        bucket = data.setdefault(scope, {})
        count = 0
        for item in items:
            key = item.get("key")
            value = item.get("value")
            if not key or value is None:
                continue
            bucket[key] = {"value": str(value), "meta": item.get("meta") or {}}
            count += 1
        self._save(data)
        return count

    def export_items(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        data = self._load()
        bucket = data.get(scope, {})
        return [
            {"key": name, "value": entry.get("value"), "meta": entry.get("meta") or {}}
            for name, entry in bucket.items()
            if isinstance(entry, dict)
        ]

