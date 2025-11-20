# src/adaos/adapters/fs/path_provider.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from adaos.services.settings import Settings


@dataclass(slots=True)
class PathProvider:
    """Единая точка истинны для путей. Всегда работает с pathlib.Path."""

    base: Path
    package_dir: Path
    subnet_id: str

    # --- конструкторы ---
    @classmethod
    def from_settings(cls, settings: Settings) -> "PathProvider":
        return cls(base=Path(settings.base_dir).expanduser().resolve())

    # совместимость со старым стилем: PathProvider(settings)
    def __init__(self, settings: Settings | str | Path):
        base = settings.base_dir
        package_dir: Path = settings.package_dir
        self.subnet_id = settings.subnet_id
        object.__setattr__(self, "base", base.expanduser().resolve())
        object.__setattr__(self, "package_dir", package_dir.expanduser().resolve())

    # --- базовые каталоги ---
    def package_path(self) -> Path:
        """Package-level locales shipped with AdaOS (see ``get_ctx().paths``)."""
        return (self.package_dir).resolve()

    def locales_dir(self) -> Path:
        """Package-level locales shipped with AdaOS (see ``get_ctx().paths``)."""
        return (self.package_dir / "locales").resolve()

    def skill_templates_dir(self) -> Path:
        return (self.package_dir / "skills_templates").resolve()

    def scenario_templates_dir(self) -> Path:
        return (self.package_dir / "scenario_templates").resolve()

    def base_dir(self) -> Path:
        return self.base

    # --- workspace helpers ---

    def workspace_dir(self) -> Path:
        return (self.base / "workspace").resolve()

    def skills_workspace_dir(self) -> Path:
        return (self.workspace_dir() / "skills").resolve()

    def scenarios_workspace_dir(self) -> Path:
        return (self.workspace_dir() / "scenarios").resolve()

    # --- registry caches ---

    def skills_cache_dir(self) -> Path:
        return (self.base / "skills").resolve()

    def scenarios_cache_dir(self) -> Path:
        return (self.base / "scenarios").resolve()

    def skills_dir(self) -> Path:
        return self.skills_workspace_dir()

    def scenarios_dir(self) -> Path:
        return self.scenarios_workspace_dir()

    def models_dir(self) -> Path:
        return (self.base / "models").resolve()

    def logs_dir(self) -> Path:
        return (self.base / "logs").resolve()

    def cache_dir(self) -> Path:
        return (self.base / "cache").resolve()

    def state_dir(self) -> Path:
        return (self.base / "state").resolve()

    def locales_base_dir(self) -> Path:
        """Base directory for runtime locales exposed via ``get_ctx().paths``."""

        return (self.base / "i18n").resolve()

    def skills_locales_dir(self) -> Path:
        """Skill locales managed through the global context."""

        return self.locales_base_dir()

    def scenarios_locales_dir(self) -> Path:
        """Scenario locales managed through the global context."""

        return self.locales_base_dir()

    # dev section

    def dev_dir(self) -> Path:
        return (self.base / "dev" / self.subnet_id).resolve()

    def dev_skills_dir(self) -> Path:
        return (self.dev_dir() / "skills").resolve()

    def dev_scenarios_dir(self) -> Path:
        return (self.dev_dir() / "scenarios").resolve()

    def tmp_dir(self) -> Path:
        return (self.base / "tmp").resolve()

    def ensure_tree(self) -> None:
        for p in (
            self.locales_dir(),
            self.locales_base_dir(),
            self.base_dir(),
            self.workspace_dir(),
            self.skill_templates_dir(),
            self.scenario_templates_dir(),
            self.models_dir(),
            self.logs_dir(),
            self.cache_dir(),
            self.state_dir(),
            self.tmp_dir(),
        ):
            p.mkdir(parents=True, exist_ok=True)
