"""旧版评测的数据源配置（只读）。

旧版数据评测按项目保存数据源配置，落数据目录（0600，不进工作区 git、不随导出/克隆外带）。
新的评测在独立评测服务中配置数据源，这里不再新增或修改配置，保留读取有两个用途：

- 发布知识包时，配置引用的上传数据表仍作为数据映射的表头来源（见 package_mapping）；
- 删除项目时清除配置文件：里面存着连接凭据。

很早的配置用资料区 eval_data 标签文件（``resources`` 路径），读取时自动复制成上传文件并改写配置。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import config
from . import eval_data_files

_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class DataSourceError(ValueError):
    """项目 ID 不合法。"""


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def file_ids(source: dict) -> list[str]:
    return [f["id"] for f in source.get("files") or [] if isinstance(f, dict) and f.get("id")]


def _file(project_id: str) -> Path:
    if not _PROJECT_ID.match(project_id or ""):
        raise DataSourceError("项目 ID 不合法")
    return Path(config.DATA_DIR) / "eval-data-sources" / f"{project_id}.json"


def _load(project_id: str) -> list[dict]:
    path = _file(project_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [s for s in data.get("sources") or [] if isinstance(s, dict) and s.get("id")]


def _save(project_id: str, sources: list[dict]) -> None:
    path = _file(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _migrate_legacy(project_id: str, sources: list[dict], root: Path | None) -> list[dict]:
    """旧配置的 resources（资料区 eval_data 文件路径）→ 复制成上传文件并改写配置。

    源文件已不存在的路径直接丢弃；迁移只在拿得到项目目录时进行，结果立即落盘。
    """
    legacy = [s for s in sources if "resources" in s]
    if not legacy or root is None:
        return sources
    for source in legacy:
        files = list(source.get("files") or [])
        for rel in source.pop("resources") or []:
            entry = eval_data_files.import_workspace_file(root, _text(rel))
            if entry:
                files.append(entry)
        source["files"] = files[: eval_data_files.MAX_FILES_PER_SOURCE]
    _save(project_id, sources)
    return sources


def referenced_file_ids(project_id: str, root: Path | None = None) -> set[str]:
    return {i for s in list_sources(project_id, root) for i in file_ids(s)}


def list_sources(project_id: str, root: Path | None = None) -> list[dict]:
    """最近使用/更新的排在前面。给出 root 时顺带迁移旧配置。"""
    sources = _migrate_legacy(project_id, _load(project_id), root)
    sources.sort(key=lambda s: max(s.get("used_at", 0), s.get("updated_at", 0)), reverse=True)
    return sources


def purge_project(project_id: str) -> None:
    """删除项目时清除其全部数据源配置：里面存着连接凭据，不得留在盘上。"""
    try:
        path = _file(project_id)
    except DataSourceError:
        return
    for target in (path, path.with_suffix(".tmp")):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
