#!/usr/bin/env bash
# 把合成目标程序编成测试用的产物（不进 git）：JS 目录 / zip / asar、.NET dll、PyInstaller onefile、普通 zip。
# 用法：bash build.sh <输出目录>；在有 docker 的 x86_64 Linux 上跑。
# 镜像可用环境变量换：NODE_IMAGE、PY_IMAGE、DEBIAN_IMAGE；PIP_INDEX、NPM_REGISTRY 指定源。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
NODE_IMAGE="${NODE_IMAGE:-node:22.23.4-slim}"
PY_IMAGE="${PY_IMAGE:-python:3.11-slim}"
DEBIAN_IMAGE="${DEBIAN_IMAGE:-debian:bookworm-slim}"
PIP_INDEX="${PIP_INDEX:-https://pypi.org/simple}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
UID_GID="$(id -u):$(id -g)"

rm -rf "$OUT/js-app" && cp -r "$HERE/js-app" "$OUT/js-app"

docker run --rm -v "$HERE:/src:ro" -v "$OUT:/out" -w /src "$PY_IMAGE" sh -c "
  cd /src/js-app && python -m zipfile -c /out/js-app.zip . &&
  cd /src/plain && python -m zipfile -c /out/plain.zip . &&
  chown $UID_GID /out/js-app.zip /out/plain.zip"

docker run --rm -v "$HERE:/src:ro" -v "$OUT:/out" -e HOME=/tmp "$NODE_IMAGE" sh -c "
  npx --yes --registry $NPM_REGISTRY @electron/asar@3.2.17 pack /src/js-app /out/js-app.asar &&
  chown $UID_GID /out/js-app.asar"

# 安装目录形态：resources/app.asar 加一个占位的可执行文件
rm -rf "$OUT/js-packaged" && mkdir -p "$OUT/js-packaged/resources"
cp "$OUT/js-app.asar" "$OUT/js-packaged/resources/app.asar" && printf 'MZplaceholder' > "$OUT/js-packaged/sample.exe"
docker run --rm -v "$OUT:/out" "$PY_IMAGE" sh -c "
  cd /out/js-packaged && python -m zipfile -c /out/js-packaged.zip . && chown $UID_GID /out/js-packaged.zip"

docker run --rm -v "$HERE:/src:ro" -v "$OUT:/out" "$DEBIAN_IMAGE" sh -c "
  apt-get update -qq >/dev/null && apt-get install -y -qq mono-mcs >/dev/null &&
  mcs -target:library -out:/out/Sample.Orders.dll /src/dotnet/Orders.cs &&
  chown $UID_GID /out/Sample.Orders.dll"

docker run --rm -v "$HERE:/src:ro" -v "$OUT:/out" "$PY_IMAGE" sh -c "
  apt-get update -qq >/dev/null && apt-get install -y -qq binutils >/dev/null &&
  pip install -q -i $PIP_INDEX pyinstaller==6.11.1 &&
  cp -r /src/python /tmp/py && cd /tmp/py &&
  pyinstaller --log-level WARN --onefile --name orders-app orders_app.py &&
  cp dist/orders-app /out/orders-app && chown $UID_GID /out/orders-app"

ls -l "$OUT"
