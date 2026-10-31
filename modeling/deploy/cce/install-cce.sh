#!/usr/bin/env bash
set -euo pipefail
exec python3 -B "$(dirname "$0")/install-cce.py" "$@"
