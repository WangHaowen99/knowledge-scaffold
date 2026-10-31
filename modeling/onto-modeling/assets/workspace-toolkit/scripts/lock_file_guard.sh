#!/usr/bin/env bash
# PreToolUse hook — block hand edits to tool-managed files in .ontology-state/.
#
# CLAUDE.md §9 declares the .ontology-state/ directory tool-managed. Lock
# files are written/cleared by state_helper.py; editing them by hand corrupts
# cross-command coordination. The script run verification record is written
# only by script_verify.py run; a hand-written record would bypass the
# pre-commit verification gate. This hook denies Edit/Write/MultiEdit calls
# on either, with a message pointing the operator at the correct command.

set -u

HOOK_INPUT=$(cat 2>/dev/null || true)
[ -z "$HOOK_INPUT" ] && exit 0

HOOK_INPUT="$HOOK_INPUT" python3 - <<'PY'
import json
import os
import re
import sys

try:
    data = json.loads(os.environ.get('HOOK_INPUT', '') or "{}")
except Exception:
    sys.exit(0)

file_path = (data.get('tool_input') or {}).get('file_path') or ''
if not file_path:
    sys.exit(0)

norm = file_path.replace('\\', '/')
if re.search(r'(^|/)\.ontology-state/lock-[^/]+\.json$', norm):
    reason = (
        "锁文件由工具管理，禁止手工编辑（见 CLAUDE.md §9）。"
        "如需清理残留锁，运行：python3 .claude/scripts/state_helper.py force-release <cmd>"
    )
elif re.search(r'(^|/)\.ontology-state/script-verifications\.json$', norm):
    reason = (
        "脚本运行验证记录只能由验证工具写入，禁止手工编辑。请编写用例并运行："
        "python3 .claude/scripts/script_verify.py run <脚本> --cases <用例文件>"
    )
else:
    sys.exit(0)

print(json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    },
    ensure_ascii=False,
))
PY
