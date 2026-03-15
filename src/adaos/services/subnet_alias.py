from __future__ import annotations

import re


_GENERIC_HUB_ALIAS_RE = re.compile(r"^hub(?:-\d+)?$", re.IGNORECASE)


def display_subnet_alias(alias: str | None, subnet_id: str | None) -> str | None:
    raw_alias = str(alias or "").strip()
    raw_subnet = str(subnet_id or "").strip()
    if raw_alias and not _GENERIC_HUB_ALIAS_RE.fullmatch(raw_alias):
        return raw_alias
    if raw_subnet:
        return raw_subnet
    return raw_alias or None

