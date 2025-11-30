from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml  # type: ignore

from .models import CodeEntry


ROOT = Path(__file__).resolve().parents[1]
CODE_MAP_PATH = ROOT / "artifacts" / "code_map.yaml"
SRC_ROOT = ROOT  


class CodeMapError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_code_map() -> List[Dict[str, Any]]:
    """
    Загружает карту кода из YAML один раз за процесс.
    """
    if not CODE_MAP_PATH.exists():
        raise CodeMapError(
            f"code_map.yaml не найден по пути {CODE_MAP_PATH}. "
            f"Сначала необходимо сгенерировать карту (repo_skim.py)."
        )

    try:
        with CODE_MAP_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
    except Exception as e:
        raise CodeMapError(f"Ошибка чтения {CODE_MAP_PATH}: {e}") from e

    if not isinstance(data, list):
        raise CodeMapError("Ожидался список записей в code_map.yaml")

    return data


def entry_matches_query(entry: Dict[str, Any], query: str) -> bool:
    """
    Простое полнотекстовое сопоставление.
    """
    q = query.lower()
    for key in ("path", "funcs", "endpoints", "exports", "types", "interfaces"):
        v = entry.get(key)
        if isinstance(v, str) and q in v.lower():
            return True

    classes = entry.get("classes") or []
    for cls in classes:
        name = cls.get("Name")
        methds = cls.get("methds")
        if isinstance(name, str) and q in name.lower():
            return True
        if isinstance(methds, str) and q in methds.lower():
            return True

    tags = entry.get("tags") or []
    for t in tags:
        if isinstance(t, str) and q in t.lower():
            return True

    return False


def to_code_entry(entry: Dict[str, Any]) -> CodeEntry:
    return CodeEntry(
        path=entry.get("path"),
        tags=entry.get("tags"),
        funcs=entry.get("funcs"),
        endpoints=entry.get("endpoints"),
        exports=entry.get("exports"),
        types=entry.get("types"),
        interfaces=entry.get("interfaces"),
        classes=entry.get("classes"),
    )


def list_entries(tag: Optional[str] = None, limit: int = 50) -> List[CodeEntry]:
    entries = load_code_map()
    if tag:
        t = tag.lower()
        entries = [
            e
            for e in entries
            if any(
                isinstance(x, str) and t in x.lower()
                for x in e.get("tags", [])
            )
        ]
    return [to_code_entry(e) for e in entries[:limit]]


def search_entries(query: str, limit: int = 20) -> List[CodeEntry]:
    entries = load_code_map()
    matched = [e for e in entries if entry_matches_query(e, query)]
    return [to_code_entry(e) for e in matched[:limit]]


def get_entry(path: str) -> Optional[CodeEntry]:
    entries = load_code_map()
    for e in entries:
        if e.get("path") == path:
            return to_code_entry(e)
    return None


def read_source_text(path: str, max_bytes: int = 200_000) -> Tuple[str, Optional[str]]:
    """
    Пытается прочитать исходник по относительному пути из карты.
    Возвращает (path, content or None).
    """
    abs_path = (SRC_ROOT / path).resolve()
    try:
        # защита от выхода за пределы ROOT
        if not str(abs_path).startswith(str(SRC_ROOT.resolve())):
            return path, None

        with abs_path.open("r", encoding="utf-8") as f:
            content = f.read(max_bytes + 1)
        if len(content) > max_bytes:
            content = content[:max_bytes]
        return path, content
    except Exception:
        return path, None
