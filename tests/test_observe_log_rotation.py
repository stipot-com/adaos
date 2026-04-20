from __future__ import annotations

import gzip
import json
from pathlib import Path

from adaos.services import observe


def test_write_local_rotates_events_log(monkeypatch, tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "events.log"
    log_path.write_text("x" * 20, encoding="utf-8")

    monkeypatch.setattr(observe, "_LOG_FILE", log_path)
    monkeypatch.setattr(observe, "_MAX_BYTES", 10)
    monkeypatch.setattr(observe, "_KEEP", 3)

    observe._write_local({"topic": "test.topic", "payload": {"ok": True}})

    rotated = log_path.with_suffix(".log.1.gz")
    assert rotated.exists()
    with gzip.open(rotated, "rt", encoding="utf-8") as fh:
        assert fh.read() == "x" * 20

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"topic": "test.topic", "payload": {"ok": True}}
