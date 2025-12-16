from __future__ import annotations

import os

_DEFAULT_WEBSPACE_ID = os.getenv("ADAOS_WEBSPACE_ID") or "desktop"
# Dedicated development webspace. This id is reserved and cannot be deleted.
_DEV_WEBSPACE_ID = os.getenv("ADAOS_DEV_WEBSPACE_ID") or "dev"


def default_webspace_id() -> str:
    """Return the configured default webspace identifier used for Yjs state."""
    return _DEFAULT_WEBSPACE_ID


def dev_webspace_id() -> str:
    """Return the reserved development webspace identifier."""
    return _DEV_WEBSPACE_ID

