#!/usr/bin/env bash
# Post-commit version message dispatcher.
# Invoked by the PostToolUse hook after a git commit Bash tool call.
# Prints a Chinese version summary + next-step hint based on commit prefix.

# --- Hook self-guard ---------------------------------------------------------
# PostToolUse hooks fire on every Bash; settings.json's `if` clause is
# ignored by the current hook engine. Gate by reading the tool input JSON
# from stdin and matching `git commit`. Also bail when the commit itself
# failed (pre-commit hook block / conflict / empty commit) — otherwise we
# misreport "已保存版本 N" using the unchanged HEAD. TTY invocations bypass.
if [ ! -t 0 ]; then
    HOOK_INPUT=$(cat 2>/dev/null || true)
    PARSED=$(printf '%s' "${HOOK_INPUT:-}" | python3 -c '
import json, sys
try:
    data = json.loads(sys.stdin.read() or "{}")
    cmd  = data.get("tool_input", {}).get("command", "")
    code = data.get("tool_response", {}).get("exitCode", "")
    print(f"{code}\t{cmd}")
except Exception:
    pass
' 2>/dev/null || true)
    EXIT_CODE=$(printf '%s' "${PARSED:-}" | cut -f1)
    BASH_CMD=$(printf '%s' "${PARSED:-}" | cut -f2-)
    if ! printf '%s' "${BASH_CMD:-}" | grep -qE '(^|[;&|]|[[:space:]])git[[:space:]]+commit($|[[:space:]])'; then
        exit 0
    fi
    if [ -n "${EXIT_CODE:-}" ] && [ "${EXIT_CODE}" != "0" ]; then
        exit 0
    fi
fi
# -----------------------------------------------------------------------------

set -u

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

last=$(git -C "$ROOT" log --oneline -1 2>/dev/null) || exit 0
[ -z "$last" ] && exit 0

N=$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null)
MSG=$(echo "$last" | sed 's/^[0-9a-f]* //')

print_header() {
    echo "已保存版本 $N: $MSG"
    echo "如需回退到上一版本，回复\"回退\""
}

case "$MSG" in
    research:*)
        print_header
        echo "💡 调研完成，建议运行 /ontology-flowchart 创建或完善流程图"
        ;;
    flowchart:*)
        print_header
        echo "💡 流程图就绪，建议运行 /ontology-extract-object 开始提取对象"
        ;;
    extract:*objects*)
        print_header
        echo "💡 对象提取完成，建议运行 /ontology-extract-operation 继续提取逻辑"
        ;;
    extract:*operation*)
        print_header
        echo "💡 逻辑提取完成，建议运行 /ontology-validate 开始校验"
        ;;
    trace:*testcases*)
        print_header
        echo "💡 追踪场景已保存，下次追踪同一逻辑会自动复用"
        ;;
    fix:*trace*)
        print_header
        echo "💡 追踪修复完成，建议回到 /ontology-trace 继续走查其他场景或运行 /ontology-codegen 生成代码"
        ;;
    fix:*rule*|fix:*semantic*|fix:*校验*|fix:*validate*)
        print_header
        echo "💡 校验修复完成，建议运行 /ontology-mock 生成测试数据"
        ;;
    fix:*)
        print_header
        echo "💡 修复已保存，建议运行 /ontology-validate 确认无残留问题"
        ;;
    mock:*)
        print_header
        echo "💡 测试数据就绪，建议运行 /ontology-trace 进行场景追踪"
        ;;
    codegen:*)
        print_header
        echo "💡 代码生成完成"
        ;;
esac
