# src/adaos/agent/ovos_adapter/__init__.py
import warnings
from adaos.integrations.ovos import *  # type: ignore # noqa: F401,F403

if __name__ == "adaos.agent.integrations.ovos_adapter":  # pragma: no cover - compatibility shim
    warnings.warn(
        "adaos.agent.integrations.ovos_adapter is deprecated; use adaos.adapters.ovos",
        DeprecationWarning,
        stacklevel=2,
    )
