"""网关里装了哪些转换器：读只读目录里每个连接器包的清单（`converter` 段），不导入转换器代码。

转换器代码只在任务子进程里按清单的入口导入（`worker`），网关进程本身不加载解析库。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..evidence.convert import ConverterInfo

_log = logging.getLogger(__name__)

DIR_ENV = "ONTO_CONNECTOR_CONVERTERS_DIR"
DEFAULT_DIR = "/opt/connector/converters"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$")
_TYPE_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


@dataclass(frozen=True)
class LoadedConverter:
    info: ConverterInfo
    directory: Path
    entry: str

    @property
    def entry_path(self) -> Path:
        return self.directory / self.entry


def _complete(directory: Path, entry: str, types: tuple) -> bool:
    """清单写全了：入口文件在、写法对，至少声明了一种类型且写法都对。"""
    if not _ENTRY_RE.match(entry) or not (directory / entry).is_file():
        return False
    return bool(types) and all(_TYPE_RE.match(item) for item in types)


def _load(directory: Path) -> LoadedConverter | None:
    data = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8")) or {}
    spec = data.get("converter")
    name = str(data.get("name") or "")
    if data.get("kind") != "provider" or not isinstance(spec, dict):
        return None
    if name != directory.name or not _NAME_RE.match(name):
        return None
    entry = str(spec.get("entry") or "")
    types = tuple(str(item).lower() for item in spec.get("types") or [])
    if not _complete(directory, entry, types):
        _log.warning("转换器 %s 的清单不完整，跳过", name)
        return None
    info = ConverterInfo(id=name, version=str(data.get("version") or "0.0.0"), label=str(data.get("label") or name),
                         types=types, license_note=str(spec.get("license") or ""))
    return LoadedConverter(info=info, directory=directory, entry=entry)


def load(base: Path | str | None = None) -> dict[str, LoadedConverter]:
    """{转换器 ID: 转换器}；目录不存在时为空（网关照常起，健康检查报 0 个）。"""
    root = Path(base or os.environ.get(DIR_ENV) or DEFAULT_DIR)
    found: dict[str, LoadedConverter] = {}
    if not root.is_dir():
        return found
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / "manifest.yaml").is_file():
            continue
        try:
            item = _load(directory)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _log.warning("读不了转换器清单 %s：%s", directory.name, exc)
            continue
        if item is not None:
            found[item.info.id] = item
    return found
