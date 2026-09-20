#!/usr/bin/env bash
# PreToolUse hook — block agent writes to any directory named resource.
#
# `resource/` contains user-maintained source material. Agents may read it and
# may refresh derived ontology files from it, but must not create, modify, move,
# or delete files under a resource directory.

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


def is_resource_path(path: Path) -> bool:
    return under_project(path) and "resource" in path.parts


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

violations: list[tuple[str, str]] = []
for raw in raw_candidates:
    resolved = resolve_candidate(raw)
    if resolved is not None and is_resource_path(resolved):
        violations.append((raw, str(resolved)))

if not violations:
    sys.exit(0)

seen = set()
unique: list[tuple[str, str]] = []
for raw, resolved in violations:
    if resolved in seen:
        continue
    seen.add(resolved)
    unique.append((raw, resolved))

reasons = "; ".join(f"{raw} -> {resolved}" for raw, resolved in unique)
print(json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"禁止模型修改 resource 目录下的内容：{reasons}。"
                "resource 是用户维护的源材料，只允许读取；如需变更源材料，请由用户手工修改，"
                "模型只能基于 resource 刷新或生成派生产物。"
            ),
        }
    },
    ensure_ascii=False,
))
PY
