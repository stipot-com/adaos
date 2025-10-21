"""Self-test runner for skills runtime pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence

from adaos.services.testing.bootstrap import skill_tests_root


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


def run_tests(
    root: Path,
    *,
    log_path: Path,
    interpreter: Path | None = None,
    python_paths: Sequence[str] | None = None,
    skill_env_path: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    skill_name: str | None = None,
    skill_version: str | None = None,
    slot_current_dir: Path | None = None,
    dev_mode: bool = False,
) -> Dict[str, TestResult]:
    results: Dict[str, TestResult] = {}
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env_template = os.environ.copy()
    if extra_env:
        env_template.update({k: str(v) for k, v in extra_env.items()})
    if skill_env_path:
        env_template["ADAOS_SKILL_ENV_PATH"] = str(skill_env_path)

    # стабильные переменные для тестов
    if skill_name:
        env_template["ADAOS_SKILL_NAME"] = skill_name
        env_template["ADAOS_SKILL_PACKAGE"] = f"skills.{skill_name}"
    env_template["ADAOS_SKILL_ROOT"] = str(root)
    env_template["ADAOS_SKILL_MODE"] = "dev" if dev_mode else "runtime"
    # DEV hints для автобутстрапа рантайма:
    if dev_mode:
        # Путь к .adaos/dev/<subnet> (родитель 'skills')
        env_template["ADAOS_DEV_DIR"] = os.getenv("ADAOS_DEV_DIR", "")  # см. ниже — проставим в менеджере
        # Полный путь к корню навыка
        env_template["ADAOS_DEV_SKILL_DIR"] = str(root)

    python_entries: list[str] = []
    if root.exists():
        python_entries.append(str(root))

    # PYTHONPATH по режимам
    if dev_mode:
        python_entries.append(str(root))
    else:
        # runtime: src + vendor
        src_root = root  # root у нас = src/skills/<name>, см. существующую логику вызова
        vendor_root = src_root.parent / "vendor"
        if vendor_root.exists():
            python_entries.append(str(vendor_root))
        python_entries.append(str(src_root))

    for p in python_paths or []:
        if p:
            python_entries.append(p)

    existing = env_template.get("PYTHONPATH")
    if existing:
        python_entries.extend(existing.split(os.pathsep))
    if python_entries:
        env_template["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_entries))

    dev_tests_root = root / "tests"
    if not dev_mode and slot_current_dir is not None and skill_name:
        runtime_tests_root = skill_tests_root(slot_current_dir, skill_name)
    else:
        runtime_tests_root = dev_tests_root

    with log_path.open("w", encoding="utf-8") as log:
        if not dev_mode:
            # старые suite-скрипты
            suite_results: Dict[str, TestResult] = {}
            for suite in ("smoke", "contract", "e2e-dryrun"):
                suite_dir = runtime_tests_root / suite
                if not suite_dir.exists():
                    continue
                suite_results[suite] = _run_suite(
                    suite,
                    suite_dir,
                    timeout=_TEST_TIMEOUTS.get(suite, 30),
                    log=log,
                    interpreter=interpreter,
                    env=env_template,
                    skill_name=skill_name,
                    skill_version=skill_version,
                    slot_dir=slot_current_dir,
                )
            results.update(suite_results)

            need_fallback = (
                not results or all(res.status == "skipped" for res in results.values())
            )
            if need_fallback and runtime_tests_root.exists():
                fallback = _run_pytest_suite(
                    suite_name="pytest",
                    tests_dir=runtime_tests_root,
                    marker=None,
                    timeout=max(_TEST_TIMEOUTS.values()),
                    log=log,
                    interpreter=interpreter,
                    env=env_template,
                )
                if fallback is not None:
                    results[fallback.name] = fallback
        else:
            # pytest в DEV: сначала директории, затем маркеры
            dev_groups: list[tuple[str, Path | None, str | None]] = []
            for suite in ("smoke", "contract", "e2e"):
                suite_dir = dev_tests_root / suite
                if suite_dir.exists():
                    dev_groups.append((suite, suite_dir, None))
                else:
                    # маркерная группа (папки нет)
                    dev_groups.append((suite, None, suite))
            # если нет ни папок, ни маркеров — один прогон всех tests/
            if not any((dev_tests_root / n).exists() for n in ("smoke", "contract", "e2e-dryrun", "e2e")):
                dev_groups = [("pytest", dev_tests_root, None)]

            for suite_name, suite_dir, marker in dev_groups:
                outcome = _run_pytest_suite(
                    suite_name=suite_name,
                    tests_dir=suite_dir or dev_tests_root,
                    marker=marker,
                    timeout=_TEST_TIMEOUTS.get(suite_name, 60),
                    log=log,
                    interpreter=interpreter,
                    env=env_template,
                )
                if outcome is not None:
                    results[suite_name] = outcome

    return results


def _run_pytest_suite(
    suite_name: str,
    tests_dir: Path,
    *,
    marker: str | None,  # <-- НОВОЕ
    timeout: int,
    log,
    interpreter: Path | None,
    env: Mapping[str, str],
) -> TestResult | None:
    # Запуск из каталога тестов, цель "."; расширяем правило поиска файлов
    local_cfg = tests_dir / "pytest.dev.ini"
    try:
        local_cfg.write_text(
            "[pytest]\n" "testpaths = .\n" "python_files = test_*.py *_test.py *.spec.py\n" "addopts = -q -vv -s --maxfail=1\n" "log_cli = false\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    # Запускаем из каталога тестов, цель "." и свой конфиг
    cmd = [str(interpreter or sys.executable), "-m", "pytest", "-c", str(local_cfg), "."]
    if marker:
        cmd.extend(["-m", marker])
    log.write(
        "$ " + " ".join(cmd) + "\n"
        f"CWD={tests_dir}\n"
        f"ENV.ADAOS_SKILL_PACKAGE={env.get('ADAOS_SKILL_PACKAGE')}\n"
        f"ENV.ADAOS_SKILL_NAME={env.get('ADAOS_SKILL_NAME')}\n"
        f"ENV.ADAOS_DEV_DIR={env.get('ADAOS_DEV_DIR')}\n"
        f"ENV.ADAOS_DEV_SKILL_DIR={env.get('ADAOS_DEV_SKILL_DIR')}\n"
        f"ENV.PYTHONPATH={env.get('PYTHONPATH','')}\n\n"
    )
    proc = subprocess.run(
        cmd,
        cwd=str(tests_dir),
        stdout=log,
        stderr=log,
        env=dict(env),
        timeout=timeout,
        check=False,
        text=False,
    )
    if proc.returncode == 0:
        return TestResult(name=suite_name, status="passed", detail=None)
    if proc.returncode == 5:
        # нет подходящих тестов — пропускаем эту группу
        return None
    detail = f"pytest exit code {proc.returncode}"
    status = "failed" if proc.returncode == 1 else "error"
    return TestResult(name=suite_name, status=status, detail=detail)


def _run_suite(
    name: str,
    suite_dir: Path,
    *,
    timeout: int,
    log,
    interpreter: Path | None,
    env: Mapping[str, str],
    skill_name: str | None,
    skill_version: str | None,
    slot_dir: Path | None,
) -> TestResult:
    commands = list(
        _discover_commands(
            suite_dir,
            interpreter=interpreter,
            skill_name=skill_name,
            skill_version=skill_version,
            slot_dir=slot_dir,
        )
    )
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
            env=env,
        )
        print(proc)
        log.write(f"$ {' '.join(command)}\n")
        log.write(proc.stdout or "")
        log.write("\n")
        if proc.returncode != 0:
            return TestResult(name=name, status="failed", detail=f"command {' '.join(command)} exited {proc.returncode}")
    return TestResult(name=name, status="passed")


def _discover_commands(
    suite_dir: Path,
    *,
    interpreter: Path | None,
    skill_name: str | None,
    skill_version: str | None,
    slot_dir: Path | None,
) -> Iterable[list[str]]:
    for path in sorted(suite_dir.iterdir()):
        if path.is_dir():
            continue
        if path.suffix == ".py":
            runner = str(interpreter or sys.executable)
            if skill_name and slot_dir is not None:
                command = [
                    runner,
                    "-m",
                    "adaos.services.skill.tests_entry",
                    "--skill-name",
                    skill_name,
                    "--skill-version",
                    skill_version or "",
                    "--slot-dir",
                    str(slot_dir),
                    "--",
                    str(path),
                ]
                yield command
            else:
                yield [runner, str(path)]
        elif path.suffix in {".sh", ""} and path.stat().st_mode & 0o111:
            yield [str(path)]
