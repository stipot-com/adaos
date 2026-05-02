from __future__ import annotations

# Files in this list affect bootstrap/supervisor behavior and therefore require
# root promotion only after the prepared slot has already been validated.
BOOTSTRAP_CRITICAL_PATHS: tuple[str, ...] = (
    "src/adaos/apps/supervisor.py",
    "src/adaos/apps/autostart_runner.py",
    "src/adaos/apps/core_update_apply.py",
    "src/adaos/services/core_update.py",
    "src/adaos/services/autostart.py",
    "src/adaos/services/bootstrap_update.py",
    "src/adaos/services/node_display.py",
    "src/adaos/services/node_runtime_state.py",
    "src/adaos/services/runtime_refresh.py",
    "src/adaos/services/scenario/webspace_runtime.py",
    "src/adaos/services/subnet/link_client.py",
    "src/adaos/services/subnet/link_manager.py",
    "src/adaos/apps/cli/commands/setup.py",
    "src/adaos/apps/cli/commands/skill.py",
)
