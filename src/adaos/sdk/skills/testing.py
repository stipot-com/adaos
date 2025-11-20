from __future__ import annotations
import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

# Рантайм проставит эти env
_ENV_PKG = "ADAOS_SKILL_PACKAGE"  # e.g. skills.weather_skill
_ENV_NAME = "ADAOS_SKILL_NAME"  # e.g. weather_skill
_ENV_ROOT = "ADAOS_SKILL_ROOT"  # абсолютный путь к корню навыка
_ENV_MODE = "ADAOS_SKILL_MODE"  # "dev" | "runtime"


def _lazy_dev_bootstrap() -> None:
    """Инициализирует DEV-рантайм, если есть подсказки в ENV и контекст ещё не поднят."""
    dev_dir = os.getenv("ADAOS_DEV_DIR")
    skill_dir = os.getenv("ADAOS_DEV_SKILL_DIR")
    skill_name = os.getenv("ADAOS_SKILL_NAME")
    # Allow deriving dev_dir from skill_dir if not provided
    if not (skill_dir and skill_name):
        return
    if not dev_dir and skill_dir:
        try:
            dev_dir = str(Path(skill_dir).resolve().parent.parent)
        except Exception:
            dev_dir = None
        if dev_dir:
            os.environ["ADAOS_DEV_DIR"] = dev_dir
    # пробуем официальный bootstrap, если есть
    try:
        mod = importlib.import_module("adaos.services.testing.bootstrap")
        init_from_env = getattr(mod, "init_from_env", None)
        if callable(init_from_env):
            # Ensure env carries derived dev_dir for the bootstrap helper
            if not os.getenv("ADAOS_DEV_DIR") and skill_dir:
                try:
                    os.environ["ADAOS_DEV_DIR"] = str(Path(skill_dir).resolve().parent.parent)
                except Exception:
                    pass
            init_from_env()
            return
    except Exception:
        pass
    # fallback: прямой бутстрап (без внешних зависимостей на импорт модуля bootstrap)
    try:
        from pathlib import Path
        from adaos.services.agent_context import ensure_runtime_context, set_ctx
        from adaos.services.skill.secrets_backend import SkillSecretsBackend
        from adaos.services.crypto.secrets_service import SecretsService
        from adaos.services.agent_context import get_ctx as _get_ctx_check

        # создаём/берём контекст поверх dev_dir (.adaos/dev/<subnet>)
        ctx = ensure_runtime_context(Path(dev_dir))
        set_ctx(ctx)
        # активируем skill ctx
        if not ctx.skill_ctx.set(skill_name, Path(skill_dir)):
            raise RuntimeError(f"failed to set skill ctx for {skill_name}")
        # подцепим secrets backend (на dev data store)
        store = Path(dev_dir) / "files" / "secrets.json"
        ctx.secrets = SecretsService(SkillSecretsBackend(store), ctx.caps)
        # sanity check
        _ = _get_ctx_check()
    except Exception:
        # Логики в тестах может быть достаточно и без контекста — не мешаем падением здесь,
        # пусть ошибка всплывёт в месте вызова (как было).
        return


def _ensure_ctx_if_needed():
    # если рантайм уже поднят — сразу выходим
    try:
        from adaos.services.agent_context import get_ctx

        _ = get_ctx()
        return
    except Exception:
        pass
    # иначе пробуем DEV bootstrapping
    _lazy_dev_bootstrap()


def _skill_pkg() -> str:
    pkg = os.getenv(_ENV_PKG)
    if pkg:
        return pkg
    name = os.getenv(_ENV_NAME)
    if not name:
        raise RuntimeError("skill package not configured (missing ADAOS_SKILL_PACKAGE/NAME)")
    return f"skills.{name}"


@dataclass(frozen=True)
class SkillUnderTest:
    name: str
    package: str
    root: str
    mode: str

    def import_module(self, subpath: str = "") -> ModuleType:
        mod = self.package if not subpath else f"{self.package}.{subpath.strip('.')}"
        return importlib.import_module(mod)

    def import_(self, subpath: str, attr: str):
        mod = self.import_module(subpath)
        try:
            return getattr(mod, attr)
        except AttributeError as exc:
            raise AttributeError(f"{mod.__name__} has no attribute '{attr}'") from exc


def skill() -> SkillUnderTest:
    _ensure_ctx_if_needed()
    pkg = _skill_pkg()
    name = pkg.split(".", 1)[1] if pkg.startswith("skills.") else pkg
    return SkillUnderTest(
        name=name,
        package=pkg,
        root=os.getenv(_ENV_ROOT, ""),
        mode=os.getenv(_ENV_MODE, ""),
    )
