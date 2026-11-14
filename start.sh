#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"
export PYTHONUTF8=1
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON=python3
    elif command -v python >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON=python
    else
        printf '%s\n' 'Python 3.11 or newer is required: https://www.python.org/downloads/' >&2
        exit 1
    fi
    if ! "$BOOTSTRAP_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        printf '%s\n' 'Python 3.11 or newer is required.' >&2
        exit 1
    fi
    if ! "$BOOTSTRAP_PYTHON" -m venv .venv; then
        printf '%s\n' 'Cannot create .venv. On Debian/Ubuntu, install the python3-venv package.' >&2
        exit 1
    fi
fi

if ! "$VENV_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11); import consolecraft, minecraft_launcher_lib, rich, platformdirs, requests, keyring, questionary' >/dev/null 2>&1; then
    printf '%s\n' 'Installing Console Launcher dependencies...'
    "$VENV_PYTHON" -m pip install --disable-pip-version-check -e .
fi

exec "$VENV_PYTHON" -m consolecraft "$@"

