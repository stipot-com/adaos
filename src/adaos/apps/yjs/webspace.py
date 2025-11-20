from __future__ import annotations

import os

_DEFAULT_WEBSPACE_ID = os.getenv('ADAOS_WEBSPACE_ID') or 'desktop'


def default_webspace_id() -> str:
    """Return the configured webspace identifier used for Yjs state."""
    return _DEFAULT_WEBSPACE_ID
