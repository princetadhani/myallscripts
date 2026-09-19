#!/usr/bin/env bash
# Activate the myallscripts venv in your CURRENT shell.
#
# This script must be SOURCED, not executed, because a venv activation
# only affects the shell it runs in — a subprocess can't change your
# interactive shell's environment.
#
# Usage (from anywhere):
#   source /Users/prince.tadhani/myallscripts/activate.sh
#   . /Users/prince.tadhani/myallscripts/activate.sh
#
# Or, from inside myallscripts/:
#   source activate.sh

# Resolve this script's own directory regardless of cwd, works in bash/zsh.
if [ -n "${BASH_SOURCE:-}" ]; then
    _MYALLSCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _MYALLSCRIPTS_DIR="$(cd "$(dirname "${(%):-%x}")" && pwd)"
else
    echo "[activate.sh] Unsupported shell — please run: source venv/bin/activate" >&2
    return 1 2>/dev/null || exit 1
fi

_VENV_ACTIVATE="${_MYALLSCRIPTS_DIR}/venv/bin/activate"

if [ ! -f "${_VENV_ACTIVATE}" ]; then
    echo "[activate.sh] venv not found at ${_MYALLSCRIPTS_DIR}/venv" >&2
    echo "[activate.sh] Create it with: python3 -m venv ${_MYALLSCRIPTS_DIR}/venv" >&2
    return 1 2>/dev/null || exit 1
fi

source "${_VENV_ACTIVATE}"
unset _MYALLSCRIPTS_DIR _VENV_ACTIVATE

echo "[activate.sh] myallscripts venv activated ($(python3 --version))"
