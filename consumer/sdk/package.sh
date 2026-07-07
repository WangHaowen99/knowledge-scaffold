#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-all}"

case "$TARGET" in
  all)
    echo "Packaging TypeScript SDK..."
    "$ROOT_DIR/typescript-sdk/package.sh"
    echo
    echo "Packaging Python SDK..."
    "$ROOT_DIR/python-sdk/package.sh"
    ;;
  typescript)
    "$ROOT_DIR/typescript-sdk/package.sh"
    ;;
  python)
    "$ROOT_DIR/python-sdk/package.sh"
    ;;
  *)
    echo "Usage: $0 [all|typescript|python]"
    exit 1
    ;;
esac

echo

echo "SDK packaging finished."