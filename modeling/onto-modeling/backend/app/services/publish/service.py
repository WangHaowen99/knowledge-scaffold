"""发布流程（方案 A8、A9；详设 ⑱；API 契约 3.20、3.21）。

确定提交 → git archive 只读树（tree.release_tree）→ 门禁（gates）→ 适配器渲染 →
留存包（release_store，先留存再打标签，留存失败就不算发布）→ 打 release/<版本> 注释标签 →
写发布记录（项目元数据 last_package_publish）。工作区里没提交的改动永远不进包。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

from .. import package_mapping, release_store
from .. import solver as solver_service
from ..workspace.errors import WorkspaceError
from ..workspace.files import unmarked_table_resources
from ..workspace.gitops import _git, _head_commit, _run_git
from ..workspace.layout import _content_revision
from . import gates, versioning
from .ontology_package import ADAPTER, VARIANT_GRAPH, VARIANT_STANDARD, resolve_binding
from .spi import ReleaseSource
from .tree import release_tree, uncommitted_package_changes

CHECK_LIMIT = 50
CHECK_TABLE_LIMIT = 100
VARIANTS = (VARIANT_STANDARD, VARIANT_GRAPH)


def resolve_commit(root: Path, commit: str | None) -> str:
    """发布的来源提交：缺省主线最新；给定时必须是主线（HEAD 的 first-parent 链）上的提交。"""
    if not commit:
        return _head_commit(root)
    if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit):
        raise WorkspaceError("版本号不合法", 400, "invalid_commit")
    found = _run_git(root, ("rev-parse", "-q", "--verify", f"{commit}^{{commit}}"))
    full = found.stdout.strip()
    if found.returncode != 0 or not full:
        raise WorkspaceError("版本不存在", 400, "invalid_commit")
    if full not in _git(root, "rev-list", "--first-parent", "HEAD").split():
        raise WorkspaceError("只能发布版本历史里的版本", 400, "invalid_commit")
    return full


def _source(manager, project_id: str, root: Path, tree: Path, commit: str, version: str, **options) -> ReleaseSource:
    # 不经 get_project：发布在项目锁内调用，再取一次锁会和自己的文件锁互斥
    return ReleaseSource(
        project_id=project_id, commit=commit, tree=tree, meta=manager._read_meta(root),
        version=version, state_root=root, **options,
    )


# ---- 渲染 ----


def export_package_zip(
    manager,
    project_id: str,
    dest_path: Path,
    binding: dict | None = None,
    graph: bool = False,
    allow_unbound: bool = False,
    *,
    source_root: Path | None = None,
    version: str = "",
    source_commit: str = "",
    draft: bool = False,
) -> Path:
    """按给定的树渲染一份包（不打标签、不留存）：source_root 缺省为工作区当前文件。

    发布与按版本补渲染走 publish_package / release_package；这里给预览、导出与测试用。
    version 缺省按主线版本号取 0.{n}.0。
    """
    root = manager.project_root(project_id)
    if not version:
        from .. import mainline

        version = f"0.{mainline.count(root)}.0"
    src = _source(
        manager, project_id, root, source_root or root, source_commit, version,
        variant=VARIANT_GRAPH if graph else VARIANT_STANDARD,
        allow_unbound=allow_unbound, draft=draft, binding=binding,
    )
    ADAPTER.render(src, Path(dest_path))
    return Path(dest_path)


# ---- 发布前检查（PB-01） ----


def package_binding_check(manager, project_id: str, commit: str | None = None) -> dict:
    """发布前体检：阻断项在前，附修复用的表清单、对象文档版本号、门禁链与最近一次发布记录。

    与发布同口径：绑定与求解门禁都基于提交导出的只读树，另列出包相关目录里不会进包的未提交改动。
    修复句柄里的文档版本号取工作区文件（修复写的是工作区）。条目数有上限，超出时 truncated=True。
    """
    started = time.time()
    root = manager.project_root(project_id)
    commit = resolve_commit(root, commit)
    tree_error = None
    binding = {"issues": [], "warnings": [], "tables": {}, "objects": {}, "problems": []}
    table_resources, solver = [], {"enabled": False}
    script_findings: list[dict] = []
    pack_gates: list[dict] = []
    try:
        with release_tree(root, commit) as tree:
            binding = package_mapping.resolve_object_tables(tree, tables=package_mapping.read_source_tables(tree))
            table_resources = unmarked_table_resources(tree, CHECK_LIMIT)
            solver = solver_service.release_status_at(tree, root)
            src = _source(manager, project_id, root, tree, commit, versioning.next_version(root, commit)[0])
            script_findings = gates.render_findings(src)
        pack_gates = _pack_gates(manager, root, commit)
    except WorkspaceError as exc:
        if exc.code not in ("release_tree_unsafe", "release_tree_mismatch"):
            raise
        tree_error = {"code": exc.code, "level": "error", "message": str(exc)}
    issues = sorted(binding["issues"], key=lambda item: not item["blocking"])
    issue_messages = {item["message"] for item in issues}
    warnings = [w for w in binding["warnings"] if w not in issue_messages]
    tables = sorted(binding["tables"])
    shown = []
    for item in issues[:CHECK_LIMIT]:
        rel = f"ontology/object/{item['object']}.md"
        doc = root / rel
        revision = _content_revision(doc.read_bytes()) if doc.is_file() else ""
        shown.append({**item, "path": rel, "revision": revision})
    objects = binding["objects"]
    uncommitted = uncommitted_package_changes(root)
    gate_list = gates.gate_chain(issues, solver, script_findings, tree_error, pack_gates)
    return {
        "commit": commit,
        "blocking": sum(1 for item in issues if item["blocking"]),
        "issues": shown,
        "warnings": warnings[:CHECK_LIMIT],
        "tables": tables[:CHECK_TABLE_LIMIT],
        "table_count": len(tables),
        "objects": len(objects),
        "bound_objects": sum(1 for item in objects.values() if item["table"]),
        "truncated": len(issues) > CHECK_LIMIT or len(warnings) > CHECK_LIMIT or len(tables) > CHECK_TABLE_LIMIT,
        "last_publish": manager.get_project(project_id).get("last_package_publish"),
        "table_resources": table_resources,
        # 求解契约体检单列：blocking 仍只计数据绑定，普通包这里只有 enabled=False
        "solver": solver,
        "gates": gate_list,
        "format": "solver" if solver.get("enabled") else "ontology",
        "uncommitted": uncommitted[:CHECK_LIMIT],
        "uncommitted_count": len(uncommitted),
        "checked_at": time.time(),
        "duration_ms": int((time.time() - started) * 1000),
    }


def _pack_gates(manager, root: Path, commit: str) -> list[dict]:
    """包声明、发布前查的门禁（ADR-0068）；读包声明出错不挡发布前检查，记日志。"""
    from ..packs import extensions

    try:
        return extensions.publish_gates(manager, root, commit, 0)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("包声明的发布门禁没跑起来", exc_info=True)
        return []


def set_object_data_source(
    manager, project_id: str, key: str, table: str, base_revision: str = "", author: str = "",
) -> dict:
    """修复入口：把对象「# 数据来源」声明改成现有数据表之一，table 为空表示「无」。"""
    root = manager.project_root(project_id)
    rel = f"ontology/object/{key}.md"
    if not key or "/" in key or "\\" in key:
        raise WorkspaceError("对象不存在", 404)
    if not manager._resolve_inside(root, rel).is_file():
        raise WorkspaceError("对象不存在", 404)
    doc = root / rel
    table = (table or "").strip()
    if table:
        by_lower = {name.lower(): name for name in package_mapping.read_source_tables(root)}
        if table.lower() not in by_lower:
            raise WorkspaceError(f"数据表「{table}」不在 schema 资料里", 400, "invalid_argument")
        table = by_lower[table.lower()]
    content = package_mapping.set_declared_source(doc.read_text(encoding="utf-8"), table)
    return manager.write_file(project_id, rel, content, base_revision=base_revision, author=author)


# ---- 发布（PB-03） ----


def publish_package(
    manager,
    project_id: str,
    dest_path: Path | None = None,
    allow_unbound: bool = False,
    author: str = "",
    commit: str | None = None,
) -> dict:
    """用户发布知识包：按提交（缺省主线最新）打包、留存、打发布标签，返回发布记录（同时写进元数据）。

    版本号见 versioning：该提交已有发布标签就沿用（同一提交重复发布，版本号与包字节都相同），
    否则 0.{n}.0 且严格大于已有的全部发布标签。降级发布须可追溯：记录谁、何时发布，以及哪些
    对象因此未绑定数据表。求解知识包另有门禁：静态检查无错误，且当前修订有通过的权威验证回执；
    allow_unbound 只放宽数据绑定，不放宽求解门禁。dest_path 给定时把留存的包复制过去。
    """
    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        commit = resolve_commit(root, commit)
        version, existing = versioning.next_version(root, commit)
        tag = versioning.tag_name(version)
        with tempfile.TemporaryDirectory(prefix="onto-publish-") as scratch:
            rendered = Path(scratch) / "package.zip"
            with release_tree(root, commit) as tree:
                solver_service.ensure_publishable(tree, root)
                src = _source(manager, project_id, root, tree, commit, version, allow_unbound=allow_unbound)
                src = dataclasses.replace(src, binding=resolve_binding(src))
                manifest = ADAPTER.render(src, rendered)
            degraded = manifest.unbound_objects
            # 留存发布包，供按版本下载（ADR-0020）：先留存再打标签，留存失败就不算发布。
            # 同一版本已有留存时以留存为准，这次拿到的也是那份字节
            stored = release_store.save(manager.data_dir, project_id, version, rendered, {
                "commit": commit, "tag": tag, "released_at": int(time.time()), "released_by": author,
                "degraded": bool(degraded), "unbound_count": len(degraded),
                "unbound_objects": degraded[:CHECK_LIMIT],
            })
        if dest_path is not None:
            shutil.copyfile(release_store.get(manager.data_dir, project_id, version, commit)[0], dest_path)
        if not existing:
            _git(root, "tag", "-a", tag, "-m", versioning.release_message(version, author, degraded), commit)
        uncommitted = uncommitted_package_changes(root)
        record = {
            "at": int(time.time()),
            "by": author,
            "degraded": bool(degraded),
            "unbound_count": len(degraded),
            "unbound_objects": degraded[:CHECK_LIMIT],
            "version": version,
            "commit": commit,
            "tag": tag,
            "format": stored.get("format"),
            "sha256": stored.get("sha256"),
            "size": stored.get("size"),
            "uncommitted": uncommitted[:CHECK_LIMIT],
            "uncommitted_count": len(uncommitted),
        }
        manager._update_meta_unlocked(root, last_package_publish=record)
    return record


# ---- 发布记录、按版本取包、求解验证候选（三份契约，ADR-0020） ----


def releases(manager, project_id: str) -> list[dict]:
    """发布记录：release/* 标签，版本号从新到旧。

    标签给出版本、提交、发布时间、发布人与是否降级；留存记录补上格式、包摘要与拉取记录
    （留存功能之前打的标签没有留存，格式与摘要为 None，第一次下载补存后才有）。
    """
    root = manager.project_root(project_id)
    out = _git(
        root, "for-each-ref", f"refs/tags/{versioning.TAG_PREFIX}",
        "--format=%(refname:short)%1f%(*objectname)%(objectname)%1f%(taggerdate:unix)%1f%(contents)%1e",
    )
    stored = release_store.records(manager.data_dir, project_id)
    entries = []
    for record in out.split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) != 4 or not (number := versioning.parse_tag(parts[0])):
            continue
        version = versioning.text(number)
        commit = parts[1][:40]
        by, degraded = versioning.parse_release_message(parts[3])
        package = stored.get(version)
        if package and package.get("commit") != commit:
            package = None
        entries.append({
            "number": number,
            "version": version,
            "tag": parts[0],
            "commit": commit,
            "released_at": int(parts[2]) if parts[2].isdigit() else None,
            "released_by": by,
            "degraded": degraded,
            "unbound_objects": list((package or {}).get("unbound_objects") or []),
            "format": package.get("format") if package else None,
            "package": {"sha256": package["sha256"], "size": package["size"]} if package else None,
            "pulls": list((package or {}).get("pulls") or []),
        })
    entries.sort(key=lambda item: item.pop("number"), reverse=True)
    return entries


def release_package(manager, project_id: str, version: str, variant: str = VARIANT_STANDARD) -> tuple[Path, dict]:
    """按版本取发布包：读发布时留存的包。

    标准包留存缺失（数据目录丢失、留存功能之前的旧标签）时，按标签提交重新渲染一次并补存，记录标
    backfilled；图模式变体第一次下载时按同一提交渲染并留存。之后同一版本同一变体都读这一份。
    """
    if not versioning.VERSION_RE.match(version or ""):
        raise WorkspaceError("版本号不合法", 400, "invalid_argument")
    if variant not in VARIANTS:
        raise WorkspaceError(f"变体只支持 {'、'.join(VARIANTS)}", 400, "invalid_argument")
    root = manager.project_root(project_id)
    tag = versioning.tag_name(version)
    found = _run_git(root, ("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"))
    commit = found.stdout.strip()
    if found.returncode != 0 or not commit:
        raise WorkspaceError(f"版本 {version} 不存在", 404, "release_not_found")
    stored = release_store.get(manager.data_dir, project_id, version, commit, variant)
    if stored is not None:
        return stored
    with manager._project_lock(project_id):
        stored = release_store.get(manager.data_dir, project_id, version, commit, variant)
        if stored is not None:
            return stored
        message = _git(root, "for-each-ref", f"refs/tags/{tag}", "--format=%(contents)")
        by, degraded = versioning.parse_release_message(message)
        with tempfile.TemporaryDirectory(prefix="onto-release-render-") as scratch:
            dest = Path(scratch) / "package.zip"
            try:
                with release_tree(root, commit) as tree:
                    src = _source(
                        manager, project_id, root, tree, commit, version,
                        variant=variant, allow_unbound=degraded,
                    )
                    ADAPTER.render(dataclasses.replace(src, binding=resolve_binding(src)), dest)
            except WorkspaceError as exc:
                raise WorkspaceError(
                    f"版本 {version} 没有留存的包，按发布标签重新生成失败：{exc}",
                    409, "release_package_unavailable",
                ) from exc
            release_store.save(manager.data_dir, project_id, version, dest, {
                "commit": commit, "tag": tag, "released_by": by, "degraded": degraded,
                "backfilled": variant == VARIANT_STANDARD,
            }, variant)
    return release_store.get(manager.data_dir, project_id, version, commit, variant)


def solver_candidate_package(manager, project_id: str, dest_path: Path, allow_unbound: bool = False) -> dict:
    """求解验证候选包：按 HEAD 提交渲染，manifest 标 draft，不打标签、不算发布。

    只跳过验证门禁本身（验证就是为了它），静态检查与数据绑定照发布同口径拦截。
    返回提交、发布时会得到的版本号与求解契约修订号，验证方据此回写回执。
    """
    from .. import solver_contract

    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        commit = _head_commit(root)
        version, _ = versioning.next_version(root, commit)
        with release_tree(root, commit) as tree:
            check = solver_service.solver_check_at(tree)
            if not check.needs_validation:
                raise WorkspaceError(
                    "当前版本没有启用的求解契约，无需端到端验证", 400, "solver_validation_not_required"
                )
            if check.findings:
                raise WorkspaceError(
                    "求解契约静态检查未通过：" + "；".join(item["message"] for item in check.findings[:3]),
                    400, solver_contract.CODE_INVALID,
                )
            src = _source(manager, project_id, root, tree, commit, version, allow_unbound=allow_unbound, draft=True)
            ADAPTER.render(dataclasses.replace(src, binding=resolve_binding(src)), Path(dest_path))
    return {
        "commit": commit,
        "version": version,
        "revision": check.revision,
        "contract_digest": str(check.digests.get("total") or ""),
    }
