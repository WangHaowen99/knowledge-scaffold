#!/usr/bin/env bash
# PreToolUse hook — block hand edits to .ontology-state/lock-*.json.
#
# CLAUDE.md §9 declares the .ontology-state/ directory tool-managed. Lock
# files are written/cleared by state_helper.py; editing them by hand corrupts
# cross-command coordination. This hook denies Edit/Write/MultiEdit calls
# whose target path matches .ontology-state/lock-<cmd>.json, with a message
# pointing the operator at the correct cleanup command.

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
if not re.search(r'(^|/)\.ontology-state/lock-[^/]+\.json$', norm):
    sys.exit(0)

print(json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "锁文件由工具管理，禁止手工编辑（见 CLAUDE.md §9）。"
                "如需清理残留锁，运行：python3 .claude/scripts/state_helper.py force-release <cmd>"
            ),
        }
    },
    ensure_ascii=False,
))
PY
