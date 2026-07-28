#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BACKEND_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if [ "${PYTHON_EXE:-}" != "" ]; then
  PYTHON_CMD="$PYTHON_EXE"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "No usable Python interpreter found." >&2
  exit 1
fi

export PYTHONPATH="$BACKEND_ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_CMD" -m pytest \
  "$BACKEND_ROOT/tests/test_interaction_guard_chain.py" \
  -q
