# src\adaos\sdk\i18n.py
# Adopted
from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional

from adaos.services.agent_context import get_ctx
from adaos.services.i18n.service import DEFAULT_LANG, I18nService

from .context import get_current_skill

_PREBOOT_CACHE: Dict[str, Dict[str, str]] = {}


def _preboot_messages(lang: str) -> Dict[str, str]:
    cache = _PREBOOT_CACHE.get(lang)
    if cache is not None:
        return cache

    locales_root = resources.files("adaos").joinpath("locales")
    messages: Dict[str, str] = {}
    for candidate in (lang, DEFAULT_LANG):
        candidate_path = locales_root.joinpath(f"{candidate}.json")
        if candidate_path.is_file():
            messages = json.loads(candidate_path.read_text(encoding="utf-8"))
            break
    _PREBOOT_CACHE[lang] = messages
    return messages


class I18n:
    def __init__(self, lang: Optional[str] = None):
        self.lang = lang or os.getenv("ADAOS_LANG") or DEFAULT_LANG

    def data(self, key: str) -> Any:
        """
        Возвращает СЫРОЕ значение из локали (dict/list/str), без форматирования.
        Работает как в preboot-режиме, так и с полноценным I18nService.
        """
        # preboot (нет контекста)
        try:
            ctx = get_ctx()
        except RuntimeError:
            messages = _preboot_messages(self.lang)
            return _dig(messages, key)

        # полноценный сервис
        svc = I18nService(ctx)
        cur = get_current_skill()
        skill_path: Optional[Path] = getattr(cur, "path", None) if cur else None
        skill_id: Optional[str] = getattr(cur, "name", None) if cur else None
        # просим «сырое» значение у сервиса (см. патч к сервису ниже)
        return svc.translate_data(
            key,
            lang=self.lang,
            skill_path=skill_path,
            skill_id=skill_id,
        )


    def translate(self, key: str, **kwargs: Any) -> str:
        try:
            ctx = get_ctx()
        except RuntimeError:
            messages = _preboot_messages(self.lang)
            text = _dig(messages, key)
            try:
                return text.format(**kwargs)
            except Exception:
                return text

        svc = I18nService(ctx)
        cur = get_current_skill()
        skill_path: Optional[Path] = getattr(cur, "path", None) if cur else None
        skill_id: Optional[str] = getattr(cur, "name", None) if cur else None
        scope = "skill" if key.startswith("prep.") else "global"
        return svc.translate(
            key,
            lang=self.lang,
            params=kwargs,
            skill_path=skill_path,
            skill_id=skill_id,
            scope=scope,
        )


_: Any = I18n().translate

def _dig(tree: Any, dotted: str) -> Any:
    """Безопасно проходит по 'a.b.c' внутри dict’а, иначе возвращает ключ как строку."""
    if not isinstance(tree, dict):
        return dotted
    cur: Any = tree
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return dotted
        cur = cur[part]
    return cur

__all__ = ["I18n", "_"]
