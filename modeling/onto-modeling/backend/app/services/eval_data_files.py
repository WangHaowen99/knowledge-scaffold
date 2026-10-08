"""评测数据文件：数据源里手动上传的业务数据表（Excel）。

文件落在项目工作区 ``.ontology-state/eval-data/<file_id>/<原文件名>``：

- 不进工作区 git（目录内自带 ``*`` 的 .gitignore——模板 .gitignore 没排除
  .ontology-state，评测结束的 ``git add -A`` 会把状态文件一并提交）、不进知识包、
  不随导出/克隆外带（与数据源配置同一生命周期）；
- 仍在工作区内，评测优化时 agent 可用工具箱 check_eval_data.py 直接对账；
- 数据源配置只存文件 ID，删除配置时回收不再被引用的文件。

每个文件一个目录，目录名即 ID，文件名保持上传时的原名，展示与对账都用得上。
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from pathlib import Path

DATA_DIR_PARTS = (".ontology-state", "eval-data")
SUFFIXES = (".xlsx", ".xlsm")
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_SOURCE = 20
# 上传后未保存进任何数据源（关掉了表单）的文件，超过此时长才回收，
# 避免把正在编辑、尚未保存的表单里的文件删掉
ORPHAN_TTL_SECONDS = 24 * 3600

_FILE_ID = re.compile(r"^[0-9a-f]{12}$")


class DataFileError(ValueError):
    """上传或引用的评测数据文件不合法（接口层映射为 400）。"""


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


def _safe_name(filename: str) -> str:
    name = Path(str(filename or "").replace("\\", "/")).name.strip()
    if not name or name.startswith("."):
        raise DataFileError("无效的文件名")
    if Path(name).suffix.lower() not in SUFFIXES:
        raise DataFileError("评测数据仅支持 Excel 表（.xlsx / .xlsm）")
    return name[:120]


def _entry(file_id: str, path: Path) -> dict:
    return {"id": file_id, "name": path.name, "size": path.stat().st_size}


def save(root: Path, filename: str, data: bytes) -> dict:
    """保存一份上传的 Excel，返回 {id, name, size}。"""
    name = _safe_name(filename)
    if not data:
        raise DataFileError("上传的文件为空")
    if len(data) > MAX_FILE_BYTES:
        raise DataFileError(f"单个评测数据文件不能超过 {MAX_FILE_BYTES // 1024 // 1024} MB")
    file_id = uuid.uuid4().hex[:12]
    target = _file_dir(root, file_id) / name
    target.write_bytes(data)
    return _entry(file_id, target)


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


def describe(root: Path, file_ids: list) -> list[dict]:
    """文件 ID → [{id, name, size}]；ID 非法或文件已不存在时抛 DataFileError。"""
    ids = [str(i).strip() for i in (file_ids or []) if str(i).strip()]
    if len(ids) > MAX_FILES_PER_SOURCE:
        raise DataFileError(f"每个数据源最多 {MAX_FILES_PER_SOURCE} 份评测数据文件")
    entries = []
    for file_id in dict.fromkeys(ids):
        path = path_of(root, file_id)
        if path is None:
            raise DataFileError("评测数据文件不存在或已被删除，请重新上传")
        entries.append(_entry(file_id, path))
    return entries


def all_files(root: Path) -> list[Path]:
    folder = data_dir(root)
    if not folder.is_dir():
        return []
    paths = []
    for child in sorted(folder.iterdir()):
        path = path_of(root, child.name) if child.is_dir() else None
        if path is not None:
            paths.append(path)
    return paths


def remove(root: Path, file_id: str) -> None:
    if _FILE_ID.match(str(file_id or "")):
        shutil.rmtree(data_dir(root) / file_id, ignore_errors=True)


def prune(root: Path, referenced: set[str], min_age: float = ORPHAN_TTL_SECONDS) -> list[str]:
    """回收没有被任何数据源引用的文件；min_age=0 时立即回收。返回回收的 ID。"""
    folder = data_dir(root)
    if not folder.is_dir():
        return []
    now = time.time()
    removed = []
    for child in folder.iterdir():
        if not child.is_dir() or not _FILE_ID.match(child.name) or child.name in referenced:
            continue
        if now - child.stat().st_mtime < min_age:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    return removed
