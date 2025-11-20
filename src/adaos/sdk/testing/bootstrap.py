from __future__ import annotations
from pathlib import Path
from typing import Optional
import os

from adaos.services.agent_context import ensure_runtime_context, get_ctx, set_ctx
from adaos.services.skill.secrets_backend import SkillSecretsBackend
from adaos.services.crypto.secrets_service import SecretsService


def init_dev_runtime(*, dev_dir: Path, skill_dir: Path, skill_name: str) -> None:
    """
    Minimal DEV runtime bootstrap for tests:
    - ensures AgentContext bound to <dev_dir> (.adaos/dev/<subnet>)
    - sets current skill context (name, path)
    - wires SecretsService to <dev_dir>/files/secrets.json
    """
    base_dir = Path(dev_dir).resolve()
    skill_dir = Path(skill_dir).resolve()

    ctx = ensure_runtime_context(base_dir)  # создает Settings/Paths/Repos и т.д.
    # привяжем обратно в глобал, если кто-то дернет get_ctx()
    set_ctx(ctx)

    # выставим skill ctx (имя + путь)
    if not ctx.skill_ctx.set(skill_name, skill_dir):
        # если по какой-то причине не удалось — явно бросим
        raise RuntimeError(f"failed to establish skill ctx for '{skill_name}' at {skill_dir}")

    # локальный secrets backend под DEV
    secrets_store = base_dir / "files" / "secrets.json"
    ctx.secrets = SecretsService(SkillSecretsBackend(secrets_store), ctx.caps)


def init_from_env() -> None:
    dev = os.getenv("ADAOS_DEV_DIR")
    skill = os.getenv("ADAOS_DEV_SKILL_DIR")
    name = os.getenv("ADAOS_SKILL_NAME") or ""
    if not (dev and skill and name):
        return  # нет подсказок — ничего не делаем
    init_dev_runtime(dev_dir=Path(dev), skill_dir=Path(skill), skill_name=name)
