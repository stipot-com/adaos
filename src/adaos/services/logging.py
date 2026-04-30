from __future__ import annotations
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from adaos.domain import Event
from adaos.ports.paths import PathProvider
from adaos.ports import EventBus


def _json_formatter(record: logging.LogRecord) -> str:
    # `record.asctime` is only populated when a base Formatter runs `formatTime()`.
    # Since we generate JSON directly, compute timestamps ourselves.
    try:
        ts = float(getattr(record, "created", 0.0) or 0.0)
    except Exception:
        ts = 0.0
    iso = None
    try:
        if ts:
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        iso = None
    base = {
        "level": record.levelname,
        "logger": record.name,
        "msg": record.getMessage(),
        "time": iso,
        "ts": ts or None,
    }
    if hasattr(record, "extra"):
        try:
            base.update(record.extra)  # type: ignore[attr-defined]
        except Exception:
            pass
    return json.dumps(base, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _json_formatter(record)


def _parse_log_level(name: str | None, *, default: int) -> int:
    if not name:
        return default
    try:
        raw = str(name).strip().upper()
    except Exception:
        return default
    if not raw:
        return default
    if raw == "WARN":
        raw = "WARNING"
    if raw.isdigit():
        try:
            return int(raw)
        except Exception:
            return default
    try:
        v = getattr(logging, raw)
    except Exception:
        return default
    if isinstance(v, int):
        return v
    return default


def _parse_hide_rules() -> list[tuple[str, int]]:
    """
    Hide chatty loggers without changing global log level.

    Env:
    - ADAOS_LOG_HIDE: comma-separated rules:
        * `prefix` -> hide below ADAOS_LOG_HIDE_LEVEL
        * `prefix=LEVEL` / `prefix:LEVEL` -> hide below LEVEL for that prefix
    - ADAOS_LOG_HIDE_LEVEL: default level for rules without explicit LEVEL (default: WARNING)
    """
    raw = os.getenv("ADAOS_LOG_HIDE", "") or ""
    try:
        s = str(raw).strip()
    except Exception:
        s = ""
    if not s:
        return []
    default_level = _parse_log_level(os.getenv("ADAOS_LOG_HIDE_LEVEL", "WARNING"), default=logging.WARNING)
    rules: list[tuple[str, int]] = []
    for token in s.split(","):
        try:
            item = str(token).strip()
        except Exception:
            continue
        if not item:
            continue
        sep = "=" if "=" in item else (":" if ":" in item else None)
        if sep:
            prefix, lvl = item.split(sep, 1)
            prefix = prefix.strip()
            min_level = _parse_log_level(lvl, default=default_level)
        else:
            prefix = item.strip()
            min_level = default_level
        if not prefix:
            continue
        rules.append((prefix, int(min_level)))
    return rules


class PrefixMinLevelFilter(logging.Filter):
    def __init__(self, rules: list[tuple[str, int]]):
        super().__init__()
        self._rules = [(p, int(lvl)) for (p, lvl) in rules if p]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            name = record.name
            level = record.levelno
        except Exception:
            return True
        for prefix, min_level in self._rules:
            if name.startswith(prefix):
                return level >= min_level
        return True


def setup_logging(paths: PathProvider, level: str = "INFO") -> logging.Logger:
    """
    Настройка логов:
      - консоль (stderr)
      - файл {logs_dir}/adaos.log (ротация)
    JSON формат, чтобы легко парсить.
    """
    logs_dir = Path(paths.logs_dir())
    logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = logs_dir / "adaos.log"

    logger = logging.getLogger("adaos")
    resolved_level = (os.getenv("ADAOS_LOG_LEVEL") or level or "INFO").upper()
    logger.setLevel(getattr(logging, resolved_level, logging.INFO))
    logger.handlers.clear()

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(JsonFormatter())
    stream_h.setLevel(logger.level)

    file_h = RotatingFileHandler(logfile, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(JsonFormatter())
    file_h.setLevel(logger.level)

    logger.addHandler(stream_h)
    logger.addHandler(file_h)
    logger.propagate = False

    # Optional noise suppression (apply to handlers so it affects all child loggers).
    try:
        rules = _parse_hide_rules()
        if rules:
            flt = PrefixMinLevelFilter(rules)
            stream_h.addFilter(flt)
            file_h.addFilter(flt)
    except Exception:
        pass
    # logger.info("logging.initialized", extra={"extra": {"logfile": str(logfile)}})
    return logger


def attach_event_logger(bus: EventBus, logger: Optional[logging.Logger] = None) -> None:
    """
    Подписывает логгер на все события шины.
    """
    try:
        if str(os.getenv("ADAOS_LOG_EVENTS", "1") or "1").strip() == "0":
            return
    except Exception:
        pass
    base_logger = logger or logging.getLogger("adaos.events")
    try:
        include_payload = str(os.getenv("ADAOS_LOG_EVENTS_PAYLOAD", "0") or "0").strip() != "0"
    except Exception:
        include_payload = False

    def _handler(ev: Event) -> None:
        iso_time = datetime.fromtimestamp(getattr(ev, "ts", 0), tz=timezone.utc).isoformat() if getattr(ev, "ts", None) else None
        payload = ev.payload if include_payload else None
        base_logger.info(
            "event",
            extra={
                "extra": {
                    "time": iso_time,
                    "type": ev.type,
                    "source": ev.source,
                    "ts": ev.ts,
                    "payload": payload,
                }
            },
        )

    bus.subscribe("", _handler)
