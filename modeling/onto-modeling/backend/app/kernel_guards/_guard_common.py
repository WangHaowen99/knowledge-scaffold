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
"""

from __future__ import annotations

import fnmatch
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

    def _segment(self, words: list[str], cwd: Path, bodies: list[str], depth: int) -> Path:
        args: list[str] = []
        index = 0
        while index < len(words):
            word = words[index]
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
        if index >= len(rest):
            return cwd
        sub = rest[index]
        args = rest[index + 1:]
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
    """资料区：项目根下的 resource/，以及目标路径里出现的任何 resource 目录（同旧守卫口径）。"""
    dirs = [call.root / "resource"]
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


def guard_toolset(call: Call) -> str | None:
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


def _active_scope(call: Call) -> tuple[list[str], list[str]] | None:
    """正在执行的命令的写入范围 (命令名, 放行的路径模式)；没有在执行的命令、或有不属于任何节点的
    命令（不限范围）时返回 None。文件读不出按没有处理（命令照常，只是不收窄）。"""
    try:
        data = json.loads(call.root.joinpath(*ACTIVE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, dict) or not commands:
        return None
    names, patterns = [], []
    for name, spec in commands.items():
        allow = spec.get("allow") if isinstance(spec, dict) else None
        if not spec or not isinstance(spec, dict) or not spec.get("node") or not isinstance(allow, list):
            return None
        names.append(str(name))
        patterns += [str(item).replace("**", "*") for item in allow if str(item) not in patterns]
    return names, patterns


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
        if not any(fnmatch.fnmatchcase(rel, pattern) for pattern in scope[1]):
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
        return None
    reasons = "; ".join(dict.fromkeys(raw for raw, _ in found))
    kinds = {kind for _, kind in found}
    if "managed" in kinds:
        return (
            f"ontology/solver/ 是求解契约受管目录，不允许直接写入：{reasons}。"
            "Decision Model、Schema 与 Presentation 须成组一致，请提示用户在求解模型面板中维护。"
        )
    if "outside" in kinds and scope is not None:
        names, patterns = scope
        allowed = "、".join(patterns) or "（没有声明可写的路径）"
        return (
            f"正在执行「{'、'.join(names)}」，这一步只能写它声明的范围：{allowed}；此次写入越界：{reasons}。"
            "范围外的条目可能正被你在编辑器里修改；确实要改，先结束这条命令，或换用负责那部分内容的命令。"
        )
    packaged_hint = (
        "只有 ontology/{object,operation,rule,metric,scripts} 会进入知识包（consumer 可见）；"
        "案例/方案类知识请建模为 ontology/rule/ 下的场景规则（触发特征 → 方案要点），并 wikilink 关联涉事对象。"
    )
    return f"ontology/ 下不允许自创目录：{reasons}。允许的子目录以 ontology_checker 白名单为准；{packaged_hint}"


GUARDS = {
    "resource": guard_resource,
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
