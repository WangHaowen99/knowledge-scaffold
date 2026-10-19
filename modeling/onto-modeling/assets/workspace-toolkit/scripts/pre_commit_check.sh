#!/usr/bin/env bash
# Pre-commit rule check — runs ontology_checker on staged ontology/ files.
# Blocks the commit (decision=block) only on ERROR severity; warnings and
# suggestions are surfaced via stderr so the user still sees them.

# --- Hook self-guard ---------------------------------------------------------
# Claude Code's PreToolUse hook for Bash fires on EVERY Bash tool call;
# settings.json's `if` field is silently ignored by the current hook engine,
# so we gate here by reading the tool input JSON from stdin and matching
# `git commit`. Manual TTY invocations bypass this gate (so devs can still
# run the script by hand for debugging).
if [ ! -t 0 ]; then
    HOOK_INPUT=$(cat 2>/dev/null || true)
    BASH_CMD=$(printf '%s' "${HOOK_INPUT:-}" | python3 -c '
import json, sys
try:
    data = json.loads(sys.stdin.read() or "{}")
    print(data.get("tool_input", {}).get("command", ""))
except Exception:
    pass
' 2>/dev/null || true)
    if ! printf '%s' "${BASH_CMD:-}" | grep -qE '(^|[;&|]|[[:space:]])git[[:space:]]+commit($|[[:space:]])'; then
        exit 0
    fi
fi
# -----------------------------------------------------------------------------

set -u
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$ROOT" || exit 0

# --- Resource guard ----------------------------------------------------------
# resource/ is user-managed source material. Agent commits must never include
# created, modified, moved, or deleted files under any resource directory.
RESOURCE_STAGED=$(git diff --cached --name-only -z 2>/dev/null | tr '\0' '\n' \
    | grep -E '(^|/)resource(/|$)' || true)
if [ -n "$RESOURCE_STAGED" ]; then
    REASON=$(printf 'resource/ 是用户维护的源材料，模型禁止提交对任意 resource 目录的改动：\n%s\n\n请取消暂存这些文件；若源材料需要调整，请由用户手工修改。' "$RESOURCE_STAGED")
    python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
    exit 0
fi

# --- Toolchain check：lint .claude/scripts/*.py and *.sh ----------------------
# 这部分检查的对象是工具自身（hooks、state_helper、checker 等）。任何工具脚本
# 在提交时存在语法错误都直接 block — 工具坏了下一次 commit 会连 hook 都跑不起来。
STAGED_TOOLS=$(git diff --cached --name-only -z 2>/dev/null | tr '\0' '\n' \
    | grep -E '^\.claude/(scripts|agents|commands)/' || true)

# Generated executable scripts are part of the package contract, not ordinary docs.
STAGED_SCRIPTS=$(git diff --cached --name-only -z 2>/dev/null | tr '\0' '\n' \
    | grep -E '^ontology/scripts/.*\.py$' || true)

TOOL_ERRORS=""
if [ -n "$STAGED_TOOLS" ]; then
    while IFS= read -r path; do
        [ -z "$path" ] && continue
        [ -f "$path" ] || continue
        case "$path" in
            *.py)
                if ! python3 -m py_compile "$path" 2>/dev/null; then
                    err=$(python3 -m py_compile "$path" 2>&1 | head -3)
                    TOOL_ERRORS="${TOOL_ERRORS}${path}: Python 语法错误 — ${err}\n"
                fi
                ;;
            *.sh)
                if ! bash -n "$path" 2>/dev/null; then
                    err=$(bash -n "$path" 2>&1 | head -3)
                    TOOL_ERRORS="${TOOL_ERRORS}${path}: Shell 语法错误 — ${err}\n"
                fi
                ;;
        esac
    done <<< "$STAGED_TOOLS"
fi

if [ -n "$TOOL_ERRORS" ]; then
    # Tool scripts have syntax errors — block immediately, ignoring the rest.
    REASON=$(printf '工具脚本存在语法错误，已阻止提交（继续提交会让后续 hook 失效）：\n%b\n请修复后重新 git add 并提交。' "$TOOL_ERRORS")
    python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
    exit 0
fi

if [ -n "$STAGED_SCRIPTS" ]; then
    SCRIPT_OUTPUT=$(python3 .claude/scripts/ontology_checker.py ontology --json --include-scripts 2>/dev/null || echo '[]')
    SCRIPT_ERRORS=$(printf '%s' "$SCRIPT_OUTPUT" | python3 -c '
import json, sys
try:
    print(sum(1 for item in json.loads(sys.stdin.read() or "[]") if item.get("severity") == "错误" and item.get("category") == "脚本契约"))
except Exception:
    print(1)
' 2>/dev/null || echo 1)
    if [ "$SCRIPT_ERRORS" -gt 0 ]; then
        REASON='ontology/scripts 中存在脚本契约错误：请运行 python3 .claude/scripts/ontology_checker.py ontology --include-scripts 修复后再提交。'
        python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
        exit 0
    fi
fi

# --- Script run verification -------------------------------------------------
# 新增或修改的脚本必须用 script_verify.py 跑通（jiuwenbox 沙箱优先，不可用时在容器内），
# 有与当前源码一致的通过记录才能提交。
VERIFY_TARGETS=$(git diff --cached --name-only --diff-filter=ACMR -z 2>/dev/null | tr '\0' '\n' \
    | grep -E '^ontology/scripts/[^/]+\.py$' || true)
if [ -n "$VERIFY_TARGETS" ]; then
    mapfile -t VERIFY_FILES <<< "$VERIFY_TARGETS"
    if ! VERIFY_OUTPUT=$(python3 .claude/scripts/script_verify.py check "${VERIFY_FILES[@]}" 2>&1); then
        REASON=$(printf '以下脚本还没有通过当前版本的运行验证，已阻止提交：\n%s\n\n按 /ontology-codegen Step 4.6 编写用例，运行 python3 .claude/scripts/script_verify.py run <脚本> --cases <用例文件>，全部通过后再提交。' "$VERIFY_OUTPUT")
        python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
        exit 0
    fi
fi

# --- Research report check ---------------------------------------------------
# 调研报告的内容验收。"报告已保存"不等于内容已通过：只要暂存了 research/ 下的
# 报告，就必须过 research_checker 的确定性检查（计数、ID、字段、来源），
# 否则错误报告会被下游的对象提取直接当成事实来源。
RESEARCH_STAGED=$(git diff --cached --name-only -z 2>/dev/null | tr '\0' '\n' \
    | grep -E '^research/.*\.md$' | grep -v '/index\.md$' || true)
if [ -n "$RESEARCH_STAGED" ]; then
    RESEARCH_OUTPUT=$(python3 .claude/scripts/research_checker.py check 2>&1)
    RESEARCH_CODE=$?
    RESEARCH_OUTPUT=$(printf '%s' "$RESEARCH_OUTPUT" | head -40)
    # 1=有确定性错误或人工待决未完成 → 阻止；2=已无报告可核验（如删除报告）→ 放过
    if [ "$RESEARCH_CODE" -eq 1 ]; then
        REASON=$(printf '调研报告未通过内容验收，已阻止提交：\n\n%s\n\n修正报告后重新 git add 并提交；需人工判断的条目请先确认再在报告中勾选。' "$RESEARCH_OUTPUT")
        python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
        exit 0
    fi
fi

# --- Ontology rule check（原逻辑） -------------------------------------------
# Only relevant when staged files include ontology docs. Use -z for NUL-safe parsing.
# rule / metric 有意不在范围内：从知识包导入的规则、指标是知识包契约的格式（规则内容 /
# 判断条件 / 知识证据 …），与工具箱模板不同，结构检查会把它们整篇判为缺章节。两套
# 格式对齐之前在这里拦，导入项目里任何改到规则、指标的提交都会被挡住。
STAGED=$(git diff --cached --name-only -z 2>/dev/null | tr '\0' '\n' \
    | grep -E '^ontology/(object|operation|action|data-mock|knowledge)/.*\.md$' || true)
if [ -z "$STAGED" ]; then
    exit 0
fi

# Collect unique stems, filtering out anything that would confuse argparse
# (stems starting with `-`). CLAUDE.md rule 3 keeps legitimate filenames
# alphanumeric/Chinese, so a leading `-` indicates a malformed filename.
declare -a STEMS=()
declare -a SKIPPED=()
# Staged ontology docs may live in nested object/operation folders; ontology_checker
# accepts bare stems, so strip directories before passing --files.
while IFS= read -r staged_path; do
    stem=$(basename "$staged_path" .md)
    [ -n "$stem" ] || continue
    case "$stem" in
        -*) SKIPPED+=("$stem"); continue ;;
    esac
    already=0
    if [ "${#STEMS[@]}" -gt 0 ]; then
        for existing in "${STEMS[@]}"; do
            if [ "$existing" = "$stem" ]; then already=1; break; fi
        done
    fi
    [ "$already" -eq 0 ] && STEMS+=("$stem")
done < <(echo "$STAGED")

if [ "${#SKIPPED[@]}" -gt 0 ]; then
    echo "⚠️ 跳过异常文件名（以 - 开头）：${SKIPPED[*]}" >&2
fi

if [ "${#STEMS[@]}" -eq 0 ]; then
    exit 0
fi

# ontology_checker emits progress to stderr; stdout is pure JSON.
OUTPUT=$(python3 .claude/scripts/ontology_checker.py ontology --json --include-mock --files "${STEMS[@]}" 2>/dev/null || echo '[]')

COUNTS=$(echo "$OUTPUT" | python3 -c '
import json, sys
try:
    issues = json.loads(sys.stdin.read() or "[]")
except Exception:
    issues = []
def c(s): return sum(1 for i in issues if i.get("severity") == s)
print(c("错误"), c("警告"), c("建议"))
' 2>/dev/null || echo '0 0 0')

ERRORS=$(echo "$COUNTS" | awk '{print $1+0}')
WARNINGS=$(echo "$COUNTS" | awk '{print $2+0}')
SUGGESTIONS=$(echo "$COUNTS" | awk '{print $3+0}')

if [ "$WARNINGS" -gt 0 ] || [ "$SUGGESTIONS" -gt 0 ]; then
    echo "⚠️ 待提交文档含 ${WARNINGS} 条警告 / ${SUGGESTIONS} 条建议（不阻止提交）。运行 /ontology-validate 查看详情。" >&2
fi

if [ "$ERRORS" -gt 0 ]; then
    FILES_LIST=$(printf '%s ' "${STEMS[@]}")
    REASON=$(printf '待提交的文档含 %d 个规则错误，已阻止提交。\n\n排查方式（任选其一）：\n1. 在 Claude 对话中运行 /ontology-validate 走完整修复流程；\n2. 直接跑 python3 .claude/scripts/ontology_checker.py ontology --include-mock --files %s 看具体问题；\n3. /ontology-impact <文件> 查看该文件的下游影响面。\n\n修复后 git add + commit 即可。' "${ERRORS}" "${FILES_LIST}")
    python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
fi

# Knowledge files have a different schema; run a dedicated check.
KNOWLEDGE_STAGED=$(echo "$STAGED" | grep -E '^ontology/knowledge/' || true)
if [ -n "$KNOWLEDGE_STAGED" ]; then
    K_OUTPUT=$(python3 .claude/scripts/ontology_checker.py ontology --json --include-knowledge 2>/dev/null || echo '[]')
    K_COUNTS=$(echo "$K_OUTPUT" | python3 -c '
import json, sys
try:
    issues = [i for i in json.loads(sys.stdin.read() or "[]") if i.get("category","").startswith("知识")]
except Exception:
    issues = []
def c(s): return sum(1 for i in issues if i.get("severity") == s)
print(c("错误"), c("警告"))
' 2>/dev/null || echo '0 0')
    K_ERRORS=$(echo "$K_COUNTS" | awk '{print $1+0}')
    K_WARNINGS=$(echo "$K_COUNTS" | awk '{print $2+0}')
    if [ "$K_WARNINGS" -gt 0 ]; then
        echo "⚠️ 业务知识：${K_WARNINGS} 条警告（不阻止提交）。" >&2
    fi
    if [ "$K_ERRORS" -gt 0 ]; then
        REASON="待提交的业务知识含 ${K_ERRORS} 个错误。运行 python3 .claude/scripts/ontology_checker.py ontology --include-knowledge 查看详情。"
        python3 -c '
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}, ensure_ascii=False))
' "$REASON"
    fi
fi

exit 0
