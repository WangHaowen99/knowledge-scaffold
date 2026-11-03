#!/usr/bin/env bash
# Post-commit message dispatcher.
# Invoked by the PostToolUse hook after a git commit Bash tool call.
# 给助手看的一两行：提交记进了这一轮的修改（这一轮结束合入后才是版本），以及产物核验状态。
# 不给版本号、不提「回退」、不推荐下一条命令：版本在每一轮结束合入时产生，下一步由建议条给出。

# --- Hook self-guard ---------------------------------------------------------
# PostToolUse hooks fire on every Bash; settings.json's `if` clause is
# ignored by the current hook engine. Gate by reading the tool input JSON
# from stdin and matching `git commit`. Also bail when the commit itself
# failed (pre-commit hook block / conflict / empty commit) — otherwise we
# misreport a save using the unchanged HEAD. TTY invocations bypass.
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

MSG=$(echo "$last" | sed 's/^[0-9a-f]* //')

print_header() {
    echo "已记进这一轮的修改：$MSG（这一轮结束平台自动存成新版本；不要对用户说版本号，也不要让用户回复「回退」「撤回」）"
}

verified_hint() {
    # 保存成功与产物核验分开；只读当前回执，不重复调用检查器或模型。
    if (cd "$ROOT" && python3 .claude/scripts/verify_artifacts.py "$1" --status >/dev/null 2>&1); then
        echo "💡 产物轻量核验通过（业务覆盖与语义检查仍按原流程执行）"
    else
        echo "⚠️ 版本已保存，完整阶段产物仍待核验；不要据此宣布提取完成。运行 python3 .claude/scripts/verify_artifacts.py $1"
    fi
}

case "$MSG" in
    research:*)
        print_header
        # 提交成功不代表内容已验收（回执可能因源资料改动而失效），按实际阶段提示
        STAGE=$(cd "$ROOT" && python3 .claude/scripts/state_helper.py detect-stage 2>/dev/null | tr -d '[:space:]')
        if [ "$STAGE" = "资料分析待核验" ]; then
            echo "⚠️ 报告已保存但未通过内容验收，运行 python3 .claude/scripts/research_checker.py 查看错误项"
        fi
        ;;
    flowchart:*)
        print_header
        verified_hint diagrams
        ;;
    extract:*objects*)
        print_header
        verified_hint objects
        ;;
    extract:*operation*)
        print_header
        verified_hint operations
        ;;
    trace:*testcases*|fix:*|mock:*|codegen:*)
        print_header
        ;;
esac
