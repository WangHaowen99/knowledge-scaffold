#!/usr/bin/env python3
"""内核守卫（详设 4.8；方案 D8～D10）：PreToolUse 写保护的共用实现，只用标准库。

由内核注入每个项目的装配目录（`.claude/scripts/`），包删不掉、改不了，只能在清单里追加
拒绝型守卫。四个 *_guard.sh 都是一行包装：`python3 _guard_common.py <守卫名>`。

协议同 Claude Code 的 PreToolUse hook：stdin 收 `{"tool_name", "tool_input", "cwd"}` JSON，
拒绝时 stdout 打印 permissionDecision=deny，放行时什么都不打印。

出错即拒（D9）：输入为空、不是 JSON、守卫自身抛异常，一律输出 deny——Claude CLI 把 hook
脚本的异常当成非阻断，守卫只能自己兜底。

Bash 命令的写入目标按常见写法解析（D10），补上实测能绕过的几类：
- `cd X && ...`、`pushd X`：后续相对路径按切换后的目录解析；
- `python/perl/node/ruby -c|-e` 与 heredoc 内联代码：代码里出现受保护路径且有写操作就拒；
- `find ... -delete|-exec`、`git checkout/restore/rm/clean/mv` 带路径（含 `.` 这类覆盖整个
  受保护目录的祖先路径）；
- 路径里带变量：同一条命令里赋过值的先代入，代不出来且命令里出现受保护目录名就拒；
- `bash -c` / `sh -c` / `eval` 里的命令串递归解析。
这些只防误改和常见写法，防不住有意绕过；真正改不了要靠进程隔离（受保护目录第 4 层）。

范围限定（ADR-0085）：组装结果（.claude/flow.yaml）带 `confine` 时，范围守卫另外挡两类写：
- 主线上的范围文件 status 是 active：基线提交里有、editable 里没有的本体条目不能写也不能删，新增条目不限；
- 范围文件本身只能在它所属节点的命令运行时写。
放宽只认已经合入主线的版本（读主线 HEAD）；副本里确认的范围只收紧，和主线的取并集。读不出、不是 active 就不收窄。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SEPARATORS = {"|", "||", "&", "&&", ";", ";;", "(", ")", "|&", "\n"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
WRITE_ALL_ARGS = {"rm", "rmdir", "touch", "mkdir", "chmod", "chown", "chgrp", "truncate", "shred", "unlink"}
WRITE_LAST_ARG = {"cp", "install", "rsync", "ln"}
INTERPRETERS = {"python", "python3", "perl", "node", "ruby", "php", "deno", "bun"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
PREFIX_COMMANDS = {"sudo", "env", "command", "nohup", "time", "nice", "exec", "builtin", "xargs", "timeout", "stdbuf"}
GIT_PATH_SUBCOMMANDS = {"checkout", "restore", "rm", "clean", "mv"}
# 改了就能让 resource/ 在助手副本里「被删」的 git 写法（ADR-0046 审查修复）
GIT_INTERNAL_SUBCOMMANDS = {"sparse-checkout", "worktree"}
GIT_INTERNAL_FLAGS = ("--skip-worktree", "--no-skip-worktree", "--assume-unchanged", "--no-assume-unchanged")
GIT_INTERNAL_CONFIG = ("sparse", "excludesfile", "worktreeconfig", "hookspath")
# 内联代码里出现这些调用才算写（读资料的 python -c 很常见，不能一律拒）
_CODE_WRITE_RE = re.compile(
    r"open\([^)]*['\"][wax+][bt+]?['\"]|open\([^)]*['\"]\+?>|\.write\(|write_text|write_bytes|unlink|"
    r"os\.remove|rmtree|rmdir|os\.rename|\.rename\(|os\.replace|shutil\.|os\.system|subprocess|mkdir|"
    r"touch\(|chmod|chown|\.save\(|to_csv|to_excel|json\.dump\(|truncate|symlink|"
    r"unlinkSync|writeFileSync|appendFileSync|rmSync|renameSync|sysopen|File\.(?:write|delete|rename)",
)

# 本体目录白名单（与 ontology_checker 的 ALLOWED_MD_SUBDIRS + 非 md 工具链目录一致）
ALLOWED_ONTOLOGY_DIRS = {
    "object", "operation", "logic", "rule", "metric", "action",
    "data-mock", "testcase", "scripts", "knowledge",
    "diagrams", "diagrams-summary", "mermaid", "excel",
}
# 正在执行的命令与它们的写入范围（state_helper acquire 时写，方案 D11）
ACTIVE_REL = (".onto", "run", "active.json")
# 求解契约受管目录：只能经求解模型面板 / 后端接口写入
MANAGED_ONTOLOGY_DIRS = {"solver"}
# 范围限定管的本体条目（ADR-0085）：会进知识包或跟着条目走的目录，每个文件一个条目；index.md 不算
CONFINED_ONTOLOGY_DIRS = ("object", "operation", "logic", "rule", "metric", "action", "data-mock", "testcase", "scripts")
CONFINE_CACHE_REL = (".onto", "run", "confine-cache.json")
_ONTOLOGY_MENTION_RE = re.compile(r"(?<![\w.-])ontology(?:/[^\s'\"`;|&()<>,\]}]*)?")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))


@dataclass
class Target:
    raw: str
    path: Path | None  # 解析后的绝对路径；变量代不出来时为 None
    destructive: bool = False  # 递归删除、批量还原这类：覆盖受保护目录的祖先路径也算命中
    unresolved: bool = False


@dataclass
class Call:
    """一次工具调用：写入目标、内联代码与命令原文。"""

    root: Path
    cwd: Path
    tool: str
    targets: list[Target] = field(default_factory=list)
    code: list[str] = field(default_factory=list)
    command: str = ""
    # 动 git 内部设置的子命令（稀疏规则、跳过检出标记、worktree、钩子路径）：副本靠它们隔开资料区
    git_internal: list[str] = field(default_factory=list)
    # 打、删、推发布标签（release/*）：发布只在发布页做，陪伴建模、自主构建都不让助手打
    git_release: list[str] = field(default_factory=list)
    # `ln -s` 建的符号链接：(链接所在目录, 链接目标)；变量代不出来时为 None
    links: list[tuple[Path | None, str | None]] = field(default_factory=list)


# ---- 输入与项目根 ----


def _project_root(data: dict) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env:
        return Path(env).resolve()
    start = data.get("cwd") or os.getcwd()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(start).resolve()


def _resolve(base: Path, raw: str) -> Path:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = base / path
    return Path(os.path.realpath(path))


def _lexical(base: Path, raw: str) -> Path:
    """不跟随符号链接的绝对路径：认 `.claude/...`、`CLAUDE.md` 这类链接本身的位置。"""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = base / path
    return Path(os.path.normpath(path))


# ---- Bash 命令解析 ----


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\r", ""), posix=True, punctuation_chars="|&;<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        # 引号不配对：按空白粗切，宁可多查
        return re.split(r"\s+", command)


def _segments(tokens: list[str]) -> list[tuple[list[str], str]]:
    """按分隔符切成简单命令，带上它前面的分隔符（判断 cd 是否影响后续用）。"""
    out: list[tuple[list[str], str]] = []
    current: list[str] = []
    joiner = ";"
    for token in tokens:
        if token in SEPARATORS:
            if current:
                out.append((current, joiner))
            current, joiner = [], token
            continue
        current.append(token)
    if current:
        out.append((current, joiner))
    return out


_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _substitute(text: str, variables: dict[str, str]) -> str:
    return _VAR_RE.sub(lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), text)


def _has_variable(text: str) -> bool:
    return "$" in text or "`" in text


def _heredoc_bodies(command: str) -> list[str]:
    """heredoc 的正文（`<<EOF ... EOF`、`<<'EOF'`、`<<-EOF`）。"""
    bodies = []
    for match in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", command):
        tag = match.group(2)
        rest = command[match.end():]
        end = re.search(rf"^\s*{re.escape(tag)}\s*$", rest, re.M)
        bodies.append(rest[: end.start()] if end else rest)
    return bodies


_WRITE_COMMANDS = WRITE_ALL_ARGS | WRITE_LAST_ARG | {"mv", "sed", "gsed", "perl", "tee", "dd"} | SHELLS | INTERPRETERS


def _find_writes(args: list[str]) -> bool:
    """find 会不会改文件：-delete、-fprint*，或 -exec 跟的是写命令（-exec grep 这类读操作放行）。"""
    for index, arg in enumerate(args):
        if arg in ("-delete", "-fprint", "-fprintf", "-fls"):
            return True
        if arg in ("-exec", "-execdir", "-ok", "-okdir") and index + 1 < len(args):
            if Path(args[index + 1]).name in _WRITE_COMMANDS:
                return True
    return False


class _Parser:
    def __init__(self, call: Call):
        self.call = call
        self.variables: dict[str, str] = {}

    def add(self, cwd: Path, raw: str, destructive: bool = False) -> None:
        raw = raw.strip()
        if not raw or raw.startswith("/dev/") or raw in ("-", "&1", "&2") or raw.startswith("&"):
            return
        value = _substitute(raw, self.variables)
        if _has_variable(value):
            self.call.targets.append(Target(raw, None, destructive, unresolved=True))
            return
        self.call.targets.append(Target(raw, _lexical(cwd, value), destructive))

    def parse(self, command: str, cwd: Path, depth: int = 0) -> None:
        if depth > 3:
            return
        bodies = _heredoc_bodies(command)
        stripped = command
        for body in bodies:
            stripped = stripped.replace(body, "\n")
        tokens = _tokens(stripped)
        # 重定向：> 后面的词是目标（shlex 把 2> 切成 2 和 >）
        segments = _segments(tokens)
        previous: list[str] = []
        for segment, joiner in segments:
            if joiner == "|" and segment and Path(segment[0]).name == "xargs":
                self._xargs(segment, previous, cwd)
            cwd = self._segment(segment, cwd, bodies, depth)
            previous = segment

    def _xargs(self, segment: list[str], previous: list[str], cwd: Path) -> None:
        """`find X | xargs rm`：xargs 跑的是写命令时，前一段列出的路径都算破坏性目标。"""
        rest = [word for word in segment[1:] if not word.startswith("-")]
        if not rest or Path(rest[0]).name not in _WRITE_COMMANDS or not previous:
            return
        for arg in previous[1:]:
            if arg.startswith("-") or arg in ("!", "("):
                break
            self.add(cwd, arg, destructive=True)

    def _value(self, cwd: Path, raw: str) -> Path | None:
        value = _substitute(raw, self.variables)
        return None if _has_variable(value) else _lexical(cwd, value)

    def _symlinks(self, rest: list[str], cwd: Path) -> None:
        """`ln -s 目标 链接`、`ln -s 目标… 目录`、`ln -s -t 目录 目标…`、`ln -s 目标`（链接建在当前目录）。"""
        operands: list[str] = []
        directory, no_dir, index = None, False, 0
        while index < len(rest):
            arg = rest[index]
            if arg in ("-t", "--target-directory", "-S", "--suffix") and index + 1 < len(rest):
                if arg in ("-t", "--target-directory"):
                    directory = rest[index + 1]
                index += 2
                continue
            if arg.startswith("--target-directory="):
                directory = arg.split("=", 1)[1]
            elif arg in ("-T", "--no-target-directory"):
                no_dir = True
            elif arg == "--":
                operands += rest[index + 1:]
                break
            elif not arg.startswith("-"):
                operands.append(arg)
            index += 1
        if directory is not None:
            link_dir, targets = self._value(cwd, directory), operands
        elif len(operands) == 1:
            link_dir, targets = cwd, operands
        elif len(operands) >= 2:
            last = self._value(cwd, operands[-1])
            single = len(operands) == 2 and last is not None and (no_dir or not last.is_dir())
            link_dir, targets = (last.parent if single else last), operands[:-1]
        else:
            return
        for target in targets:
            value = _substitute(target, self.variables)
            known = link_dir is not None and not _has_variable(value)
            self.call.links.append((link_dir, os.path.expanduser(value) if known else None))

    def _segment(self, words: list[str], cwd: Path, bodies: list[str], depth: int) -> Path:
        args: list[str] = []
        index = 0
        while index < len(words):
            word = words[index]
            # `2>/dev/null`、`2>&1` 切出来是 2、>、…：紧挨重定向符的数字是文件描述符，不是路径
            if word.isdigit() and index + 1 < len(words) and (words[index + 1] in REDIRECTS
                                                              or words[index + 1] in ("<", ">&", "<&")):
                index += 1
                continue
            if word in (">&", "<&"):
                # `>&2`、`2>&1`、`>&-` 是复制或关闭描述符；`>& 文件` 才写文件
                if word == ">&" and index + 1 < len(words) and not words[index + 1].isdigit() \
                        and words[index + 1] != "-":
                    self.add(cwd, words[index + 1])
                index += 2
                continue
            if word in REDIRECTS or word == "<":
                if word != "<" and index + 1 < len(words):
                    self.add(cwd, words[index + 1])
                index += 2
                continue
            if word in ("<<", "<<-"):
                index += 2
                continue
            args.append(word)
            index += 1
        # 前置的变量赋值：记下来供后续代入（D=resource; touch $D/a.md）
        while args and _ASSIGN_RE.match(args[0]):
            name, value = _ASSIGN_RE.match(args[0]).groups()
            self.variables[name] = _substitute(value, self.variables)
            args = args[1:]
        while args and Path(args[0]).name in PREFIX_COMMANDS:
            args = args[1:]
            while args and (args[0].startswith("-") or _ASSIGN_RE.match(args[0])):
                args = args[1:]
        if not args:
            return cwd
        name = Path(args[0]).name
        rest = args[1:]
        plain = [arg for arg in rest if not arg.startswith("-")]
        recursive = any(re.fullmatch(r"-[a-zA-Z]*[rRf][a-zA-Z]*", arg) or arg in ("--recursive", "--force")
                        for arg in rest)

        if name == "ln" and any(arg == "--symbolic" or re.fullmatch(r"-[a-zA-Z]*s[a-zA-Z]*", arg) for arg in rest):
            self._symlinks(rest, cwd)
        if name in ("cd", "pushd"):
            if plain:
                target = _substitute(plain[0], self.variables)
                if _has_variable(target):
                    self.call.targets.append(Target(plain[0], None, unresolved=True))
                    return cwd
                return _lexical(cwd, target)
            return cwd
        if name in SHELLS or name == "eval":
            inner = None
            if name == "eval":
                inner = " ".join(rest)
            elif "-c" in rest and rest.index("-c") + 1 < len(rest):
                inner = rest[rest.index("-c") + 1]
            if inner:
                self.parse(inner, cwd, depth + 1)
            elif bodies:
                for body in bodies:
                    self.parse(body, cwd, depth + 1)
            return cwd
        if name in ("perl", "ruby") and any(re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z.]*", arg) for arg in rest):
            # perl -pi -e '...' 文件：原地改写，文件参数是写入目标
            for arg in plain:
                self.add(cwd, arg)
            return cwd
        if re.fullmatch(r"(python[0-9.]*|perl|node|ruby|php|deno|bun)", name):
            inline = any(re.fullmatch(r"-[a-zA-Z]*[ce]", arg) for arg in rest) or "-" in rest or not rest
            if inline:
                self.call.code.extend(rest)
                self.call.code.extend(bodies)
            return cwd
        if name == "tee":
            for arg in plain:
                self.add(cwd, arg)
        elif name in WRITE_ALL_ARGS:
            for arg in plain:
                self.add(cwd, arg, destructive=recursive or name in ("rmdir",))
        elif name in WRITE_LAST_ARG:
            if "-t" in rest and rest.index("-t") + 1 < len(rest):
                self.add(cwd, rest[rest.index("-t") + 1])
            elif plain:
                self.add(cwd, plain[-1])
        elif name == "mv":
            # 移走源文件等于删掉它：源与目标都算
            for arg in plain:
                self.add(cwd, arg, destructive=True)
        elif name in ("sed", "gsed", "perl") and any(arg == "-i" or arg.startswith(("-i", "-pi")) for arg in rest):
            for arg in plain:
                self.add(cwd, arg)
        elif name == "dd":
            for arg in rest:
                if arg.startswith("of="):
                    self.add(cwd, arg[3:])
        elif name in ("unzip",) and "-d" in rest and rest.index("-d") + 1 < len(rest):
            self.add(cwd, rest[rest.index("-d") + 1], destructive=True)
        elif name == "tar" and (rest and "x" in rest[0].lstrip("-") or "-x" in rest or "--extract" in rest):
            for flag in ("-C", "--directory"):
                if flag in rest and rest.index(flag) + 1 < len(rest):
                    self.add(cwd, rest[rest.index(flag) + 1], destructive=True)
        elif name in ("curl", "wget"):
            for flag in ("-o", "-O", "--output", "--output-document"):
                if flag in rest and rest.index(flag) + 1 < len(rest):
                    self.add(cwd, rest[rest.index(flag) + 1])
        elif name == "find":
            if _find_writes(rest):
                roots = []
                for arg in rest:
                    if arg.startswith("-") or arg in ("!", "("):
                        break
                    roots.append(arg)
                for root in roots or ["."]:
                    self.add(cwd, root, destructive=True)
        elif name == "git":
            cwd = self._git(rest, cwd)
        return cwd

    def _git(self, rest: list[str], cwd: Path) -> Path:
        index = 0
        work = cwd
        while index < len(rest) and rest[index].startswith("-"):
            option = rest[index]
            if option == "-C" and index + 1 < len(rest):
                work = _lexical(work, _substitute(rest[index + 1], self.variables))
                index += 2
                continue
            if option in ("-c", "--git-dir", "--work-tree") and index + 1 < len(rest):
                index += 2
                continue
            index += 1
        options = rest[:index]
        if index >= len(rest):
            return cwd
        sub = rest[index]
        args = rest[index + 1:]
        lowered = " ".join([*options, *args]).lower()
        if (sub in GIT_INTERNAL_SUBCOMMANDS
                or (sub == "update-index" and any(flag in args for flag in GIT_INTERNAL_FLAGS))
                or (sub == "config" and any(key in lowered for key in GIT_INTERNAL_CONFIG))
                or any(key in lowered for key in ("core.sparsecheckout", "core.hookspath"))):
            self.call.git_internal.append(f"git {sub}")
        if (sub in ("tag", "push", "update-ref") and any("release/" in arg for arg in args)) \
                or (sub == "push" and "--tags" in args):
            self.call.git_release.append(" ".join(["git", sub, *args])[:120])
        if sub not in GIT_PATH_SUBCOMMANDS:
            return cwd
        if "--" in args:
            paths = args[args.index("--") + 1:]
        else:
            paths = [arg for arg in args if not arg.startswith("-")]
            if sub == "checkout":
                # 没有 -- 时第一个词可能是分支名：只认像路径的（含 / 或 . 或存在的文件）
                paths = [arg for arg in paths if "/" in arg or arg in (".", "*") or (work / arg).exists()]
        if sub == "clean" and not paths:
            paths = ["."]
        for arg in paths:
            self.add(work, arg, destructive=True)
        return cwd


def collect(data: dict) -> Call:
    root = _project_root(data)
    cwd = Path(data.get("cwd") or os.getcwd())
    if not cwd.is_absolute():
        cwd = root / cwd
    tool = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input 不是对象")
    call = Call(root=root, cwd=cwd, tool=tool)
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            call.targets.append(Target(value, _lexical(cwd, value)))
    command = tool_input.get("command")
    if isinstance(command, str) and command:
        call.command = command
        _Parser(call).parse(command, cwd)
    return call


# ---- 受保护目录判定 ----


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _covers(target: Target, protected: Path) -> bool:
    """目标是否碰到受保护路径：在它里面；破坏性操作的目标是它的祖先；通配符能匹配到它。"""
    path = target.path
    if path is None:
        return False
    if _under(path, protected) or _under(Path(os.path.realpath(path)), protected):
        return True
    if target.destructive and _under(protected, path):
        return True
    name = path.name
    if any(ch in name for ch in "*?["):
        parent = path.parent
        if _under(protected, parent) and protected != parent:
            first = protected.relative_to(parent).parts[0]
            return fnmatch.fnmatch(first, name)
    return False


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    for name in names:
        if re.search(rf"(?<![\w.-]){re.escape(name)}(?:/|(?=['\"\s)\]}};|&]|$))", text):
            return True
    return False


def _code_writes_to(call: Call, names: tuple[str, ...]) -> bool:
    code = "\n".join(call.code)
    return bool(code) and _mentions(code, names) and bool(_CODE_WRITE_RE.search(code))


def _variable_hit(call: Call, names: tuple[str, ...]) -> bool:
    return any(target.unresolved for target in call.targets) and _mentions(call.command, names)


def _hits(call: Call, protected: list[Path], names: tuple[str, ...]) -> list[str]:
    found = [target.raw for target in call.targets if any(_covers(target, p) for p in protected)]
    if _code_writes_to(call, names):
        found.append("内联代码")
    if _variable_hit(call, names):
        found.append("含变量的路径")
    return list(dict.fromkeys(found))


# ---- 四个守卫 ----


def _resource_dirs(call: Call) -> list[Path]:
    """资料区：项目根下的 resource/，以及目标路径里出现的任何 resource 目录（同旧守卫口径）。

    助手副本里 resource 是指向主线工作区的链接：链接后面的真实目录同样受保护（经相对或绝对路径直接写主线资料）。
    """
    dirs = [call.root / "resource"]
    real = Path(os.path.realpath(call.root / "resource"))
    if real != call.root / "resource":
        dirs.append(real)
    for target in call.targets:
        if target.path is not None and _under(target.path, call.root):
            parts = target.path.relative_to(call.root).parts
            if "resource" in parts:
                dirs.append(call.root.joinpath(*parts[: parts.index("resource") + 1]))
    return list(dict.fromkeys(dirs))


def guard_resource(call: Call) -> str | None:
    hits = _hits(call, _resource_dirs(call), ("resource",))
    if not hits:
        return None
    return (
        f"禁止模型修改 resource 目录下的内容：{'; '.join(hits)}。"
        "resource 是用户维护的源材料，只允许读取；如需变更源材料，请由用户手工修改，"
        "模型只能基于 resource 刷新或生成派生产物。"
    )


# ---- 资料原件只读文本版（F4c；ADR-0073、0076）----

# 要经多源连接器转成文本版才读的资料原件：PDF、Office、图片等。主容器里不解析它们——内测引擎跑在后端进程里，
# 它自带的读文件工具遇到 PDF 会用 pdfplumber 在进程内解析（openjiuwen 依赖 pdfplumber，镜像里卸不掉）
BINARY_ORIGINALS = (".pdf", ".doc", ".docx", ".docm", ".dot", ".dotx", ".xls", ".xlsx", ".xlsm", ".xlsb", ".ppt",
                    ".pptx", ".pptm", ".odt", ".ods", ".odp", ".rtf", ".wps", ".et", ".dps", ".png", ".jpg", ".jpeg",
                    ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic")
_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic")
# 只看文件元信息、不解析内容的命令：碰到原件也放行
_META_COMMANDS = {"ls", "stat", "file", "wc", "du", "sha256sum", "sha1sum", "md5sum", "shasum", "cksum", "git", "echo",
                  "printf", "test", "[", "cd", "pwd", "realpath", "readlink", "basename", "dirname", "true", "false"}
_ORIGINAL_RE = re.compile(r"[^\s'\"`;|&()<>=]+?(?:" + "|".join(re.escape(s) for s in BINARY_ORIGINALS) + r")(?![\w.])",
                          re.IGNORECASE)


def _text_version(call: Call, path: Path) -> str | None:
    """资料原件 → 文本版的工作区相对路径（resource/text/<分组>/<原文件名>.md）；不在资料区里返回 None。"""
    for base in _resource_dirs(call)[:2]:
        if _under(path, base) and path != base:
            rel = path.relative_to(base).as_posix()
            return None if rel.startswith("text/") else f"resource/text/{rel}.md"
    return None


def _original_reason(call: Call, path: Path, shown: str) -> str:
    text = _text_version(call, path)
    if path.suffix.lower() in _IMAGES:
        return f"图片资料「{shown}」暂不支持读取，按未读处理。"
    if text and (call.root / text).is_file():
        return f"资料原件「{shown}」不在这里解析，改读它的文本版 {text}（多源连接器转出的 Markdown，带页码、工作表标记）。"
    return (f"资料原件「{shown}」还没有文本版（转换中、暂不支持或转换失败），这里不解析原件；"
            "这份资料按未读处理，资料区能看到原因。")


def _originals_in_command(call: Call) -> list[tuple[Path, str]]:
    """命令（含 heredoc、内联代码）里提到的资料原件：路径直接在资料区下，或只写了文件名、资料区里有同名原件。"""
    found = []
    names = {}
    for base in _resource_dirs(call)[:1]:
        if base.is_dir():
            for path in base.rglob("*"):
                if path.suffix.lower() in BINARY_ORIGINALS and "text" not in path.relative_to(base).parts[:1]:
                    names.setdefault(path.name, path)
    for raw in _ORIGINAL_RE.findall(call.command):
        candidate = raw.strip("'\"")
        for path in (_lexical(call.cwd, candidate), _lexical(call.root, candidate)):
            if _text_version(call, path) is not None:
                found.append((path, candidate))
                break
        else:
            if Path(candidate).name in names:
                found.append((names[Path(candidate).name], candidate))
    return found


def _only_metadata(command: str) -> bool:
    for words, _joiner in _segments(_tokens(command)):
        words = [word for word in words if not _ASSIGN_RE.match(word)]
        while words and words[0] in PREFIX_COMMANDS:
            words = words[1:]
        if words and Path(words[0]).name not in _META_COMMANDS:
            return False
    return True


def guard_resource_read(call: Call) -> str | None:
    """读资料原件：Read / 内测引擎 read_file 直接读，或 Bash 里拿解析库、转换命令碰原件，一律拒绝并指向文本版。"""
    if call.tool == "Bash":
        if not call.command:
            return None
        hits = _originals_in_command(call)
        if not hits or _only_metadata(call.command):
            return None
        return _original_reason(call, *hits[0])
    for target in call.targets:
        if target.path is not None and target.path.suffix.lower() in BINARY_ORIGINALS \
                and _text_version(call, target.path) is not None:
            return _original_reason(call, target.path, target.raw)
    return None


def _toolset_dirs(call: Call) -> list[Path]:
    root = call.root
    dirs = [root / ".claude", root / "CLAUDE.md", root / "README.md", root / ".onto", root / ".git"]
    dotclaude = root / ".claude"
    if dotclaude.exists():
        real = Path(os.path.realpath(dotclaude))
        # 装配目录与包仓库：.claude 指向 <数据目录>/assembled/<哈希>
        dirs += [real.parent, real.parent.parent / "packs"] if real.parent.name == "assembled" else [real]
    here = Path(os.path.realpath(__file__)).parent.parent
    if here.parent.name == "assembled":
        dirs += [here.parent, here.parent.parent / "packs"]
    return list(dict.fromkeys(dirs))


# 内联代码里建符号链接的调用：python os.symlink / Path.symlink_to、node fs.symlink(Sync)、ruby File.symlink、perl / php symlink
_CODE_SYMLINK_RE = re.compile(r"(?:os\.symlink|\.symlink_to|symlinkSync|fs\.symlink|File\.symlink|(?<![\w.])symlink)\s*\(\s*")
_STRING_RE = re.compile(r"""(['"])((?:(?!\1).)*)\1""")


def _code_links(call: Call) -> list[tuple[Path | None, str | None]]:
    """内联代码里的建链接调用：第一个参数（链接目标）是字面量就按当前目录解析，不是就当看不出来。"""
    out: list[tuple[Path | None, str | None]] = []
    for code in call.code:
        for match in _CODE_SYMLINK_RE.finditer(code or ""):
            literal = _STRING_RE.match(code, match.end())
            out.append((call.cwd, os.path.expanduser(literal.group(2)) if literal else None))
    return out


def _escaping_links(call: Call) -> list[str]:
    """要建的符号链接里，目标按链接所在目录解析后不在项目根（或副本根）内的；资料区（副本里是链接）也算根内。"""
    roots = [Path(os.path.realpath(call.root))]
    if (call.root / "resource").exists():
        roots.append(Path(os.path.realpath(call.root / "resource")))
    out = []
    for link_dir, target in [*call.links, *_code_links(call)]:
        if link_dir is None or not target:
            out.append(target or "（目标看不出来）")
            continue
        real = Path(os.path.realpath(target if os.path.isabs(target) else link_dir / target))
        if not any(_under(real, root) for root in roots):
            out.append(target)
    return out


def guard_toolset(call: Call) -> str | None:
    if call.git_release:
        return f"发布标签（release/*）只在发布页打，助手不能打、删、推：{'; '.join(dict.fromkeys(call.git_release))}。"
    escaping = _escaping_links(call)
    if escaping:
        return f"不能建指向工作区外的符号链接：{'; '.join(dict.fromkeys(escaping))}。需要的内容复制进工作区再用。"
    if call.git_internal:
        return f"不能改版本库的内部设置：{'; '.join(dict.fromkeys(call.git_internal))}。"
    hits = _hits(call, _toolset_dirs(call), (".claude", "CLAUDE.md", ".onto", "assembled", ".git"))
    if not hits:
        return None
    return (
        f"工具箱与受保护目录只读，禁止写入：{'; '.join(hits)}。"
        "（.claude、CLAUDE.md、README.md 是装配出来的能力包，.onto 是能力包锁文件，.git 是版本库内部）"
        "正确去处：一次性辅助脚本写到工作区 tmp/ 目录（用完删除）；"
        "正式代码产物走 codegen 流程进 ontology/scripts/。"
        "若发现工具箱脚本/模板本身有 bug，请把现象反馈给维护者，不要在任务工作区里现场 patch。"
    )


def guard_lock(call: Call) -> str | None:
    for target in call.targets:
        norm = (target.path.as_posix() if target.path is not None else target.raw.replace("\\", "/"))
        if re.search(r"(^|/)\.ontology-state/lock-[^/]+\.json$", norm):
            return (
                "锁文件由工具管理，禁止手工编辑（见 CLAUDE.md §9）。"
                "如需清理残留锁，运行：python3 .claude/scripts/state_helper.py force-release <cmd>"
            )
        if re.search(r"(^|/)\.ontology-state/script-verifications\.json$", norm):
            return (
                "脚本运行验证记录只能由验证工具写入，禁止手工编辑。请编写用例并运行："
                "python3 .claude/scripts/script_verify.py run <脚本> --cases <用例文件>"
            )
    if _hits(call, [call.root / ".ontology-state"], (".ontology-state",)) and any(
        target.destructive for target in call.targets
    ):
        return "工具状态目录 .ontology-state 由工具管理，禁止整体删除或还原。"
    return None


def _active_scope(call: Call) -> tuple[list[str], list[str], set[str]] | None:
    """正在执行的命令的写入范围 (命令名, 放行的路径模式, 段外已有的条目)；没有在执行的命令、或有不属于任何节点的
    命令（不限范围）时返回 None。文件读不出按没有处理（命令照常，只是不收窄）。

    分段建模时 state_helper 另记 `protect`：这一段以外、已经有了的条目，在放行的模式里也不让改（详设 4.3）。"""
    try:
        data = json.loads(call.root.joinpath(*ACTIVE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, dict) or not commands:
        return None
    names, patterns, protect = [], [], set()
    for name, spec in commands.items():
        if not _command_alive(call.root, str(name)):
            continue  # 命令被打断、引擎换了实例：它的范围不再算数（审查第 9 条）
        allow = spec.get("allow") if isinstance(spec, dict) else None
        if not spec or not isinstance(spec, dict) or not spec.get("node") or not isinstance(allow, list):
            return None
        names.append(str(name))
        patterns += [str(item).replace("**", "*") for item in allow if str(item) not in patterns]
        if isinstance(spec.get("protect"), list):
            protect |= {str(item) for item in spec["protect"]}
    return (names, patterns, protect) if names else None


def _command_alive(root: Path, command: str) -> bool:
    """命令的锁还在、持有者还活着：锁记的引擎实例是工作区当前的引擎，或记的进程号还在。"""
    try:
        lock = json.loads((root / ".ontology-state" / f"lock-{command}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(lock, dict):
        return False
    owner = lock.get("owner")
    if owner:
        try:
            engine = json.loads(root.joinpath(".onto", "run", "engine.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(engine, dict) and owner == f"engine:{engine.get('engine_id')}"
    pid = lock.get("owner_pid")
    if pid is None:
        return True  # 没有持有者信息的旧锁：按还在跑
    try:
        pid = int(pid)
        if pid <= 1:
            return False
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _ontology_kind(call: Call, path: Path, scope: tuple[list[str], list[str]] | None) -> str | None:
    if not _under(path, call.root):
        return None
    parts = path.relative_to(call.root).parts
    if len(parts) < 3 or parts[0] != "ontology":
        return None
    sub = parts[1]
    if sub in MANAGED_ONTOLOGY_DIRS:
        return "managed"
    if sub not in ALLOWED_ONTOLOGY_DIRS:
        return "unknown"
    if scope is not None:
        rel = "/".join(parts)
        if not any(fnmatch.fnmatchcase(rel, pattern) for pattern in scope[1]) or rel in scope[2]:
            return "outside"
    return None


def guard_scope(call: Call) -> str | None:
    scope = _active_scope(call)
    found = []
    for target in call.targets:
        if target.path is None:
            continue
        kind = _ontology_kind(call, target.path, scope)
        if kind:
            found.append((target.raw, kind))
    solver = call.root / "ontology" / "solver"
    if _code_writes_to(call, ("ontology/solver",)) or any(
        target.destructive and target.path is not None and _covers(target, solver) for target in call.targets
    ):
        found.append(("ontology/solver", "managed"))
    if not found:
        return _guard_confine(call)
    reasons = "; ".join(dict.fromkeys(raw for raw, _ in found))
    kinds = {kind for _, kind in found}
    if "managed" in kinds:
        return (
            f"ontology/solver/ 是求解契约受管目录，不允许直接写入：{reasons}。"
            "Decision Model、Schema 与 Presentation 须成组一致，请提示用户在求解模型面板中维护。"
        )
    if "outside" in kinds and scope is not None:
        names, patterns, protect = scope
        allowed = "、".join(patterns) or "（没有声明可写的路径）"
        sliced = "；别的段已有的条目只读，只能链接引用" if protect else ""
        return (
            f"正在执行「{'、'.join(names)}」，这一步只能写它声明的范围：{allowed}{sliced}；此次写入越界：{reasons}。"
            "范围外的条目可能正被你在编辑器里修改；确实要改，先结束这条命令，或换用负责那部分内容的命令。"
        )
    packaged_hint = (
        "只有 ontology/{object,operation,rule,metric,scripts} 会进入知识包（consumer 可见）；"
        "案例/方案类知识请建模为 ontology/rule/ 下的场景规则（触发特征 → 方案要点），并 wikilink 关联涉事对象。"
    )
    return f"ontology/ 下不允许自创目录：{reasons}。允许的子目录以 ontology_checker 白名单为准；{packaged_hint}"


# ---- 范围限定（ADR-0085） ----


def _confine_spec(call: Call) -> tuple[dict, list[str]] | None:
    """组装结果里的 confine，和归范围文件所属节点的命令（这些命令运行时才能写范围文件）。"""
    try:
        flow = json.loads((call.root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    spec = flow.get("confine") if isinstance(flow, dict) else None
    if not isinstance(spec, dict) or not isinstance(spec.get("scope_file"), str) or not spec["scope_file"]:
        return None
    commands = flow.get("commands") if isinstance(flow.get("commands"), dict) else {}
    owners = [str(name) for name, item in commands.items() if isinstance(item, dict) and item.get("node") == spec.get("node")]
    return spec, owners


def _git_out(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=root, capture_output=True, timeout=10)
    if out.returncode != 0:
        raise LookupError(args[0])
    return out.stdout.decode("utf-8", "replace")


def _mainline_head(root: Path) -> tuple[Path, str]:
    """助手副本是 git worktree：主线是公共目录的 HEAD（不是副本自己的分支）。"""
    common = Path(_git_out(root, "rev-parse", "--git-common-dir").strip())
    common = common if common.is_absolute() else (root / common).resolve()
    return common, _git_out(root, "--git-dir", str(common), "rev-parse", "HEAD").strip()


def _confined_entries(common: Path, head: str, scope_file: str, local: str) -> list[str]:
    """只读的条目：主线上的范围加上副本里的范围，取并集。放宽要等合入主线；副本里确认的收紧立刻算，确认的这一轮
    接着改也受限。"""
    out = []
    try:
        out += _entries_outside(common, json.loads(_git_out(common, "--git-dir", str(common), "show", f"{head}:{scope_file}")))
    except (LookupError, ValueError):
        pass
    try:
        out += _entries_outside(common, json.loads(local))
    except ValueError:
        pass
    return sorted(set(out))


def _entries_outside(common: Path, scope) -> list[str]:
    """范围文档 → 只读的条目（基线里有、editable 里没有）；读不出、不是 active 返回空。"""
    if not isinstance(scope, dict) or scope.get("status") != "active":
        return []
    baseline = scope.get("baseline") if isinstance(scope.get("baseline"), dict) else {}
    commit = str(baseline.get("commit") or "")
    editable = scope.get("editable")
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit) or not isinstance(editable, list):
        return []
    try:
        listed = _git_out(common, "--git-dir", str(common), "ls-tree", "-r", "-z", "--name-only", commit, "--", "ontology")
    except LookupError:
        return []
    allowed = {str(item) for item in editable}
    out = []
    for rel in listed.split("\0"):
        parts = rel.split("/")
        if (len(parts) >= 3 and parts[1] in CONFINED_ONTOLOGY_DIRS and parts[-1].lower() != "index.md"
                and not parts[-1].startswith(".") and rel not in allowed):
            out.append(rel)
    return out


def _confine_protected(call: Call, scope_file: str) -> set[str] | None:
    """只读条目，按主线 HEAD 和副本里范围文件的内容缓存在副本的 .onto/run/ 下（助手写不了 .onto）。git 出错返回 None，不收窄也不缓存。"""
    try:
        common, head = _mainline_head(call.root)
    except (OSError, subprocess.SubprocessError, LookupError):
        return None
    try:
        local = (call.root / scope_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        local = ""
    key = [head, scope_file, hashlib.sha256(local.encode("utf-8")).hexdigest()]
    cache = call.root.joinpath(*CONFINE_CACHE_REL)
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("key") == key and isinstance(cached.get("protected"), list):
            return {str(item) for item in cached["protected"]}
    except (OSError, ValueError):
        pass
    try:
        protected = _confined_entries(common, head, scope_file, local)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps({"key": key, "protected": protected}, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, cache)
    except OSError:
        pass  # 缓存写不进去只是下次再算
    return set(protected)


def _confine_target_hit(call: Call, target: Target, protected: set[str]) -> bool:
    if target.path is None:
        return False
    for path in dict.fromkeys((target.path, Path(os.path.realpath(target.path)))):
        if not _under(path, call.root):
            if target.destructive and _under(call.root, path):
                return True
            continue
        rel = path.relative_to(call.root).as_posix()
        if rel in protected:
            return True
        if target.destructive and (rel == "." or any(item.startswith(rel + "/") for item in protected)):
            return True
        if any(ch in rel for ch in "*?[") and any(
                fnmatch.fnmatchcase(item, rel) or fnmatch.fnmatchcase(item, rel + "/*") for item in protected):
            return True
    return False


def _confine_code_hit(call: Call, protected: set[str]) -> bool:
    """内联代码有写操作，且提到的 ontology 路径落在只读条目上或是它们的上级目录（只写 ontology 也算）。"""
    code = "\n".join(call.code)
    if not code or not _CODE_WRITE_RE.search(code):
        return False
    for match in _ONTOLOGY_MENTION_RE.finditer(code):
        mention = re.split(r"[{$%*?\[]", match.group(0), maxsplit=1)[0].rstrip("/")
        if any(item == mention or item.startswith(mention + "/") for item in protected):
            return True
    return False


def _guard_confine(call: Call) -> str | None:
    if not call.targets and not call.code:
        return None
    declared = _confine_spec(call)
    if declared is None:
        return None
    spec, owner_commands = declared
    scope_file = spec["scope_file"]
    entry = str(spec.get("entry") or "圈定范围的命令")
    if _hits(call, [call.root / scope_file], (scope_file,)) and not any(
            _command_alive(call.root, name) for name in owner_commands):
        return (f"范围文件 {scope_file} 只能在 {entry} 里写；直接改它也扩大不了范围（放宽只认合入主线的版本），"
                f"要调整范围请运行 {entry}。")
    protected = _confine_protected(call, scope_file)
    if not protected:
        return None
    found = [target.raw for target in call.targets if _confine_target_hit(call, target, protected)]
    if _confine_code_hit(call, protected):
        found.append("内联代码")
    if _variable_hit(call, ("ontology",)):
        found.append("含变量的路径")
    if not found:
        return None
    shown = "、".join(list(dict.fromkeys(found))[:5])
    return f"本轮增量范围外的条目只读：{shown}；要扩大范围请重新圈定（{entry}），不再需要限制就说「结束本轮」。新增条目不受限。"


# ---- 插件开发（ADR-0087） ----

PLUGINS_DIR = "plugins"
# 项目根下一定要拦的名字（目录里还没有的也算）；其余按根目录下实际有的条目补
_PROJECT_TOP = ("ontology", "resource", "reports", ".claude", "CLAUDE.md", "README.md", ".onto", ".ontology-state",
                "tmp")
_ABS_PATH_RE = re.compile(r"(?:(?<=[\s'\"`(=,\[])|^)(~?/[^\s'\"`;|&()<>,\]}]+)")
PLUGIN_SCOPE_REASON = "插件开发模式只能改 plugins/ 下的草稿，本体、资料和项目里别的文件只读"


def _data_dir(root: Path) -> Path | None:
    """项目根（主线工作区或助手副本）所在的数据目录；认不出来时 None。"""
    if root.name == "assistant" and root.parent.parent.name == "changesets":
        return root.parent.parent.parent
    if root.parent.name == "workspaces":
        return root.parent.parent
    return None


def _plugin_allowed(root: Path, data: Path | None, path: Path) -> bool:
    """项目根内只放行 plugins/ 下；数据目录下（别的项目、主线工作区、包仓库、装配目录）一律不放行；别处放行。"""
    plugins = root / PLUGINS_DIR
    for candidate in dict.fromkeys((path, Path(os.path.realpath(path)))):
        if _under(candidate, plugins) and candidate != plugins:
            continue
        if _under(candidate, root) or (data is not None and _under(candidate, data)):
            return False
    return True


def _project_names(root: Path) -> tuple[str, ...]:
    try:
        present = [name for name in os.listdir(root) if name not in (PLUGINS_DIR, ".git")]
    except OSError:
        present = []
    return tuple(dict.fromkeys([*_PROJECT_TOP, *present]))


def _mentions_top(text: str, names: tuple[str, ...]) -> bool:
    """命令或代码里把项目根下的某个条目当路径开头提到（`ontology/x`、`./reports`、`"CLAUDE.md"`）；
    `plugins/x/ontology/` 这类草稿里的同名子目录不算。"""
    for name in names:
        if re.search(rf"(?:^|[\s'\"`(=,\[])(?:\./)?{re.escape(name)}(?:/|(?=['\"`\s)\]}};|&,]|$))", text):
            return True
    return False


def guard_plugin_scope(call: Call) -> str | None:
    root = call.root
    data = _data_dir(root)
    if call.git_release or call.git_internal:
        return f"{PLUGIN_SCOPE_REASON}：不能改版本库的设置或打发布标签。"
    escaping = _escaping_links(call)
    if escaping:
        return f"{PLUGIN_SCOPE_REASON}：不能建指向工作区外的符号链接（{'; '.join(dict.fromkeys(escaping))}）。"
    found = [target.raw for target in call.targets
             if target.path is not None and not _plugin_allowed(root, data, target.path)]
    names = _project_names(root)
    if any(target.unresolved for target in call.targets) and _mentions_top(call.command, names):
        found.append("含变量的路径")
    code = "\n".join(call.code)
    if code and _CODE_WRITE_RE.search(code):
        absolute = [Path(os.path.expanduser(raw)) for raw in _ABS_PATH_RE.findall(code)]
        if _mentions_top(code, names) or any(not _plugin_allowed(root, data, path) for path in absolute):
            found.append("内联代码")
    if not found:
        return None
    return f"{PLUGIN_SCOPE_REASON}，这次写入越界：{'; '.join(list(dict.fromkeys(found))[:5])}。"


GUARDS = {
    "plugin_scope": guard_plugin_scope,
    "resource": guard_resource,
    "resource_read": guard_resource_read,
    "toolset": guard_toolset,
    "lock": guard_lock,
    "scope": guard_scope,
}


def check(guard: str, data: dict) -> str | None:
    """供测试与后端直接调用：返回拒绝理由，放行返回 None；出错抛异常。"""
    return GUARDS[guard](collect(data))


def main(argv: list[str]) -> int:
    guard = argv[1] if len(argv) > 1 else ""
    try:
        if guard not in GUARDS:
            deny(f"未知的守卫：{guard or '（空）'}，按拒绝处理")
            return 0
        raw = sys.stdin.read()
        if not raw.strip():
            deny("守卫没有收到工具调用参数，按拒绝处理")
            return 0
        data = json.loads(raw)
        if not isinstance(data, dict):
            deny("守卫收到的工具调用参数格式不对，按拒绝处理")
            return 0
        reason = check(guard, data)
        if reason:
            deny(reason)
    except Exception as exc:  # noqa: BLE001 - 出错即拒（方案 D9）
        deny(f"写保护守卫执行出错，按拒绝处理（{type(exc).__name__}）。请换一种写法，或把现象反馈给维护者。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
