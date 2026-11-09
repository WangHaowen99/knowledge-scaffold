#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'Usage: build-images.sh <version> <amd64|arm64> [registry-prefix]' >&2
  echo 'Without a registry prefix, images are loaded into local Docker.' >&2
  echo 'With a registry prefix, images are pushed for development CCE deployment.' >&2
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

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
project="$root/modeling/onto-modeling"
wheels="$project/vendor/wheels"
drawio="$project/vendor/drawio/webapp"
onto_pip_index="${ONTO_MODELING_PIP_INDEX_URL:-${PIP_INDEX_URL:-https://repo.huaweicloud.com/repository/pypi/simple}}"
mkdir -p "$wheels" "$drawio"

prepare_vendor() {
  local wheel_arch marker requirements_hash index_hash marker_value
  if [ "$arch" = amd64 ]; then
    wheel_arch=x86_64
  else
    wheel_arch=aarch64
  fi
  requirements_hash="$(sha256sum "$project/backend/requirements.txt" | cut -d ' ' -f 1)"
  index_hash="$(printf '%s' "$onto_pip_index" | sha256sum | cut -d ' ' -f 1)"
  marker_value="${requirements_hash}:${index_hash}"
  marker="$wheels/.prepared-$arch"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$marker_value" ]; then
    echo "Reusing prepared Python wheels for $arch"
  elif command -v pip3 >/dev/null 2>&1; then
    echo "Preparing Python wheels for $arch"
    if pip3 download -r "$project/backend/requirements.txt" -d "$wheels" \
      --only-binary=:all: \
      --platform "manylinux_2_17_${wheel_arch}" \
      --platform "manylinux2014_${wheel_arch}" \
      --platform "manylinux_2_28_${wheel_arch}" \
      --python-version 3.11 --implementation cp \
      --abi cp311 --abi abi3 --abi none \
      -i "$onto_pip_index"; then
      printf '%s\n' "$marker_value" > "$marker"
    else
      echo 'Warning: wheel cache incomplete; Docker build will use its package index.' >&2
    fi
  else
    echo 'Note: pip3 unavailable; Docker build will use its package index.' >&2
  fi

  if [ -f "$drawio/index.html" ]; then
    echo 'Reusing prepared drawio webapp'
  elif ! command -v npm >/dev/null 2>&1; then
    echo 'Warning: host npm unavailable; drawio editing will be unavailable in this package.' >&2
  else
    if (
      temp="$(mktemp -d)" || exit 1
      trap 'rm -rf "$temp"' EXIT
      npm pack '@yank-note/extension-drawio@2.7.0' \
        --pack-destination "$temp" --registry=https://registry.npmmirror.com >/dev/null 2>&1 \
      && tar -xzf "$temp/yank-note-extension-drawio-2.7.0.tgz" \
        -C "$temp" package/drawio/src/main/webapp \
      && cp -a "$temp/package/drawio/src/main/webapp/." "$drawio/" \
        && test -f "$drawio/index.html"
    ); then
      echo 'Drawio webapp prepared.'
    else
      echo 'Warning: drawio preparation failed; drawio editing will be unavailable in this package.' >&2
    fi
  fi
}

prepare_vendor

if [ "$#" -eq 3 ]; then
  registry="${3%/}"
  [[ -n "$registry" ]] || { usage; exit 2; }
  output=(--push)
else
  registry=modeling-offline
  output=(--load)
fi
docker buildx version >/dev/null

docker buildx build --platform "linux/$arch" "${output[@]}" \
  -t "$registry/modeling:$version" "$root/modeling"
docker buildx build --platform "linux/$arch" "${output[@]}" --target runtime \
  -t "$registry/modeling-agent:$version" "$root/modeling/modeling-agent"
docker buildx build --platform "linux/$arch" "${output[@]}" \
  --build-arg "PIP_INDEX_URL=$onto_pip_index" -f "$project/Dockerfile" \
  -t "$registry/onto-modeling:$version" "$project"

hub_build_args=()
if [ -n "${KNOWLEDGE_HUB_APT_MIRROR:-}" ]; then
  hub_build_args+=(--build-arg "APT_MIRROR=$KNOWLEDGE_HUB_APT_MIRROR")
fi
hub_pip_index="${KNOWLEDGE_HUB_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
if [ -n "$hub_pip_index" ]; then
  hub_build_args+=(--build-arg "PIP_INDEX_URL=$hub_pip_index")
fi
docker buildx build --platform "linux/$arch" "${output[@]}" "${hub_build_args[@]}" \
  -t "$registry/knowledge-hub:$version" "$root/knowledge-hub"

eval_build_args=()
eval_pip_index="${EVAL_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
if [ -n "$eval_pip_index" ]; then
  eval_build_args+=(--build-arg "PIP_INDEX_URL=$eval_pip_index")
fi
eval_npm_registry="${EVAL_NPM_REGISTRY:-${NPM_REGISTRY:-}}"
if [ -n "$eval_npm_registry" ]; then
  eval_build_args+=(--build-arg "NPM_REGISTRY=$eval_npm_registry")
fi
docker buildx build --platform "linux/$arch" "${output[@]}" "${eval_build_args[@]}" \
  -t "$registry/evaluation:$version" "$root/eval-service"

if [ "$#" -eq 3 ]; then
  echo 'Record and verify the immutable manifest digest for each pushed tag before packaging:'
  for component in modeling modeling-agent onto-modeling knowledge-hub evaluation; do
    docker buildx imagetools inspect "$registry/$component:$version"
  done
else
  echo "Local images are ready. Package with: python3 modeling/deploy/cce/package-cce.py --version $version --arch $arch --local --output <package.tar.gz>"
fi
