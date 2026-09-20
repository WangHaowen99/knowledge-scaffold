#!/usr/bin/env bash
# SessionStart hook — surface current ontology stage + outstanding work.
# Output is injected into the assistant's context so future replies are stage-aware.

set -u
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -d "$ROOT/.claude/scripts" ] || exit 0

STATE_DIR="$ROOT/.ontology-state"
STAGE=$(cd "$ROOT" && python3 .claude/scripts/state_helper.py detect-stage 2>/dev/null | tr -d '[:space:]')
[ -z "$STAGE" ] && STAGE="未开始"

LOCKS=""
if [ -d "$STATE_DIR" ]; then
    for f in "$STATE_DIR"/lock-*.json; do
        [ -f "$f" ] || continue
        name=$(basename "$f" .json | sed 's/^lock-//')
        LOCKS="$LOCKS $name"
    done
fi

REPORT_NAME=""
REPORT_PENDING=0
if [ -d "$ROOT/reports" ]; then
    latest=$(ls -1t "$ROOT/reports"/*.md 2>/dev/null | head -1)
    if [ -n "$latest" ]; then
        REPORT_NAME=$(basename "$latest")
        REPORT_PENDING=$(grep -c '^\- \[ \]' "$latest" 2>/dev/null || echo 0)
    fi
fi

# Build the additionalContext string via python3 so every field is JSON-escaped
# correctly (Chinese filenames, quotes, backslashes, embedded newlines).
STAGE="$STAGE" LOCKS="$LOCKS" REPORT_NAME="$REPORT_NAME" REPORT_PENDING="$REPORT_PENDING" \
python3 - <<'PY'
import json, os

stage = os.environ.get('STAGE', '').strip() or '未开始'
locks = os.environ.get('LOCKS', '').strip()
report_name = os.environ.get('REPORT_NAME', '').strip()
try:
    report_pending = int(os.environ.get('REPORT_PENDING', '0') or '0')
except ValueError:
    report_pending = 0

parts = [f"本体项目当前阶段：{stage}。"]
if locks:
    parts.append(f"进行中的命令：{locks.strip()}。")
if report_name:
    if report_pending > 0:
        parts.append(f"最近报告：{report_name}（{report_pending} 个 [ ] 待处理）")
    else:
        parts.append(f"最近报告：{report_name}")

payload = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": " ".join(parts),
    }
}
print(json.dumps(payload, ensure_ascii=False))
PY
