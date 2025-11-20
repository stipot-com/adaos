"""Scenario logging helpers exposed via the SDK layer."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple

from ._ctx import require_ctx

__all__ = ["setup_scenario_logger", "JsonFormatter"]


def _json_payload(record: logging.LogRecord) -> str:
    base = {
        "level": record.levelname,
        "logger": record.name,
        "msg": record.getMessage(),
    }
    timestamp = getattr(record, "asctime", None)
    if timestamp:
        base["time"] = timestamp
    extra = getattr(record, "extra", None)
    if isinstance(extra, dict):
        base.update(extra)
    return json.dumps(base, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    """Simple JSON formatter mirroring :mod:`adaos.services.logging`."""

    def format(self, record: logging.LogRecord) -> str:  # pragma: no cover - trivial wrapper
        return _json_payload(record)


def setup_scenario_logger(
    scenario_id: str,
    *,
    level: Optional[str] = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> Tuple[logging.Logger, Path]:
    """Create a dedicated logger for a scenario execution."""

    ctx = require_ctx("sdk.logging.scenario")
    paths = ctx.paths
    logs_dir = Path(paths.logs_dir()) / "scenarios"
    logs_dir.mkdir(parents=True, exist_ok=True)

    logfile = logs_dir / f"{scenario_id}.log"
    name = f"adaos.scenario.{scenario_id}"
    logger = logging.getLogger(name)

    resolved_level = (level or ctx.settings.scenario_log_level or "INFO").upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)
    logger.setLevel(numeric_level)
    logger.handlers.clear()
    logger.propagate = False

    handler = RotatingFileHandler(logfile, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setLevel(numeric_level)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    return logger, logfile

