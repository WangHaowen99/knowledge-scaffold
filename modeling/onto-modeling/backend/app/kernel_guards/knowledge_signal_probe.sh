#!/usr/bin/env bash
# UserPromptSubmit hook — surface business-knowledge trigger words.
#
# Scans the incoming user prompt for the signal words listed in
# .claude/BUSINESS_KNOWLEDGE_SNIPPET.md. When one or more match, injects an
# additionalContext reminder so Claude evaluates whether the statement should
# be sunk as business knowledge instead of relying purely on LLM vigilance.
#
# Stays silent (exit 0, no JSON) when no signal words are present so normal
# prompts incur zero overhead.

set -u

# 纠错与惯例的沉淀属于知识沉淀包（详设 5.3）：项目的装配目录里没有它的守则片段，说明没启用，不提示
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
if [ -e "$PROJECT_DIR/.claude" ] && [ ! -f "$PROJECT_DIR/.claude/KNOWLEDGE_SEDIMENTATION.md" ]; then
  exit 0
fi

HOOK_INPUT=$(cat 2>/dev/null || true)
[ -z "$HOOK_INPUT" ] && exit 0

HOOK_INPUT="$HOOK_INPUT" python3 - <<'PY'
import json
import os
import sys

try:
    data = json.loads(os.environ.get('HOOK_INPUT', '') or "{}")
except Exception:
    sys.exit(0)

prompt = (data.get('prompt') or '').strip()
if not prompt:
    sys.exit(0)

# Mirror .claude/BUSINESS_KNOWLEDGE_SNIPPET.md (纠错信号 + 业务常识/规范信号).
correction = ['不对', '你弄错了', '应该是', '其实', '实际上']
convention = ['我们公司', '我们这边', '规则是', '通常', '必须', '行业里', '标准做法']

hit_correction = [w for w in correction if w in prompt]
hit_convention = [w for w in convention if w in prompt]
if not hit_correction and not hit_convention:
    sys.exit(0)

parts = []
if hit_correction:
    parts.append(f"纠错信号：{ '、'.join(hit_correction) }")
if hit_convention:
    parts.append(f"业务常识/规范信号：{ '、'.join(hit_convention) }")

context = (
    "【业务知识信号词探针】检测到 — " + "；".join(parts) + "。\n"
    "请对照 .claude/BUSINESS_KNOWLEDGE_SNIPPET.md 评估是否需要沉淀为业务知识："
    "若是，按 .claude/INTERACTION_CONVENTIONS.md 的「业务知识确认类」模板向用户确认；"
    "若用户已用「记一下：…」「记下这条规则」等明确指令，则直接调用 knowledge_helper.py add 落盘。"
)

print(json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    },
    ensure_ascii=False,
))
PY
