"""评测数据目录 ``.ontology-state/eval-data``：不进工作区 git、不进知识包、不随导出/克隆外带。

- ``<file_id>/<原文件名>``：旧版评测数据源上传的业务数据表（Excel），已停止新增；
  发布时仍按旧配置读取表头生成数据映射（见 eval_data_sources、package_mapping）；
- 独立评测导入优化时，数据源表头也写在这个目录下（见 package_evaluations），供工具箱
  check_eval_data.py 对账。

目录内自带 ``*`` 的 .gitignore：模板 .gitignore 没排除 .ontology-state。
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

DATA_DIR_PARTS = (".ontology-state", "eval-data")
SUFFIXES = (".xlsx", ".xlsm")
MAX_FILES_PER_SOURCE = 20

_FILE_ID = re.compile(r"^[0-9a-f]{12}$")


def data_dir(root: Path) -> Path:
    return root.joinpath(*DATA_DIR_PARTS)


def _file_dir(root: Path, file_id: str) -> Path:
    folder = data_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("*\n", encoding="utf-8")
    target = folder / file_id
    target.mkdir()
    return target


def _entry(file_id: str, path: Path) -> dict:
    return {"id": file_id, "name": path.name, "size": path.stat().st_size}


def import_workspace_file(root: Path, rel: str) -> dict | None:
    """把工作区里的 Excel 复制进评测数据文件（旧配置迁移用）；文件不存在返回 None。"""
    source = (root / rel).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        return None
    if source.suffix.lower() not in SUFFIXES:
        return None
    file_id = uuid.uuid4().hex[:12]
    target = _file_dir(root, file_id) / source.name
    shutil.copy2(source, target)
    return _entry(file_id, target)


def path_of(root: Path, file_id: str) -> Path | None:
    if not _FILE_ID.match(str(file_id or "")):
        return None
    folder = data_dir(root) / file_id
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES)
    return files[0] if files else None
