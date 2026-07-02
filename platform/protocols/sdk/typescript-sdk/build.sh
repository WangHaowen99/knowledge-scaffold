#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR"

echo "Installing TypeScript SDK dependencies..."
npm install --no-fund --no-audit

echo "Building TypeScript SDK..."
npm run build

echo "Done. Output directory: $ROOT_DIR/dist"