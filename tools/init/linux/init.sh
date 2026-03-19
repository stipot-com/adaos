#!/usr/bin/env bash
# Minimal "download & bootstrap" entrypoint (Linux).
# Intended to be served as: https://app.inimatic.com/linux/init.sh
set -euo pipefail

REPO_OWNER="${ADAOS_INIT_REPO_OWNER:-stipot-com}"
REPO_NAME="${ADAOS_INIT_REPO_NAME:-adaos}"
REV_DEFAULT="${ADAOS_INIT_REV:-rev2026}"
DEST_DEFAULT="${ADAOS_INIT_DEST:-$HOME/adaos}"

log()  { printf '\033[36m[*] %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

fetch_to_file() {
  local url="$1"
  local out="$2"
  if have curl; then
    curl -fsSL "$url" -o "$out"
    return $?
  fi
  if have wget; then
    wget -qO "$out" "$url"
    return $?
  fi
  die "Neither curl nor wget is available. Install one of them and retry."
}

usage() {
  cat <<EOF
Usage: init.sh [--dest DIR] [--rev REV] [--use-git] [--] [bootstrap args...]

Defaults:
  --rev  ${REV_DEFAULT}
  --dest ${DEST_DEFAULT}

Examples:
  curl -fsSL https://app.inimatic.com/linux/init.sh | bash -s -- --join-code ABCD
  curl -fsSL https://app.inimatic.com/linux/init.sh | bash -s -- --role hub --install-service auto
EOF
}

try_install_git() {
  have git && return 0
  local sudo_cmd=""
  if [[ "${EUID:-$(id -u)}" == "0" ]]; then
    sudo_cmd=""
  elif have sudo && sudo -n true >/dev/null 2>&1; then
    sudo_cmd="sudo -n"
  else
    return 1
  fi

  if have apt-get; then
    $sudo_cmd apt-get update -y >/dev/null 2>&1 || true
    $sudo_cmd apt-get install -y git >/dev/null 2>&1 && return 0
  fi
  if have dnf; then
    $sudo_cmd dnf install -y git >/dev/null 2>&1 && return 0
  fi
  if have yum; then
    $sudo_cmd yum install -y git >/dev/null 2>&1 && return 0
  fi
  if have apk; then
    $sudo_cmd apk add --no-cache git >/dev/null 2>&1 && return 0
  fi
  if have pacman; then
    $sudo_cmd pacman -Sy --noconfirm git >/dev/null 2>&1 && return 0
  fi
  if have zypper; then
    $sudo_cmd zypper --non-interactive install git >/dev/null 2>&1 && return 0
  fi
  return 1
}

DEST="$DEST_DEFAULT"
REV="$REV_DEFAULT"
USE_GIT="${ADAOS_INIT_USE_GIT:-0}"
BOOTSTRAP_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dest) DEST="${2:-}"; shift 2 ;;
    --rev) REV="${2:-}"; shift 2 ;;
    --use-git) USE_GIT="1"; shift ;;
    --) shift; BOOTSTRAP_ARGS+=("$@"); break ;;
    *) BOOTSTRAP_ARGS+=("$1"); shift ;;
  esac
done

[[ -n "${DEST:-}" ]] || die "--dest is empty"
[[ -n "${REV:-}" ]] || die "--rev is empty"

REPO_DIR="$DEST"

log "Preparing repo at: ${REPO_DIR}"
mkdir -p "$REPO_DIR"

if ! have git; then
  log "git not found; trying to install (best-effort)..."
  if try_install_git; then
    ok "git installed"
  else
    warn "git is not available; AdaOS will run in archive (no-git) mode for skills/scenarios until you enable git"
  fi
fi

if [[ "$USE_GIT" == "1" ]]; then
  if ! have git; then
    die "git is not installed (required for --use-git). Either install git, or run without --use-git (archive download)."
  fi
  if [[ -d "$REPO_DIR/.git" ]]; then
    log "Existing git repo detected; updating..."
    git -C "$REPO_DIR" fetch --all --prune
    git -C "$REPO_DIR" checkout "$REV"
    git -C "$REPO_DIR" pull --ff-only
  else
    log "Cloning ${REPO_OWNER}/${REPO_NAME} (${REV})..."
    git clone -b "$REV" "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" "$REPO_DIR"
  fi
else
  # No-git path: download GitHub archive.
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp" >/dev/null 2>&1 || true' EXIT
  archive="$tmp/adaos.tar.gz"
  url="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REV}"
  log "Downloading source archive: ${url}"
  fetch_to_file "$url" "$archive"

  log "Extracting..."
  rm -rf "$REPO_DIR"
  mkdir -p "$REPO_DIR"
  tar -xzf "$archive" -C "$tmp"
  top_dir="$(find "$tmp" -maxdepth 1 -type d -name "${REPO_NAME}-*" | head -n 1 || true)"
  [[ -n "${top_dir:-}" ]] || die "Failed to locate extracted directory"
  # Move extracted content into DEST (portable, avoids rsync dependency).
  (cd "$top_dir" && tar -cf - .) | (cd "$REPO_DIR" && tar -xf -)
  ok "Source extracted to: ${REPO_DIR}"
fi

cd "$REPO_DIR"

# Ensure bootstrap gets a --rev unless caller already passed one.
have_rev=0
for ((i=0; i<${#BOOTSTRAP_ARGS[@]}; i++)); do
  if [[ "${BOOTSTRAP_ARGS[$i]}" == "--rev" ]]; then
    have_rev=1
    break
  fi
done
if [[ "$have_rev" != "1" ]]; then
  BOOTSTRAP_ARGS+=("--rev" "$REV")
fi

log "Running bootstrap..."
bash tools/bootstrap.sh "${BOOTSTRAP_ARGS[@]}"
