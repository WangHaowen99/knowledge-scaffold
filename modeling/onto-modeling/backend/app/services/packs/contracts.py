"""共享契约库加载器（方案 A16、D5；详设 5.7、8.1 第 5 步）。

后端与工作区脚本共用同一份契约实现（脚本契约、描述符、入参形态、操作关系、求解评估器契约、
资料分析验收、jiuwenbox 客户端、流程探针）。原来 toolkit.py、solver_contract、workspace 三处
各自加载、按裸模块名缓存在 sys.modules 里，只认镜像里的那一份，上传资产包后要重启才换——
这是「三版本并存」里后端那一份的来源。这里合成一个加载器：

- 按版本加载：项目用它装配目录里锁定的 core 版本（P2 拆出 contracts 包后改按 contracts 的
  版本），不在项目上下文里时用镜像自带的版本。项目切到新版本后，下一次调用就用新版本，
  和 agent 看到的是同一份；不再需要重启。
- 模块名带版本：`onto_contracts.<版本标签>.<脚本名>`，同一进程里多个版本互不干扰。脚本之间
  按同目录模块名 import（`from script_contract import ...`），用模块自己的 `__import__` 解析到
  同一版本的兄弟模块，不往 sys.path、不往裸模块名里塞东西。
- 只加载哈希核对过的文件：装配目录里的脚本按 MANIFEST.json 核对，对不上就退回镜像版本。
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from types import ModuleType

from ... import config
from .repository import file_sha256

_log = logging.getLogger(__name__)

# 契约库里的脚本（P2 拆包后就是 contracts 包的内容）
NAMES = (
    "operation_relations", "script_argument_shape", "script_contract", "script_descriptor",
    "solver_evaluator_contract", "research_review", "jiuwenbox_client", "flow_state",
)
_LOCK = threading.RLock()
_LIBRARIES: dict[str, "ContractLibrary"] = {}


def _tag(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")


class ContractLibrary:
    """一个版本的契约库：属性访问即加载同名脚本（`lib.script_contract`）。"""

    def __init__(self, scripts_dir: Path, tag: str, files: dict[str, str] | None = None):
        self.scripts_dir = Path(scripts_dir)
        self.tag = tag
        self.prefix = f"onto_contracts.{_tag(tag)}"
        # 装配目录的文件哈希（相对 scripts/ 的路径 → sha256）；None 表示镜像目录，不核对
        self._files = files
        self._modules: dict[str, ModuleType] = {}

    def __getattr__(self, name: str) -> ModuleType:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.load(name)

    def path(self, name: str) -> Path:
        return self.scripts_dir / f"{name}.py"

    def load(self, name: str) -> ModuleType:
        with _LOCK:
            module = self._modules.get(name)
            if module is not None:
                return module
            path = self.path(name)
            if not path.is_file():
                raise ImportError(f"契约库 {self.tag} 里没有 {name}")
            if self._files is not None:
                expected = self._files.get(f"scripts/{name}.py")
                if not expected or file_sha256(path) != expected:
                    raise ImportError(f"契约库 {self.tag} 的 {name} 与装配清单不一致")
            qualified = f"{self.prefix}.{name}"
            spec = importlib.util.spec_from_file_location(qualified, path)
            module = importlib.util.module_from_spec(spec)
            module.__dict__["__builtins__"] = self._builtins()
            # dataclass 等按 sys.modules[模块名] 回查，执行前登记
            sys.modules[qualified] = module
            self._modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(qualified, None)
                self._modules.pop(name, None)
                raise
            return module

    def _builtins(self) -> dict:
        real_import = builtins.__import__
        library = self

        def sibling_import(name, globals=None, locals=None, fromlist=(), level=0):
            # 同目录的兄弟脚本（不带点的模块名、目录里有同名 .py）解析到同一版本；其余照常导入
            if level == 0 and name and "." not in name and name not in sys.builtin_module_names \
                    and library.path(name).is_file():
                return library.load(name)
            return real_import(name, globals, locals, fromlist, level)

        namespace = dict(builtins.__dict__)
        namespace["__import__"] = sibling_import
        return namespace


# 镜像目录在进程启动时定下（镜像不可变；与原来模块导入时加载的行为一致，测试改 ASSETS_DIR 不影响它）
_IMAGE_SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


def _image_scripts() -> Path:
    return _IMAGE_SCRIPTS


def _image_tag(scripts_dir: Path) -> str:
    hasher = hashlib.sha256()
    for name in NAMES:
        path = scripts_dir / f"{name}.py"
        if path.is_file():
            hasher.update(name.encode() + b"\0" + path.read_bytes())
    return f"image-{hasher.hexdigest()[:8]}"


def default() -> ContractLibrary:
    """镜像自带的版本：不在项目上下文里（全局设置、网关状态、模块级常量）时用它。"""
    scripts = _image_scripts()
    key = f"image:{scripts.resolve()}"
    with _LOCK:
        library = _LIBRARIES.get(key)
        if library is None:
            library = ContractLibrary(scripts, _image_tag(scripts))
            _LIBRARIES[key] = library
        return library


def for_project(root: Path) -> ContractLibrary:
    """项目锁定版本的契约库：项目 `.claude` 指向的装配目录里的 core 版本。

    没有装配目录（旧工作区、测试里的最小工作区）或清单读不出时用镜像版本。
    """
    link = Path(root) / ".claude"
    if not os.path.lexists(link):
        return default()
    real = Path(os.path.realpath(link))
    if real.parent.name != "assembled":
        return default()
    try:
        manifest = json.loads((real / "MANIFEST.json").read_text(encoding="utf-8"))
        core = next(pack for pack in manifest.get("packs") or [] if pack.get("name") == "core")
    except (OSError, ValueError, StopIteration):
        return default()
    key = f"core:{core.get('version')}:{str(core.get('sha256'))[:16]}"
    with _LOCK:
        library = _LIBRARIES.get(key)
        # 同一版本的装配目录可能被清掉重建（内容被改过、换了数据目录）：目录不在了就按当前的重建
        if library is None or not library.scripts_dir.is_dir():
            library = ContractLibrary(real / "scripts", f"core-{core.get('version')}-{str(core.get('sha256'))[:8]}",
                                      files=manifest.get("files") or {})
            _LIBRARIES[key] = library
        return library


def module_for(root: Path, name: str) -> ModuleType:
    """项目锁定版本的某个契约脚本；装配目录里的文件对不上时退回镜像版本并记日志。"""
    try:
        return for_project(root).load(name)
    except ImportError as exc:
        _log.warning("项目 %s 的契约库 %s 不可用，改用镜像版本：%s", Path(root).name, name, exc)
        return default().load(name)
