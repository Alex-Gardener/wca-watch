#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" "$SCRIPT_DIR/wca.py" "$@"
