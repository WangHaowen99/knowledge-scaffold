#!/usr/bin/env bash
# PreToolUse hook — keep knowledge-package writes inside the packaged layout.
#
# 两层判定：
#   1) 任何时候：ontology/ 下自创目录（白名单之外）一律拦截；
#   2) 评测演进期（存在 .ontology-state/lock-evolution.json）：写入必须落在
#      会进入知识包的目录（object/operation/logic/rule/metric/scripts，另放行
#      testcase/data-mock），否则内容送不到 consumer，演进做的是无用功。

set -u

HOOK_INPUT=$(cat 2>/dev/null || true)
[ -z "$HOOK_INPUT" ] && exit 0

HOOK_INPUT="$HOOK_INPUT" python3 - <<'PY'
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:
    data = json.loads(os.environ.get("HOOK_INPUT", "") or "{}")
except Exception:
    sys.exit(0)

try:
    project_root = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()).resolve()
except Exception:
    project_root = Path.cwd().resolve()

try:
    cwd = Path.cwd().resolve()
except Exception:
    cwd = project_root

tool_input = data.get("tool_input") or {}


def resolve_candidate(path_str: str) -> Path | None:
    try:
        cleaned = path_str.strip().strip("'\"")
        if not cleaned or cleaned.startswith(("$", "`", "<")):
            return None
        p = Path(cleaned)
        if p.is_absolute():
            return p.resolve()
        return (cwd / p).resolve()
    except Exception:
        return None


def under_project(path: Path) -> bool:
    try:
        path.relative_to(project_root)
        return True
    except ValueError:
        return False


# 对齐 ontology_checker 的 ALLOWED_MD_SUBDIRS + 非 md 工具链目录。
ALLOWED_ONTOLOGY_DIRS = {
    "object", "operation", "logic", "rule", "metric", "action",
    "data-mock", "testcase", "scripts", "knowledge",
    "diagrams", "diagrams-summary", "mermaid", "excel",
}

# 知识包导出范围（与 backend export_package_zip 一致）——只有这些目录送达 consumer。
PACKAGED_ONTOLOGY_DIRS = {"object", "operation", "logic", "rule", "metric", "scripts"}

# 评测演进期额外放行：D 类改用例、C 类对账素材。
EVOLUTION_EXTRA_DIRS = {"testcase", "data-mock"}

EVOLUTION_LOCK = project_root / ".ontology-state" / "lock-evolution.json"
EVOLUTION_ACTIVE = EVOLUTION_LOCK.exists()


def classify(path: Path) -> str | None:
    """返回 None（放行）/ "unknown"（自创目录）/ "unpackaged"（演进期越出打包范围）。"""
    if not under_project(path):
        return None
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        return None
    if len(parts) < 3 or parts[0] != "ontology":
        return None  # ontology 根文件（如 index.md）不拦
    sub = parts[1]
    if sub not in ALLOWED_ONTOLOGY_DIRS:
        return "unknown"
    if EVOLUTION_ACTIVE and sub not in (PACKAGED_ONTOLOGY_DIRS | EVOLUTION_EXTRA_DIRS):
        return "unpackaged"
    return None


def split_shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return []


def extract_bash_write_targets(command: str) -> list[str]:
    """Best-effort extraction for common shell write targets."""
    out: list[str] = []

    # Redirections: > path, >> path, 2> path, &> path.
    for m in re.finditer(r"(?:[12&]?>>?)\s*([^\s|&;<>]+)", command):
        out.append(m.group(1))

    tokens = split_shell_tokens(command)
    if not tokens:
        return [p for p in out if not p.startswith("/dev/")]

    separators = {"|", "||", "&", "&&", ";"}
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in separators:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)

    write_all_args = {"rm", "rmdir", "touch", "mkdir", "chmod", "chown", "chgrp"}
    write_last_arg = {"cp", "mv", "install", "rsync"}

    for segment in segments:
        if not segment:
            continue
        cmd = Path(segment[0]).name
        args = [a for a in segment[1:] if a not in {"<", ">", ">>"}]

        if cmd == "tee":
            out.extend(a for a in args if not a.startswith("-"))
            continue

        if cmd in write_last_arg:
            real_args = [a for a in args if not a.startswith("-")]
            if real_args:
                out.append(real_args[-1])
            continue

        if cmd in write_all_args:
            out.extend(a for a in args if not a.startswith("-"))
            continue

        if cmd in {"sed", "gsed", "perl"} and any(
            a == "-i" or a.startswith("-i") or a.startswith("-pi") for a in args
        ):
            out.extend(a for a in args if not a.startswith("-"))

    cleaned: list[str] = []
    for p in out:
        p = p.strip().strip("'\"")
        if not p or p.startswith("/dev/"):
            continue
        if p.startswith(("$", "`", "<")):
            continue
        cleaned.append(p)
    return cleaned


raw_candidates: list[str] = []

file_path = tool_input.get("file_path") or ""
if file_path:
    raw_candidates.append(file_path)

command = tool_input.get("command") or ""
if command:
    raw_candidates.extend(extract_bash_write_targets(command))

violations: list[tuple[str, str, str]] = []
for raw in raw_candidates:
    resolved = resolve_candidate(raw)
    if resolved is None:
        continue
    kind = classify(resolved)
    if kind:
        violations.append((raw, str(resolved), kind))

if not violations:
    sys.exit(0)

seen = set()
unique: list[tuple[str, str, str]] = []
for raw, resolved, kind in violations:
    if resolved in seen:
        continue
    seen.add(resolved)
    unique.append((raw, resolved, kind))

reasons = "; ".join(f"{raw} -> {resolved}" for raw, resolved, _ in unique)
packaged_hint = (
    "只有 ontology/{object,operation,rule,metric,scripts} 会进入知识包（consumer 可见）；"
    "案例/方案类知识请建模为 ontology/rule/ 下的场景规则（触发特征 → 方案要点），"
    "并 wikilink 关联涉事对象。"
)
if any(kind == "unpackaged" for _, _, kind in unique):
    reason = (
        f"评测演进进行中，知识修订只能落在会打包的目录，此次写入越界：{reasons}。"
        f"{packaged_hint}写进不打包的目录（如 ontology/knowledge/）对评测分数不会有任何影响。"
    )
else:
    reason = (
        f"ontology/ 下不允许自创目录：{reasons}。"
        f"允许的子目录以 ontology_checker 白名单为准；{packaged_hint}"
    )

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
