from __future__ import annotations

# Files in this list affect bootstrap/supervisor behavior and therefore require
# root promotion only after the prepared slot has already been validated.
BOOTSTRAP_CRITICAL_PATHS: tuple[str, ...] = (
    "src/adaos/apps/supervisor.py",
    "src/adaos/apps/autostart_runner.py",
    "src/adaos/apps/core_update_apply.py",
    "src/adaos/services/core_update.py",
    "src/adaos/services/autostart.py",
    "src/adaos/apps/cli/commands/setup.py",
)
