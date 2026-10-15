#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'Usage: build-offline.sh <version> <amd64|arm64> [output.tar.gz]' >&2
}

if [ "${1:-}" = '-h' ] || [ "${1:-}" = '--help' ]; then
  usage
  exit 0
fi
[ "$#" -ge 2 ] && [ "$#" -le 3 ] || { usage; exit 2; }
version="$1"
arch="$2"
[[ "$version" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { usage; exit 2; }
[[ "$arch" == amd64 || "$arch" == arm64 ]] || { usage; exit 2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
modeling_dir="$(cd "$script_dir/../.." && pwd)"
output="${3:-$modeling_dir/dist/modeling-cce-$version-$arch.tar.gz}"
[[ -n "$output" ]] || { usage; exit 2; }

bash "$script_dir/build-images.sh" "$version" "$arch"
python3 "$script_dir/package-cce.py" \
  --version "$version" --arch "$arch" --local --output "$output"
