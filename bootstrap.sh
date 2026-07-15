#!/usr/bin/env bash
# Prepare a macOS development machine to run Accessor.
#
# This script is safe to rerun: already-installed Homebrew formulae and the
# existing project virtual environment are reused. It never runs `aws configure`,
# edits ~/.aws, asks for sudo, or starts the Demand Proxy.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
MIN_PYTHON_VERSION="3.11"

log() {
  printf '[accessor setup] %s\n' "$*"
}

fail() {
  printf '[accessor setup] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./bootstrap.sh

Installs missing macOS command dependencies through Homebrew and creates or
updates Accessor's local .venv. It does not configure AWS or Granted accounts.

Optional environment variable:
  PYTHON_BIN   Python 3.11+ executable to use when creating .venv
EOF
}

require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    fail "Accessor proxy setup currently supports macOS only."
  fi
}

require_homebrew() {
  if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew is required. Install it first from https://brew.sh/, then run this script again."
  fi
}

install_formula_if_missing() {
  # Check the usable executable rather than only `brew list`: a developer may
  # already have a valid installation from another supported package manager.
  local executable="$1"
  local formula="$2"
  if command -v "$executable" >/dev/null 2>&1; then
    log "Found $executable."
    return
  fi
  log "Installing Homebrew formula: $formula"
  brew install "$formula"
}

python_is_new_enough() {
  "$1" -c "import sys; raise SystemExit(sys.version_info < (${MIN_PYTHON_VERSION//./,}))"
}

choose_python() {
  local candidate
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidate="$PYTHON_BIN"
    command -v "$candidate" >/dev/null 2>&1 || fail "PYTHON_BIN is not executable: $candidate"
  else
    candidate="$(command -v python3 || true)"
  fi

  if [[ -z "$candidate" ]] || ! python_is_new_enough "$candidate"; then
    log "Installing Homebrew Python (3.11 or newer is required)."
    brew install python@3.14
    candidate="$(brew --prefix python@3.14)/bin/python3.14"
  fi
  printf '%s\n' "$candidate"
}

main() {
  case "${1:-}" in
    -h|--help)
      usage
      return
      ;;
    "")
      ;;
    *)
      usage >&2
      fail "Unknown argument: $1"
      ;;
  esac

  require_macos
  require_homebrew

  # Accessor shells out to these commands. `assume` is provided by Granted.
  install_formula_if_missing aws awscli
  install_formula_if_missing assume granted
  install_formula_if_missing sshuttle sshuttle
  install_formula_if_missing curl curl

  local python
  python="$(choose_python)"
  log "Using $($python --version)."
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log "Creating virtual environment at $VENV_DIR"
    "$python" -m venv "$VENV_DIR"
  else
    log "Reusing virtual environment at $VENV_DIR"
  fi

  log "Installing Accessor Python dependencies."
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

  "$VENV_DIR/bin/python" -c "import boto3, prompt_toolkit"
  log "Environment is ready. Next: cd \"$ROOT_DIR\" && ./accessor"
  log "Ensure Granted is configured and you can run: assume --help"
}

main "$@"
