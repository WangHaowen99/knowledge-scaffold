"""工作区工具箱脚本在后端进程里的加载点（兼容入口）。

脚本契约、脚本描述符与 operation 调用关系以工具箱脚本为单一事实源：建模 agent 在工作区
`.claude/scripts/` 下直接运行它们，后端导出与 Web 保存也调用同一份实现。加载统一走共享契约库
（services/packs/contracts.py，方案 A16）：这里给的是镜像自带的版本，要按项目锁定版本时用
`contracts.for_project(root)`。
"""

from __future__ import annotations

from types import ModuleType

from .packs import contracts as _contracts


def _load(name: str) -> ModuleType:
    return _contracts.default().load(name)


operation_relations = _load("operation_relations")
script_argument_shape = _load("script_argument_shape")
script_contract = _load("script_contract")
script_descriptor = _load("script_descriptor")
source_extract = _load("source_extract")
