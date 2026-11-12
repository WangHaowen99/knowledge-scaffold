"""评测移出的启动迁移（ADR-0020）：一次性、幂等，下个大版本整体删除本模块。

评测移出后，评测数据源里上传的业务数据表不再是发布的表来源。被对象引用的那部分不迁走，
存量项目的发布绑定就会对不上。启动时对每个项目：

1. **迁表**：``.ontology-state/eval-data/<文件ID>/`` 下的上传表，凡有 Sheet 被对象的数据绑定
   用到（「# 数据来源」声明或按列推断），复制进 ``resource/excel/``，在 materials.json 标为
   schema，只提交这些路径；
2. **清状态**：删掉 .ontology-state 下的评测数据（eval-data/、solver-validation-cases.json），以及项目元数据里的
   Modeling 关联字段。最近一轮的状态 eval.json 留着，「旧版评测记录」只读显示它（ADR-0092）。

全部项目处理完，删掉数据目录下的评测数据源配置（eval-data-sources/，含连接密码）与 Modeling
清理队列。单个项目迁表失败只记运维错误日志，它的评测数据留到下次启动再迁；数据源配置不等它，
照删——密码不能因为别的项目迁移失败而留在盘上。判断"引用了哪些上传表"看对象绑定，不依赖
数据源配置；配置只用来决定同名 Sheet 的先后（被配置引用的上传表优先，与移出前的发布口径一致）。

迁过的项目再跑一遍什么都不做：评测数据已删；即使删到一半，复制过的表字节相同、用途已是
schema，不会重复提交。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import error_log, package_mapping
from .credential_store import purge_paths, read_json

logger = logging.getLogger(__name__)

LEGACY_UPLOAD_DIR = (".ontology-state", "eval-data")
LEGACY_STATE_PATHS = (
    ".ontology-state/eval-data",
    ".ontology-state/solver-validation-cases.json",
)
LEGACY_DATA_PATHS = ("eval-data-sources", "modeling-cleanup-tasks.json")
LEGACY_META_FIELDS = ("modeling_project_id", "modeling_package_digest", "modeling_package_revision")
UPLOAD_SUFFIXES = (".xlsx", ".xlsm")
TARGET_DIR = "resource/excel"
COMMIT_MESSAGE = "chore: 评测上传的数据表迁入资料区（标为 schema）"
_FILE_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def migrate(manager) -> dict:
    """迁移全部项目并删除数据目录里的评测配置；返回 {migrated: {项目: [路径]}, failed: [...]}。"""
    data_dir = manager.workspaces_dir.parent
    referenced = _referenced_uploads(data_dir / LEGACY_DATA_PATHS[0])
    report: dict = {"migrated": {}, "failed": []}
    for root in manager._project_dirs():
        try:
            moved = migrate_project(manager, root.name, referenced.get(root.name, set()))
        except Exception as exc:  # 一个项目失败不拖累其他项目，也不拦启动
            logger.exception("评测上传表迁移失败：%s", root.name)
            error_log.log(root.name, "eval_retirement", f"评测上传表迁移失败，下次启动重试：{exc}")
            report["failed"].append(root.name)
            continue
        if moved:
            report["migrated"][root.name] = moved
            logger.info("评测上传表已迁入资料区：%s %s", root.name, moved)
    leftovers = purge_paths([data_dir / rel for rel in LEGACY_DATA_PATHS])
    if leftovers:
        logger.error("评测数据源配置未能删除：%s", leftovers)
        error_log.log("", "eval_retirement", f"评测数据源配置未能删除，请手工删除：{'、'.join(leftovers)}")
    return report


def migrate_project(manager, project_id: str, referenced_ids: set[str] | None = None) -> list[str]:
    """迁一个项目：被引用的上传表进资料区并提交，再删评测状态。返回迁入的工作区相对路径。"""
    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        uploads = _uploads(root, referenced_ids or set())
        moved = _migrate_tables(manager, root, uploads) if uploads else []
        leftovers = purge_paths([root / rel for rel in LEGACY_STATE_PATHS])
        if leftovers:
            raise OSError(f"评测状态未能删除：{'、'.join(leftovers)}")
        meta = manager._read_meta(root)
        if any(field in meta for field in LEGACY_META_FIELDS):
            for field in LEGACY_META_FIELDS:
                meta.pop(field, None)
            manager._write_meta(root, meta)
        return moved


def _referenced_uploads(folder: Path) -> dict[str, set[str]]:
    """数据源配置 → {项目 ID: 被引用的上传文件 ID}。读不到的配置按没有引用处理。"""
    found: dict[str, set[str]] = {}
    if not folder.is_dir():
        return found
    for path in folder.glob("*.json"):
        data = read_json(path, {})
        sources = data.get("sources") if isinstance(data, dict) else None
        ids = set()
        for source in sources or []:
            if not isinstance(source, dict):
                continue
            for item in source.get("files") or []:
                if isinstance(item, dict) and item.get("id"):
                    ids.add(str(item.get("id")))
        if ids:
            found[path.stem] = ids
    return found


def _uploads(root: Path, referenced_ids: set[str]) -> list[tuple[str, Path]]:
    """上传表 [(文件 ID, 路径)]：被数据源配置引用的在前，各自按文件 ID 排序。"""
    folder = root.joinpath(*LEGACY_UPLOAD_DIR)
    if not folder.is_dir():
        return []
    found = []
    for child in sorted(folder.iterdir()):
        if not child.is_dir() or child.is_symlink() or not _FILE_ID_RE.match(child.name):
            continue
        files = sorted(
            path for path in child.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in UPLOAD_SUFFIXES
        )
        if files:
            found.append((child.name, files[0]))
    return sorted(found, key=lambda item: (item[0] not in referenced_ids, item[0]))


def _migrate_tables(manager, root: Path, uploads: list[tuple[str, Path]]) -> list[str]:
    from .workspace import RESOURCE_DIRECTIVES, _commit_paths

    schema_tables = package_mapping.read_source_tables(root)
    upload_tables: dict[str, list[str]] = {}
    providers: dict[str, tuple[str, Path]] = {}
    for file_id, path in uploads:
        for name, headers in package_mapping.read_table_headers(path).items():
            if name in schema_tables or name in upload_tables:
                continue  # 同名以先读到的为准：schema 资料优先，与移出前的发布口径一致
            upload_tables[name] = headers
            providers[name] = (file_id, path)
    if not upload_tables:
        return []
    try:
        binding = package_mapping.resolve_object_tables(
            root, tables={**schema_tables, **upload_tables}, strict_declared=False,
        )
        used = {item["table"] for item in binding["objects"].values() if item.get("table")}
    except (OSError, ValueError):
        # 对象文档读不了时判断不出引用：宁可多迁，不丢表
        used = set(upload_tables)
    needed = list(dict.fromkeys(providers[name] for name in sorted(used) if name in providers))
    if not needed:
        return []
    directives = manager._resource_directives_at(root)
    before = json.dumps(directives, sort_keys=True)
    moved = []
    for file_id, path in needed:
        rel = _place(manager, root, file_id, path)
        moved.append(rel)
        directive = directives.get(rel) if isinstance(directives.get(rel), dict) else {}
        if directive.get("purpose") != "schema":
            directives[rel] = {"purpose": "schema", "reading_mode": directive.get("reading_mode", "on_demand")}
    if json.dumps(directives, sort_keys=True) != before:
        manager._write_resource_directives(root, directives)
    _commit_paths(root, [*moved, RESOURCE_DIRECTIVES], COMMIT_MESSAGE)
    return moved


def _place(manager, root: Path, file_id: str, source: Path) -> str:
    """复制到 resource/excel/：同名文件字节相同就沿用，不同就在文件名后加文件 ID。"""
    data = source.read_bytes()
    folder = root / TARGET_DIR
    candidates = [source.name, f"{source.stem}-{file_id}{source.suffix}"]
    candidates += [f"{source.stem}-{file_id}-{index}{source.suffix}" for index in range(2, 10)]
    for name in candidates:
        target = folder / name
        if target.is_symlink():
            continue
        if not target.exists():
            manager._atomic_write(target, data)
            return f"{TARGET_DIR}/{name}"
        if target.is_file() and target.read_bytes() == data:
            return f"{TARGET_DIR}/{name}"
    raise OSError(f"{TARGET_DIR} 里同名文件过多，无法放下 {source.name}")
