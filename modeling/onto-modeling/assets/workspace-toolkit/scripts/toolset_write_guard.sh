#!/usr/bin/env bash
# PreToolUse hook — 阻止任务工作区 (workspace/<task>/) 通过 symlink 或相对路径
# 跨边界写入工具集仓 (CLAUDE.md / README.md / .claude/ 及未来任何同类入口)。
#
# 触发场景：
#   - Edit / Write / MultiEdit 的目标 file_path 解析后落在 toolset 仓内但逃出当前 cwd
#   - Bash 命令里的写重定向 (`>`/`>>`)、`tee`、`cp`、`mv` 目的路径同上
#
# 通过/拒绝判据：
#   resolved_target ∈ <toolset> 且 resolved_target ∉ <cwd>
#   ↳ 等价于 "通过 symlink 或 ../.. 翻出 cwd 进入了工具集"
#
# 工具集仓内开发（cwd 的 git_root 等于工具集根）不受影响。

set -u

HOOK_INPUT=$(cat 2>/dev/null || true)
[ -z "$HOOK_INPUT" ] && exit 0

# 通过脚本自身真实路径反推工具集根：本脚本始终活在
# <toolset_root>/.claude/scripts/toolset_write_guard.sh
SCRIPT_REALPATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0")"
TOOLSET_ROOT="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.dirname(os.path.dirname(sys.argv[1]))))' "$SCRIPT_REALPATH")"

HOOK_INPUT="$HOOK_INPUT" TOOLSET_ROOT="$TOOLSET_ROOT" python3 - <<'PY'
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    data = json.loads(os.environ.get('HOOK_INPUT', '') or "{}")
except Exception:
    sys.exit(0)

toolset_root = os.environ.get('TOOLSET_ROOT', '')
if not toolset_root:
    sys.exit(0)

try:
    toolset_resolved = Path(toolset_root).resolve()
except Exception:
    sys.exit(0)

# 工具集仓自身开发：cwd 的 git_root 等于工具集根 → 全部放行
try:
    git_root = subprocess.check_output(
        ['git', 'rev-parse', '--show-toplevel'],
        stderr=subprocess.DEVNULL, text=True,
    ).strip()
except Exception:
    git_root = ''

if git_root:
    try:
        if Path(git_root).resolve() == toolset_resolved:
            sys.exit(0)
    except Exception:
        pass

try:
    cwd = Path.cwd().resolve()
except Exception:
    sys.exit(0)

tool_input = data.get('tool_input') or {}


def extract_bash_write_targets(command: str) -> list:
    """Best-effort: pull write-destination paths out of a shell command.

    Covers redirections, tee, cp, mv. Misses sed -i / python -c open() on purpose
    — they are rare in agent transcripts and hard to parse without false positives.
    """
    out = []
    # > path / >> path / 2> path / &> path — exclude /dev/, &1, &2
    for m in re.finditer(r'(?:[12&]?>>?)\s*([^\s|&;<>]+)', command):
        out.append(m.group(1))
    # tee [-flags] path
    for m in re.finditer(r'\btee\b(?:\s+-\S+)*\s+([^\s|&;<>]+)', command):
        out.append(m.group(1))
    # cp / mv: last bare arg is dst (single-pair form only)
    for m in re.finditer(r'\b(?:cp|mv)\b(?:\s+-\S+)*\s+(\S+)\s+(\S+)', command):
        out.append(m.group(2))

    cleaned = []
    for p in out:
        p = p.strip().strip("'\"")
        if not p:
            continue
        if p.startswith('/dev/'):
            continue
        if p.startswith(('$', '`', '<')):  # variable / cmd subst / heredoc marker
            continue
        cleaned.append(p)
    return cleaned


def resolve_candidate(path_str: str) -> Path | None:
    try:
        p = Path(path_str)
        if p.is_absolute():
            return p.resolve()
        return (cwd / p).resolve()
    except Exception:
        return None


def _under(target: Path, parent: Path) -> bool:
    try:
        target.relative_to(parent)
        return True
    except ValueError:
        return False


# Gather candidate destination paths
raw_candidates = []
file_path = tool_input.get('file_path') or ''
if file_path:
    raw_candidates.append(file_path)

command = tool_input.get('command') or ''
if command:
    raw_candidates.extend(extract_bash_write_targets(command))

if not raw_candidates:
    sys.exit(0)

violations = []
for raw in raw_candidates:
    resolved = resolve_candidate(raw)
    if resolved is None:
        continue
    if _under(resolved, toolset_resolved) and not _under(resolved, cwd):
        violations.append((raw, str(resolved)))

if not violations:
    sys.exit(0)

# De-dup while preserving order
seen = set()
unique = []
for raw, res in violations:
    if res in seen:
        continue
    seen.add(res)
    unique.append((raw, res))

reasons = '; '.join(f'{raw} → {res}' for raw, res in unique)

print(json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"工具箱目录只读，禁止写入：{reasons}。"
                "正确去处：一次性辅助脚本写到工作区 tmp/ 目录（用完删除）；"
                "正式代码产物走 codegen 流程进 ontology/scripts/。"
                "若发现工具箱脚本/模板本身有 bug，请把现象反馈给维护者，"
                "不要在任务工作区里现场 patch（资产是共享源，改动会影响全部项目）。"
            ),
        }
    },
    ensure_ascii=False,
))
PY
