"""agent 身份与环境隔离（方案 E10、第 0 章问题 5；P1 提前）。

- **最小环境**：两个引擎里 agent 自己执行的 bash 不再继承后端的完整环境。Claude 引擎的 CLI 经
  `bin/onto-agent-cli` 启动（只留模型网关配置与 CLI 开关），CLI 里的每条 bash 与 hook 经
  `bin/onto-agent-shell` 再筛掉全部凭据类变量；Jiuwen 引擎在进程内，它的命令执行改走同一个
  `onto-agent-shell`。
- **独立用户**：配置 ONTO_WEB_AGENT_USER（镜像里是非 root 的 onto-agent）且后端以 root 运行时，
  上面两个包装在 exec 之前切到该用户，只保留它自己的组。工作区、CLI 的 HOME、Jiuwen 工件区归这个组
  可写；密钥目录 0700 归后端用户，agent 读不到；数据目录其余部分对它不可读。
- 引擎文件只做最小接入：Claude 在拼 SDK 选项时调 `claude_options`，Jiuwen 建 agent 时调
  `install_jiuwen_shell_isolation`。
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
import sys
from pathlib import Path

from .. import config
from . import agent_exec

_log = logging.getLogger(__name__)

BIN_DIR = Path(__file__).resolve().parent / "bin"
CLI_SHIM = BIN_DIR / "onto-agent-cli"
SHELL_SHIM = BIN_DIR / "onto-agent-shell"
PERMS_MARKER = ".onto-agent-perms-v1"


def agent_user() -> str:
    return str(config.AGENT_USER or "").strip()


def switching() -> bool:
    """会不会真的切换用户：配置了 agent 用户、本进程是 root、该用户存在。"""
    return bool(_agent_ids()) and os.getuid() == 0


def _agent_ids() -> tuple[int, int] | None:
    name = agent_user()
    if not name or os.name != "posix":
        return None
    import pwd

    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def _control_env() -> dict[str, str]:
    env = {"ONTO_AGENT_PYTHON": sys.executable}
    if agent_user():
        env[agent_exec.AGENT_USER_ENV] = agent_user()
    return env


# ---------------------------------------------------------------------------
# Claude 引擎
# ---------------------------------------------------------------------------


def real_cli_path() -> str:
    """真正的 CLI：降级兼容包配置的 CLI_PATH，否则 SDK 捆绑的 CLI，再否则 PATH 上的 claude。"""
    if config.CLI_PATH:
        return str(config.CLI_PATH)
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if bundled.is_file():
            return str(bundled)
    except ImportError:
        pass
    import shutil

    return shutil.which("claude") or ""


def claude_options(env: dict[str, str]) -> dict:
    """SDK 选项里替换 cli_path 与 env：CLI 经包装启动，环境只留模型网关配置与 CLI 开关。

    SDK 会把后端的 os.environ 并进子进程环境，筛选因此放在包装里做（agent_exec.cli_env）。
    """
    real = real_cli_path()
    if not real:
        return {}
    # SDK 的版本探测不带 options.env，也走包装：真 CLI 路径不是密钥，放进本进程环境
    os.environ.setdefault(agent_exec.REAL_CLI_ENV, real)
    return {
        "cli_path": str(CLI_SHIM),
        "env": {**env, agent_exec.REAL_CLI_ENV: real, **_control_env()},
    }


# ---------------------------------------------------------------------------
# Jiuwen 引擎
# ---------------------------------------------------------------------------

_jiuwen_installed = False


def jiuwen_command(command: str) -> str:
    """Jiuwen 的一条命令 → 经 onto-agent-shell 执行（最小环境、agent 用户）。"""
    return f"exec {shlex.quote(str(SHELL_SHIM))} {shlex.quote(command)}"


def jiuwen_env(env: dict[str, str] | None) -> dict[str, str]:
    return {**agent_exec.shell_env(dict(env or os.environ)), **_control_env()}


def install_jiuwen_shell_isolation() -> bool:
    """包一层 openjiuwen 的命令执行：命令改走 onto-agent-shell，环境换成最小环境。幂等。"""
    global _jiuwen_installed
    if _jiuwen_installed:
        return True
    try:
        from openjiuwen.core.sys_operation.local import shell_operation
    except ImportError:
        return False
    original = shell_operation.ShellOperation._create_subprocess

    async def isolated(self, command, cwd, env, **kwargs):
        return await original(self, jiuwen_command(command), cwd, jiuwen_env(env), **kwargs)

    isolated.__wrapped__ = original  # type: ignore[attr-defined]
    shell_operation.ShellOperation._create_subprocess = isolated
    _jiuwen_installed = True
    return True


# ---------------------------------------------------------------------------
# 目录权限（启动时，仅 root + 配置了 agent 用户）
# ---------------------------------------------------------------------------


def _group_tree(path: Path, gid: int) -> None:
    """整棵树归 agent 组、组可读写，目录 setgid（之后新建的文件自动归这个组）。"""
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            item = Path(root) / name
            if item.is_symlink():
                continue
            try:
                os.chown(item, -1, gid)
                mode = stat.S_IMODE(item.stat().st_mode)
                extra = stat.S_IRGRP | stat.S_IWGRP | (stat.S_IXGRP | stat.S_ISGID if item.is_dir() else 0)
                if not item.is_dir() and mode & stat.S_IXUSR:
                    extra |= stat.S_IXGRP
                os.chmod(item, (mode | extra) & ~0o007)
            except OSError:
                continue
    os.chown(path, -1, gid)
    os.chmod(path, 0o2770)


def share_read_only(path: Path) -> None:
    """agent 要读、不能改的目录（装配目录：项目的 `.claude` 链到这里，里面有内核守卫）：
    目录 0755、文件 0644（可执行的 0755），去掉组写与 setgid。装配器每建好一个装配目录调一次。"""
    path = Path(path)
    if not path.exists() or path.is_symlink():
        return
    for root, dirs, files in os.walk(path):
        for name in dirs:
            item = Path(root) / name
            if not item.is_symlink():
                os.chmod(item, 0o755)
        for name in files:
            item = Path(root) / name
            if not item.is_symlink():
                os.chmod(item, 0o755 if stat.S_IMODE(item.stat().st_mode) & stat.S_IXUSR else 0o644)
    os.chmod(path, 0o755)


# agent 只读的目录：装配目录（插件运行时，见 services/packs/assembler）
READ_ONLY_DIRS = ("assembled",)


def _shared_dirs() -> list[Path]:
    """agent 要读写的目录：项目工作区、Jiuwen 工件区、CLI 的 HOME。"""
    data = Path(config.DATA_DIR).resolve()
    dirs = [data / "workspaces", data / "jiuwen"]
    home = Path(os.environ.get("HOME") or "/").resolve()
    # 只认镜像里的布局（HOME 在数据卷上，与数据目录同级或在其下），不动别的家目录
    if home != Path("/") and (home.parent == data.parent or data in home.parents):
        dirs.append(home)
    return dirs


def prepare_filesystem() -> dict:
    """root + 配置了 agent 用户时：数据目录收紧、共享目录归 agent 组、密钥目录只归后端用户。

    第一次跑会递归改存量文件的组与权限（打标记，之后只处理顶层）；本进程 umask 改为 007，
    之后新建的文件对其他用户不可读。返回做了什么，供启动自检记录。
    """
    if not agent_user():
        return {"switching": False, "reason": "未配置 ONTO_WEB_AGENT_USER"}
    ids = _agent_ids()
    if ids is None:
        _log.error("ONTO_WEB_AGENT_USER=%s 不存在，agent 仍以后端用户运行", agent_user())
        return {"switching": False, "reason": "用户不存在"}
    if os.getuid() != 0:
        _log.warning("后端不是 root，无法切到 agent 用户 %s；agent 只做了环境最小化", agent_user())
        return {"switching": False, "reason": "后端不是 root"}
    _uid, gid = ids
    os.umask(0o007)
    data = Path(config.DATA_DIR)
    data.mkdir(parents=True, exist_ok=True)
    marker = data / "run" / PERMS_MARKER
    first = not marker.exists()
    # 数据目录：agent 只能穿过（去工作区、工件区、装配目录），不能列目录；其余条目对它不可读
    os.chown(data, -1, gid)
    os.chmod(data, 0o711)
    from . import secrets

    secrets_root = secrets.ensure_dir(secrets.secrets_dir())
    shared = _shared_dirs()
    read_only = [data / name for name in READ_ONLY_DIRS]
    for directory in read_only:
        share_read_only(directory)
    for child in data.iterdir():
        if child in shared or child in read_only or child == secrets_root or child.is_symlink():
            continue
        if first:
            for root, dirs, files in os.walk(child):
                for name in [*dirs, *files]:
                    item = Path(root) / name
                    if not item.is_symlink():
                        try:
                            os.chmod(item, stat.S_IMODE(item.stat().st_mode) & ~0o007)
                        except OSError:
                            pass
        try:
            os.chmod(child, stat.S_IMODE(child.stat().st_mode) & ~0o007)
        except OSError:
            pass
    for directory in shared:
        directory.mkdir(parents=True, exist_ok=True)
        if first:
            _group_tree(directory, gid)
        else:
            os.chown(directory, -1, gid)
            os.chmod(directory, 0o2770)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")
    _log.info("agent 以 %s 运行；工作区归其组可写，密钥目录只归后端用户", agent_user())
    return {"switching": True, "user": agent_user(), "first_run": first}
