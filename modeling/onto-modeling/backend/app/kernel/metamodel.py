"""后端取元模型的入口（详设 4.4、方案 C3）：元模型只在工具箱的 `onto_metamodel.py` 里定义一份，
经共享契约库按版本加载（方案 A16、D5），和建模 agent 在工作区里用的是同一份：

- `for_project(root)`：项目锁定的 core 版本（装配目录里核对过的那份）；
- `stable()`：没有项目上下文的接口（全局 C-05 `/metamodel`、O-42 版本清单）用 stable 通道指向的 core 版本；
- `mm`：模块级常量（实体类型、目录这类导入时就要的结构）与拿不到项目根目录的辅助函数用镜像自带的
  版本，与 `services/toolkit.py` 同口径。

契约库里的脚本之间按同目录模块名 import（`import onto_metamodel`），由契约库解析到同一版本。
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType

from .. import config
from ..services.packs import contracts

_log = logging.getLogger(__name__)

MODULE_NAME = "onto_metamodel"


def for_project(root: Path) -> ModuleType:
    """项目锁定版本的元模型；项目没有装配目录或锁定的版本里没有它时用镜像版本。"""
    return contracts.module_for(root, MODULE_NAME)


def stable() -> ModuleType:
    """stable 通道指向的 core 版本的元模型；包仓库里还没有 core 或该版本没有元模型时用镜像版本。"""
    from ..services.packs.repository import PackRepository

    try:
        return contracts.for_stable(PackRepository(config.DATA_DIR)).load(MODULE_NAME)
    except ImportError as exc:
        _log.warning("stable 版本的元模型不可用，改用镜像版本：%s", exc)
        return contracts.default().load(MODULE_NAME)


mm = contracts.default().load(MODULE_NAME)
