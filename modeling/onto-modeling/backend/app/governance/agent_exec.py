#!/usr/bin/env python3
"""agent 子进程的执行包装（方案 E10）：只给最小环境，并切到 agent 的系统用户。只用标准库。

两种用法（由 bin/ 下的两个薄壳选择）：

- `cli`：Claude 引擎的 CLI 入口（SDK 的 cli_path 指向 bin/onto-agent-cli）。环境里只留运行 CLI 必需的
  变量与模型网关配置，去掉后端的 ONTO_WEB_*、JWT、管理令牌等；再设 CLAUDE_CODE_SHELL_PREFIX，
  让 CLI 里 agent 执行的每条 bash、每个 hook 都经下面的 `shell` 再筛一次；
- `shell`：agent 执行的 bash 命令（Claude 的 shell 前缀、Jiuwen 的命令执行）。只给最小环境，凭据类
  变量（含模型 Key）一律不带。

后端以 root 运行且配置了 ONTO_WEB_AGENT_USER 时，在 exec 之前切到该用户（只保留它自己的组），
umask 007；这样密钥目录（0700，归后端用户）对 agent 不可读。
"""

from __future__ import annotations

import os
import re
import sys

# 两种模式都保留的基础变量
BASE_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "TERM",
    "COLUMNS", "LINES", "PWD", "SHLVL", "SYSTEMROOT", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE",
})
BASE_PREFIXES = ("LC_",)
# CLI 本身需要的：模型网关配置、CLI 与 SDK 的开关、链路传播
CLI_KEEP = frozenset({
    "DISABLE_AUTOUPDATER", "IS_SANDBOX", "MAX_THINKING_TOKENS", "TRACEPARENT", "TRACESTATE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
CLI_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CLAUDECODE")
# agent 的 bash 能看到的：项目目录等 CLI 设的非密钥变量、git 的非密钥变量
SHELL_PREFIXES = ("CLAUDE_", "GIT_")
# 凭据类变量名：bash 环境里一律不带（模型 Key、令牌、口令、cookie……）
SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH|SESSION_ID)")
_USERINFO_URL = re.compile(r"://[^/@\s]+@")

# 包装器自己用的控制变量：读完就从环境里去掉
REAL_CLI_ENV = "ONTO_AGENT_REAL_CLI"
AGENT_USER_ENV = "ONTO_AGENT_USER"
SHELL_PREFIX_ENV = "CLAUDE_CODE_SHELL_PREFIX"
CONTROL = (REAL_CLI_ENV, AGENT_USER_ENV, "ONTO_AGENT_PYTHON")


def _base(name: str) -> bool:
    return name in BASE_KEEP or name.startswith(BASE_PREFIXES)


def cli_env(env: dict[str, str]) -> dict[str, str]:
    """CLI 进程的环境：基础变量 + 模型网关配置 + CLI 开关。"""
    return {
        name: value for name, value in env.items()
        if name not in CONTROL and (_base(name) or name in CLI_KEEP or name.startswith(CLI_PREFIXES))
    }


def shell_env(env: dict[str, str]) -> dict[str, str]:
    """agent 执行的 bash 的环境：基础变量 + 非密钥的 CLAUDE_* / GIT_*；凭据类变量一律去掉。"""
    result = {}
    for name, value in env.items():
        if name in CONTROL or SECRET_NAME.search(name):
            continue
        if name.lower() in ("http_proxy", "https_proxy", "no_proxy"):
            if _USERINFO_URL.search(value or ""):
                continue  # 带账号口令的代理地址也是凭据
            result[name] = value
            continue
        if _base(name) or name.startswith(SHELL_PREFIXES):
            result[name] = value
    return result


def drop_privileges(username: str) -> None:
    """root 切到 agent 用户：补充组只留它自己的组，再 setgid、setuid；umask 007。"""
    import pwd

    if not username or os.getuid() != 0:
        return
    entry = pwd.getpwnam(username)
    os.setgroups([entry.pw_gid])
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    os.umask(0o007)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("cli", "shell"):
        sys.stderr.write("usage: agent_exec.py cli|shell ...\n")
        return 2
    mode, args = argv[1], argv[2:]
    env = dict(os.environ)
    agent_user = env.get(AGENT_USER_ENV, "")
    if mode == "cli":
        real_cli = env.get(REAL_CLI_ENV, "")
        if not real_cli:
            sys.stderr.write("onto-agent-cli: 没有 ONTO_AGENT_REAL_CLI\n")
            return 2
        clean = cli_env(env)
        shell_shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "onto-agent-shell")
        clean[SHELL_PREFIX_ENV] = shell_shim
        drop_privileges(agent_user)
        os.execve(real_cli, [real_cli, *args], clean)
    clean = shell_env(env)
    drop_privileges(agent_user)
    if len(args) == 1:
        # CLI 的 shell 前缀与 Jiuwen 都把整条命令作为一个参数传进来
        os.execve("/bin/sh", ["/bin/sh", "-c", args[0]], clean)
    if not args:
        sys.stderr.write("onto-agent-shell: 没有要执行的命令\n")
        return 2
    path = args[0] if os.sep in args[0] else _which(args[0], clean.get("PATH", os.defpath))
    os.execve(path, args, clean)
    return 0


def _which(name: str, search: str) -> str:
    for directory in search.split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return name


if __name__ == "__main__":
    sys.exit(main(sys.argv))
