"""后端取元模型的入口：加载工具箱里的 `onto_metamodel.py`（详设 4.4、方案 C3）。

元模型只在工具箱脚本里定义一份，建模 agent 在工作区直接用它，后端按文件加载同一份：按原名注册进
sys.modules，工具箱脚本里的 `import onto_metamodel` 与后端拿到的是同一个模块对象。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .. import config

MODULE_NAME = "onto_metamodel"


def _load() -> ModuleType:
    path = (Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / f"{MODULE_NAME}.py").resolve()
    loaded = sys.modules.get(MODULE_NAME)
    if loaded is not None and Path(getattr(loaded, "__file__", "") or "").resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


mm = _load()
