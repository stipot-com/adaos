#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[AdaOS] No .venv found. Run bootstrap first:" >&2
  echo "  bash tools/bootstrap.sh" >&2
  exit 1
fi

exec .venv/bin/python -m adaos "$@"
