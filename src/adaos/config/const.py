# src/adaos/config/const.py
from __future__ import annotations

# ЖЁСТКИЕ значения по умолчанию (меняются разработчиками в коде/сборке)
REGISTRY_URL: str = "https://github.com/stipot-com/adaos-registry.git"

SKILLS_MONOREPO_URL: str | None = REGISTRY_URL
SKILLS_MONOREPO_BRANCH: str | None = "main"

SCENARIOS_MONOREPO_URL: str | None = REGISTRY_URL
SCENARIOS_MONOREPO_BRANCH: str | None = "main"

# Разрешить ли .env/ENV менять монорепо (ТОЛЬКО для dev-сборок!)
ALLOW_ENV_MONOREPO_OVERRIDE: bool = False
