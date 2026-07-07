#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

"$ROOT_DIR/build.sh"

cd "$ROOT_DIR"

echo "Packing TypeScript SDK..."
npm pack

echo "Done. Package artifact generated in: $ROOT_DIR"