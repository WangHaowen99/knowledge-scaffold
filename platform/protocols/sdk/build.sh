#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-all}"

case "$TARGET" in
  all)
    echo "Building TypeScript SDK..."
    "$ROOT_DIR/typescript-sdk/build.sh"
    echo
    echo "Building Python SDK..."
    "$ROOT_DIR/python-sdk/build.sh"
    ;;
  typescript)
    "$ROOT_DIR/typescript-sdk/build.sh"
    ;;
  python)
    "$ROOT_DIR/python-sdk/build.sh"
    ;;
  *)
    echo "Usage: $0 [all|typescript|python]"
    exit 1
    ;;
esac

echo

echo "SDK build finished."