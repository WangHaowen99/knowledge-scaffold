#!/usr/bin/env bash
# Build the Loop Forest service image: lf-service (this repo) on top of
# agent-solution's loop-forest module.
#
# Needs agent-core-java, agent-runtime-java and agent-core-ext-* already in the
# kbs-m2 Maven volume, i.e. run ./kbsuite.sh build-da first with the same refs.
# Output: image kb-suite/loop-forest:<solution commit> (+ :current).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
SOLUTION_REPO="${DA_SOLUTION_REPO:?set DA_SOLUTION_REPO to an agent-solution clone}"
REF="${LF_SOLUTION_REF:-${DA_SOLUTION_REF:-origin/common}}"
WORK="${LF_BUILD_DIR:-/tmp/lf-build}"

git -C "$SOLUTION_REPO" fetch -q origin common:refs/remotes/origin/common
solution="$(git -C "$SOLUTION_REPO" rev-parse --short "$REF")"
rm -rf "$WORK" && mkdir -p "$WORK/agent-solution"
git -C "$SOLUTION_REPO" archive "$solution" pom.xml common/agents/loop-forest | tar -x -C "$WORK/agent-solution"
cp -r "$ROOT/lf-service" "$WORK/lf-service"
rm -rf "$WORK/lf-service/target"

cat > "$WORK/settings.xml" <<'XML'
<settings><mirrors><mirror><id>huaweicloud</id><mirrorOf>central</mirrorOf>
<url>https://repo.huaweicloud.com/repository/maven/</url></mirror></mirrors></settings>
XML
cat > "$WORK/build.sh" <<'SH'
set -e
cd /build
M="mvn -B -q -s /build/settings.xml"
test -d /root/.m2/repository/com/openjiuwen/agent-core-ext-react-rails \
  || { echo "agent-core-ext-react-rails missing in kbs-m2: run ./kbsuite.sh build-da first" >&2; exit 1; }
$M -N -f agent-solution/pom.xml install
$M -f agent-solution/common/agents/loop-forest/pom.xml install -DskipTests
$M -f lf-service/pom.xml package
SH
docker run --rm -m "${LF_BUILD_MEMORY:-3g}" -e MAVEN_OPTS="${LF_MAVEN_OPTS:--Xmx1536m}" \
  -v "$WORK:/build" -v kbs-m2:/root/.m2 maven:3.9-eclipse-temurin-17 bash /build/build.sh

docker build --build-arg JRE_IMAGE="${LF_JRE_IMAGE:-eclipse-temurin:17-jre}" \
  --label "kb-suite.lf-baseline=solution:$solution" \
  -t "kb-suite/loop-forest:$solution" -t kb-suite/loop-forest:current \
  -f "$HERE/Dockerfile" "$WORK/lf-service/target"
