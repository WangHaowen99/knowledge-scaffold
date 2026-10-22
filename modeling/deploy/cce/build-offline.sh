#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'Usage: build-offline.sh <version> <amd64|arm64> [output.tar.gz] [--cleanup-images]' >&2
  echo '  --cleanup-images removes only the three local images built for this version after packaging.' >&2
}

if [ "${1:-}" = '-h' ] || [ "${1:-}" = '--help' ]; then
  usage
  exit 0
fi
[ "$#" -ge 2 ] && [ "$#" -le 4 ] || { usage; exit 2; }
version="$1"
arch="$2"
[[ "$version" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { usage; exit 2; }
[[ "$arch" == amd64 || "$arch" == arm64 ]] || { usage; exit 2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
modeling_dir="$(cd "$script_dir/../.." && pwd)"
output="${3:-$modeling_dir/dist/modeling-cce-$version-$arch.tar.gz}"
[[ -n "$output" ]] || { usage; exit 2; }
cleanup_images=0
if [ "${3:-}" = "--cleanup-images" ]; then
  output="$modeling_dir/dist/modeling-cce-$version-$arch.tar.gz"
  cleanup_images=1
elif [ "${4:-}" = "--cleanup-images" ]; then
  cleanup_images=1
fi
if [ "$#" -eq 4 ] && [ "${3:-}" = "--cleanup-images" ]; then
  usage
  exit 2
fi
if [ "$#" -eq 4 ] && [ "${4:-}" != "--cleanup-images" ]; then
  usage
  exit 2
fi

bash "$script_dir/build-images.sh" "$version" "$arch"
python3 "$script_dir/package-cce.py" \
  --version "$version" --arch "$arch" --local --output "$output"

if [ "$cleanup_images" -eq 1 ]; then
  docker image rm \
    "modeling-offline/modeling:$version" \
    "modeling-offline/modeling-agent:$version" \
    "modeling-offline/onto-modeling:$version"
  echo "Removed only the three local Modeling images for version $version."
fi
