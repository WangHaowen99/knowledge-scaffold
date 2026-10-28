"""工作区工具箱脚本（assets/workspace-toolkit/scripts）在后端进程里的加载点。

脚本契约、脚本描述符与 operation 调用关系以工具箱脚本为单一事实源：建模 agent 在工作区
`.claude/scripts/` 下直接运行它们，后端导出与 Web 保存也调用同一份实现。这些脚本按同目录
模块名互相 import，这里按原名注册进 sys.modules 再加载（不改 sys.path），保证后端各处与
脚本内部拿到的是同一份类与函数。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .. import config

SCRIPTS_DIR = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


def _load(name: str) -> ModuleType:
    path = (SCRIPTS_DIR / f"{name}.py").resolve()
    loaded = sys.modules.get(name)
    if loaded is not None and Path(getattr(loaded, "__file__", "") or "").resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 依赖顺序：script_descriptor 在模块顶层 import 前三者
operation_relations = _load("operation_relations")
script_argument_shape = _load("script_argument_shape")
script_contract = _load("script_contract")
script_descriptor = _load("script_descriptor")
