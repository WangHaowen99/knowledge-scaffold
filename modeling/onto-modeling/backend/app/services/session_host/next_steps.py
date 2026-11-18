"""统一下一步的宿主侧入口（详设 6.10「三个出口用同一份结果」；方案 D15、D16）。

宿主注入（两个引擎）在助手的工作目录（变更集副本）里跑一次 `state_helper.py suggest`，和助手命令收尾时
自己跑的是同一个命令、读同一份 `.onto/run/flow-state.json`（后端按执行记录、变更集、指纹算好写进去的）、
用同一个算法（core 的 `flow_state.suggest`），所以注入给模型的「下一步」与界面建议条（FL-02）、助手收尾时自己查的
一致（助手只拿来判断，不在回复里列出）。跑装配目录里哈希核对过的那份脚本（方案 D19）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ... import config
from ..script_env import minimal_env

TIMEOUT_SECONDS = 15


def suggest(root: Path) -> dict | None:
    """在 root（助手副本或主线工作区）里跑 `state_helper.py suggest`；跑不起来、输出读不出时返回 None。"""
    from ..packs.assembler import trusted_script

    script = trusted_script(Path(root), "state_helper.py", Path(config.ASSETS_DIR))
    try:
        proc = subprocess.run(  # NOSONAR(S4721) 跑核对过的工具箱脚本，参数列表不经 shell，最小环境变量
            ["python3", str(script), "suggest"], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS, env=minimal_env())
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
