#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$ROOT_DIR/.venv-build"

cd "$ROOT_DIR"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating build virtualenv..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip setuptools wheel build
rm -rf dist build *.egg-info
python -m build

echo "Done. Python package artifacts generated in: $ROOT_DIR/dist"