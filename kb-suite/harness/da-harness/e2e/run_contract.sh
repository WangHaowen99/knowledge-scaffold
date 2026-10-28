#!/usr/bin/env bash
# Run the openJiuwen provider contract suite against the DA engine on the
# kb-suite network, inside the Swarm image (same openjiuwen as the host).
#   AGENT_CORE_REPO   agent-core clone (required)
#   AGENT_CORE_REF    ref matching the image's openjiuwen (default v0.1.19)
#   E2E_ENGINE        deepanalyze (default) or loopforest (lf-engine, provider from ../lf-harness)
#   SWARM_IMAGE       image to run in (default kb-suite/swarm:0.2.7); for the
#                     develop build use kb-suite/swarm:dev with the agent-core
#                     commit its pyproject pins as AGENT_CORE_REF
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${AGENT_CORE_REPO:?set AGENT_CORE_REPO to an agent-core clone}"
REF="${AGENT_CORE_REF:-v0.1.19}"
IMAGE="${SWARM_IMAGE:-kb-suite/swarm:0.2.7}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$REPO" archive "$REF" tests | tar -x -C "$WORK"
SECRET="$(grep '^DA_ENGINE_SECRET=' "${KBS_SECRETS_DIR:-$HOME/.config/kb-suite}/secrets.env" | cut -d= -f2-)"
docker run --rm --network kb-suite_default \
  -v "$HERE:/src:ro" -v "$HERE/../lf-harness:/lf:ro" -v "$WORK:/agentcore" -w /agentcore \
  -e PYTHONPATH=/agentcore:/src:/lf -e PYTHONDONTWRITEBYTECODE=1 -e E2E_ENGINE="${E2E_ENGINE:-deepanalyze}" \
  -e DA_BASE_URL=http://da-engine:8192 -e DA_ENGINE_SECRET="$SECRET" \
  -e LF_BASE_URL=http://lf-engine:8193 -e LF_ENGINE_SECRET="$SECRET" \
  --entrypoint sh "$IMAGE" -c "
    pip install -q pytest pytest-asyncio -i https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1
    python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto --rootdir=/src/e2e /src/e2e $*"
