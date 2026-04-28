from __future__ import annotations

import re
import shutil
import subprocess
import sys


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _ver(cmd: str, pat: str = r"(\d+\.\d+\.\d+)") -> str | None:
    try:
        out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        return None
    m = re.search(pat, out)
    return m.group(1) if m else (out or None)


def main() -> int:
    py_cur = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    min_py = (3, 11, 9)
    py_path = _ver("python --version")  # best-effort: python in PATH
    node = _ver("node --version")
    npmv = _ver("npm --version")
    pnpmv = _ver("pnpm --version")
    ionicv = _ver("ionic --version")

    print(f"Python (current): {py_cur} ({sys.executable})  [need >=3.11.9,<3.12]")
    print(f"Python (PATH):    {py_path or 'not found'}")
    print(f"Node:             {node or 'not found'}  [LTS 20.x recommended]")
    print(f"npm:              {npmv or 'not found'}")
    print(f"pnpm:             {pnpmv or '—'} (optional)")
    print(f"Ionic:            {ionicv or '—'} (optional)")

    problems: list[str] = []
    if sys.version_info[:2] != (3, 11) or sys.version_info[:3] < min_py:
        problems.append("PythonVersion")
    if not _has("node"):
        problems.append("Node.js")
    if not _has("npm"):
        problems.append("npm")

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"- {p}")
        print("\nTip (Windows): use Python 3.11.9+ via the launcher: `py -3.11`")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

