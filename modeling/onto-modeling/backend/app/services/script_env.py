"""后端用子进程跑工作区 / 工具箱脚本时给的最小环境。

这些脚本从工作区的 `.claude/` 取（现在指向资产目录，拆包后可能由租户包提供），不能在
后端进程的身份下拿到模型 Key、JWT 与各类 ONTO_WEB_* 凭据。只透传运行 python3、git、
bash 必需的变量，其余一律不带。
"""

from __future__ import annotations

import os

# HOME：git 读全局配置要用；TMPDIR/TEMP/TMP、SYSTEMROOT：临时目录与 Windows 开发机上起 Python 所需
_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")

_FIXED = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def minimal_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _PASSTHROUGH if os.environ.get(key)}
    env.setdefault("PATH", os.defpath)
    env.update(_FIXED)
    return env
