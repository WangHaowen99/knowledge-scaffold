#!/usr/bin/env bash
# Build DA-java from the three development branches (see DA deploy-dev/README):
# agent-core-java@develop, agent-runtime-java@develop, agent-solution@common.
# Sources are exported with git archive after a fetch; working copies are not
# touched. Output: image kb-suite/deepanalyze-java:<solution commit> (+ :current)
# and deploy/compose/da/build/knowledge-service.json. DA_IMAGE_TAG names the second tag
# instead of :current (the tag compose deploys by default), for a build a test stack tries first.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CORE_REPO="${DA_CORE_REPO:?set DA_CORE_REPO to an agent-core-java clone}"
RUNTIME_REPO="${DA_RUNTIME_REPO:?set DA_RUNTIME_REPO to an agent-runtime-java clone}"
SOLUTION_REPO="${DA_SOLUTION_REPO:?set DA_SOLUTION_REPO to an agent-solution clone}"
WORK="${DA_BUILD_DIR:-/tmp/da-build}"

fetch_export() {  # repo branch dest [ref]
  # a pinned commit the clone already has needs no fetch (rebuilding a baseline offline)
  if [ -z "${4:-}" ] || ! git -C "$1" cat-file -e "$4^{commit}" 2>/dev/null; then
    git -C "$1" fetch -q origin "$2:refs/remotes/origin/$2"
  fi
  local ref="${4:-origin/$2}"
  rm -rf "$WORK/$3" && mkdir -p "$WORK/$3"
  git -C "$1" archive "$ref" | tar -x -C "$WORK/$3"
  git -C "$1" rev-parse --short "$ref"
}

mkdir -p "$WORK"
# DA_*_REF pin a commit (e.g. to rebuild the deployed baseline with a new patch).
core=$(fetch_export "$CORE_REPO" develop agent-core-java "${DA_CORE_REF:-}")
runtime=$(fetch_export "$RUNTIME_REPO" develop agent-runtime-java "${DA_RUNTIME_REF:-}")
solution=$(fetch_export "$SOLUTION_REPO" common agent-solution "${DA_SOLUTION_REF:-}")
# Local fixes waiting to land upstream (see each patch header). DA_PATCHES picks some of them by
# number ("0001 0002 0004") for a baseline the others do not apply to (deploy/compose/README.md).
patches=""
for patch in "$HERE"/patches/*.patch; do
  [ -e "$patch" ] || continue
  if [ -n "${DA_PATCHES:-}" ] && [[ " $DA_PATCHES " != *" $(basename "$patch" | cut -c1-4) "* ]]; then
    continue
  fi
  (cd "$WORK/agent-solution" && git apply -p1 "$patch")
  patches="$patches $(basename "$patch" .patch)"
done
echo "core=$core runtime=$runtime solution=$solution patches=${patches# }" | tee "$WORK/BASELINE"

cat > "$WORK/settings.xml" <<'XML'
<settings><mirrors><mirror><id>huaweicloud</id><mirrorOf>central</mirrorOf>
<url>https://repo.huaweicloud.com/repository/maven/</url></mirror></mirrors></settings>
XML
cat > "$WORK/build.sh" <<'SH'
set -e
cd /build
M="mvn -B -q -s /build/settings.xml"
if [ "${DA_SKIP_DEPS:-0}" != 1 ]; then
  $M -f agent-core-java/pom.xml clean install -Dmaven.test.skip=true
  $M -f agent-runtime-java/pom.xml clean install -DskipTests
  # Module builds do not install their parent POM; install it on its own first.
  $M -U -N -f agent-solution/pom.xml install
  $M -f agent-solution/common/agent-core-ext-java/pom.xml clean install -DskipTests
  $M -f agent-solution/common/agent-runtime-ext-java/pom.xml clean install -DskipTests
fi
$M -f agent-solution/common/agents/deepanalyze-java/pom.xml clean package -DskipTests
SH
# Every module targets Java 17, so the JDK 17 Maven image is enough. The memory
# cap keeps a hungry build from taking the whole (shared) host down.
# DA_SKIP_DEPS=1 reuses core/runtime/ext already installed in kbs-m2 (same refs).
docker run --rm -m "${DA_BUILD_MEMORY:-4g}" -e MAVEN_OPTS="${DA_MAVEN_OPTS:--Xmx2g}" \
  -e DA_SKIP_DEPS="${DA_SKIP_DEPS:-0}" \
  -v "$WORK:/build" -v kbs-m2:/root/.m2 maven:3.9-eclipse-temurin-17 bash /build/build.sh

DA_DIR="$WORK/agent-solution/common/agents/deepanalyze-java"
docker build --build-arg PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple \
  --build-arg JRE_IMAGE="${DA_JRE_IMAGE:-eclipse-temurin:21-jre-jammy}" \
  --label "kb-suite.da-baseline=core:$core runtime:$runtime solution:$solution" \
  --label "kb-suite.da-patches=${patches# }" \
  -t "kb-suite/deepanalyze-java:$solution" -t "kb-suite/deepanalyze-java:${DA_IMAGE_TAG:-current}" \
  -f "$HERE/Dockerfile" "$DA_DIR"
python3 "$HERE/make_kb_spec.py" "$DA_DIR/engine/src/main/resources/tools/openapi" "$HERE/build/knowledge-service.json"
