#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "[AdaOS] Source this script instead of executing it:" >&2
  echo "  source tools/slot-shell.sh" >&2
  echo "  source tools/slot-shell.sh --cd" >&2
  exit 1
fi

_adaos_slot_shell_main() {
  local cd_repo=0
  local arg=""
  while (($# > 0)); do
    arg="$1"
    case "$arg" in
      --cd)
        cd_repo=1
        ;;
      -h|--help)
        cat <<'EOF'
Usage:
  source tools/slot-shell.sh
  source tools/slot-shell.sh --cd
EOF
        return 0
        ;;
      *)
        echo "[AdaOS] Unknown argument: $arg" >&2
        echo "Usage: source tools/slot-shell.sh [--cd]" >&2
        return 1
        ;;
    esac
    shift
  done

  local base_dir="${ADAOS_BASE_DIR:-$HOME/.adaos}"
  local slots_root="$base_dir/state/core_slots"
  local active_path="$slots_root/active"
  if [[ ! -f "$active_path" ]]; then
    echo "[AdaOS] Active core slot marker not found: $active_path" >&2
    return 1
  fi

  local active_slot
  active_slot="$(tr -d '[:space:]' < "$active_path")"
  if [[ "$active_slot" != "A" && "$active_slot" != "B" ]]; then
    echo "[AdaOS] Invalid active core slot marker: ${active_slot:-<empty>}" >&2
    return 1
  fi

  local slot_dir="$slots_root/slots/$active_slot"
  local manifest_path="$slot_dir/manifest.json"
  if [[ ! -f "$manifest_path" ]]; then
    echo "[AdaOS] Active slot manifest not found: $manifest_path" >&2
    return 1
  fi

  local python_bin=""
  if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    python_bin="python"
  else
    echo "[AdaOS] Python is required to read slot metadata." >&2
    return 1
  fi

  if declare -F deactivate >/dev/null 2>&1; then
    deactivate >/dev/null 2>&1 || true
  fi

  local payload=""
  if ! payload="$("$python_bin" - "$manifest_path" "$slot_dir" "$active_slot" "$base_dir" <<'PY'
import json
import re
import shlex
import sys
from pathlib import Path

manifest_path, slot_dir, slot, base_dir = sys.argv[1:5]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
if not isinstance(manifest, dict):
    raise SystemExit("manifest must be an object")

slot_path = Path(slot_dir)
repo_dir = str(manifest.get("repo_dir") or manifest.get("cwd") or (slot_path / "repo")).strip()
venv_dir = str(manifest.get("venv_dir") or (slot_path / "venv")).strip()
cwd = str(manifest.get("cwd") or repo_dir).strip()
env_map = manifest.get("env") if isinstance(manifest.get("env"), dict) else {}
env_map = {str(key): str(value) for key, value in env_map.items()}

if repo_dir:
    env_map.setdefault("ADAOS_SLOT_REPO_ROOT", repo_dir)
    src_dir = Path(repo_dir) / "src"
    if src_dir.exists():
        env_map.setdefault("PYTHONPATH", str(src_dir))

env_map.setdefault("ADAOS_BASE_DIR", str(base_dir))

print(f"export ADAOS_ACTIVE_CORE_SLOT={shlex.quote(slot)}")
print(f"export ADAOS_ACTIVE_CORE_SLOT_DIR={shlex.quote(str(slot_path))}")
for key in sorted(env_map):
    if key in {"PATH", "VIRTUAL_ENV", "PYTHONHOME"}:
        continue
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        continue
    print(f"export {key}={shlex.quote(env_map[key])}")
print(f"ADAOS_SLOT_SHELL_VENV_DIR={shlex.quote(venv_dir)}")
print(f"ADAOS_SLOT_SHELL_CWD={shlex.quote(cwd)}")
PY
)"; then
    echo "[AdaOS] Failed to read active slot manifest: $manifest_path" >&2
    return 1
  fi

  eval "$payload"

  local activate_path="$ADAOS_SLOT_SHELL_VENV_DIR/bin/activate"
  if [[ ! -f "$activate_path" ]]; then
    activate_path="$ADAOS_SLOT_SHELL_VENV_DIR/Scripts/activate"
  fi
  if [[ ! -f "$activate_path" ]]; then
    echo "[AdaOS] Slot activation script not found under: $ADAOS_SLOT_SHELL_VENV_DIR" >&2
    unset ADAOS_SLOT_SHELL_VENV_DIR ADAOS_SLOT_SHELL_CWD
    return 1
  fi

  unset PYTHONHOME
  # shellcheck disable=SC1090
  source "$activate_path"
  hash -r 2>/dev/null || true

  if [[ "$cd_repo" == "1" && -n "${ADAOS_SLOT_SHELL_CWD:-}" ]]; then
    cd "$ADAOS_SLOT_SHELL_CWD" || return 1
  fi

  unset ADAOS_SLOT_SHELL_VENV_DIR ADAOS_SLOT_SHELL_CWD
  return 0
}

_adaos_slot_shell_main "$@"
_adaos_slot_shell_status=$?
unset -f _adaos_slot_shell_main
return "$_adaos_slot_shell_status"
