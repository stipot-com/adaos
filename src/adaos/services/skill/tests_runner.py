"""Self-test runner for skills runtime pipeline."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable


@dataclass(slots=True, frozen=True)
class TestResult:
    name: str
    status: str
    detail: str | None = None


_TEST_TIMEOUTS = {
    "smoke": 10,
    "contract": 30,
    "e2e-dryrun": 60,
}


def run_tests(root: Path, *, log_path: Path) -> Dict[str, TestResult]:
    results: Dict[str, TestResult] = {}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        for suite in ("smoke", "contract", "e2e-dryrun"):
            suite_dir = root / "runtime" / "tests" / suite
            if not suite_dir.exists():
                continue
            outcome = _run_suite(suite, suite_dir, timeout=_TEST_TIMEOUTS.get(suite, 30), log=log)
            results[suite] = outcome
    return results


def _run_suite(name: str, suite_dir: Path, *, timeout: int, log) -> TestResult:
    commands = list(_discover_commands(suite_dir))
    if not commands:
        return TestResult(name=name, status="skipped", detail="no tests found")

    for command in commands:
        proc = subprocess.run(
            command,
            cwd=suite_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        log.write(f"$ {' '.join(command)}\n")
        log.write(proc.stdout or "")
        log.write("\n")
        if proc.returncode != 0:
            return TestResult(name=name, status="failed", detail=f"command {' '.join(command)} exited {proc.returncode}")
    return TestResult(name=name, status="passed")


def _discover_commands(suite_dir: Path) -> Iterable[list[str]]:
    for path in sorted(suite_dir.iterdir()):
        if path.is_dir():
            continue
        if path.suffix == ".py":
            yield [sys.executable, str(path)]
        elif path.suffix in {".sh", ""} and path.stat().st_mode & 0o111:
            yield [str(path)]

