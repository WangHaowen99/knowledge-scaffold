"""发布流程（方案 A8；详设 ⑱；API 契约 3.20、3.21；ADR-0098）。

确定提交 → git archive 只读树（tree.release_tree）冻结 → 门禁 → Hub 分配版本号 → 适配器渲染 →
上传并提交到 Hub（hub_packages.publish）→ 本地留存包、打 release/<版本> 注释标签、写发布记录
（项目元数据 last_package_publish）。Hub 是版本权威，不可用时发布失败；工作区里没提交的改动永远不进包。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .. import deterministic_contract, package_mapping, release_store
from .. import solver as solver_service
from ..workspace.errors import WorkspaceError
from ..workspace.files import unmarked_table_resources
from ..workspace.gitops import _git, _head_commit, _run_git
from ..workspace.layout import _content_revision
from . import gates, stages as publish_stages, versioning
from .ontology_package import ADAPTER, VARIANT_GRAPH, VARIANT_STANDARD, resolve_binding
from .spi import ReleaseSource
from .tree import release_tree, uncommitted_package_changes

CHECK_LIMIT = 50
CHECK_TABLE_LIMIT = 100
VARIANTS = (VARIANT_STANDARD, VARIANT_GRAPH)
# 预检渲染用的占位版本号：正式版本号要到 Hub 预留时才有
PREVIEW_VERSION = "0.1.0"


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
    package_id: str = "",
) -> Path:
    """按给定的树渲染一份包（不打标签、不留存）：source_root 缺省为工作区当前文件。

    发布与按版本补渲染走 publish_package / release_package；这里给预览、导出与测试用。
    version 缺省按主线版本号取 0.{n}.0（只是预览用的占位，正式版本号由 Hub 分配）；package_id 缺省项目 ID。
    """
    root = manager.project_root(project_id)
    if not version:
        from .. import mainline

        version = f"0.{mainline.count(root)}.0"
    src = _source(
        manager, project_id, root, source_root or root, source_commit, version,
        variant=VARIANT_GRAPH if graph else VARIANT_STANDARD,
        allow_unbound=allow_unbound, draft=draft, binding=binding, package_id=package_id,
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
    parts = content_checks(manager, project_id, root, commit)
    binding, table_resources = parts["binding"], parts["table_resources"]
    solver, deterministic = parts["solver"], parts["deterministic"]
    script_findings, structure = parts["script_findings"], parts["structure"]
    pack_gates, tree_error = parts["pack_gates"], parts["tree_error"]
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
    stages = publish_stages.readiness(manager, project_id)
    gate_list = gates.gate_chain(
        issues, solver, script_findings + structure["findings"], tree_error,
        packs=pack_gates, stages=stages, deterministic=deterministic, objects=objects,
    )
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
        "deterministic": deterministic,
        # consumer 导入规则的结构自检单列：不计入数据绑定的 blocking，进「知识包结构」门禁
        "structure": structure,
        # 发布前必须完成的建模阶段（与 develop 同规则）
        "stages": stages,
        "gates": gate_list,
        "format": "solver" if solver.get("enabled") else "ontology",
        "uncommitted": uncommitted[:CHECK_LIMIT],
        "uncommitted_count": len(uncommitted),
        "checked_at": time.time(),
        "duration_ms": int((time.time() - started) * 1000),
    }


def content_checks(manager, project_id: str, root: Path, treeish: str) -> dict:
    """发布检查里看内容的几道：数据绑定、求解契约、渲染时的结构问题、consumer 导入规则、包声明的门禁、发布树安全。

    按 treeish（提交或树对象）导出的只读树算。发布检查（PB-01）和校验之后的发布就绪检查（ADR-0103）共用这一份，
    两边结论一致；treeish 不限主线，校验时查的是助手副本的树。
    """
    parts = {
        "binding": {"issues": [], "warnings": [], "tables": {}, "objects": {}, "problems": []},
        "table_resources": [], "solver": {"enabled": False}, "deterministic": {"enabled": False},
        "script_findings": [],
        "structure": {"blocking": 0, "findings": [], "truncated": False, "skipped": None},
        "pack_gates": [], "tree_error": None,
    }
    try:
        with release_tree(root, treeish) as tree:
            parts["binding"] = package_mapping.resolve_object_tables(
                tree, tables=package_mapping.read_source_tables(tree))
            parts["table_resources"] = unmarked_table_resources(tree, CHECK_LIMIT)
            parts["solver"] = solver = solver_service.release_status_at(tree, root)
            parts["deterministic"] = deterministic = deterministic_contract.release_status_at(tree)
            if solver.get("enabled") and deterministic.get("enabled"):
                parts["deterministic"] = deterministic = {
                    **deterministic,
                    "findings": [
                        *(deterministic.get("findings") or []),
                        {
                            "code": deterministic_contract.CODE_CONFLICT,
                            "message": "第一版不能在同一知识包中混合 deterministic 与 solver_workflow",
                        },
                    ],
                }
            src = _source(manager, project_id, root, tree, treeish, PREVIEW_VERSION)
            parts["script_findings"] = gates.render_findings(src)
            parts["structure"] = _structure_check(
                manager, project_id, tree, solver, deterministic, bool(parts["script_findings"]),
            )
        parts["pack_gates"] = _pack_gates(manager, root, treeish)
    except WorkspaceError as exc:
        if exc.code not in ("release_tree_unsafe", "release_tree_mismatch"):
            raise
        parts["tree_error"] = {"code": exc.code, "level": "error", "message": str(exc)}
    return parts


def _structure_check(
    manager, project_id: str, tree: Path, solver: dict, deterministic: dict, render_failed: bool,
) -> dict:
    """按 consumer 的导入规则检查这棵树导出的包（降级口径，数据绑定另有一栏）。

    求解契约有问题或渲染本身失败时导出不成包，那两处已经报过，这里跳过不重复。
    """
    from .. import hub_packages

    skipped = (
        "solver_findings" if solver.get("findings")
        else "deterministic_findings" if deterministic.get("findings")
        else "render_failed" if render_failed else None
    )
    if skipped:
        return {"blocking": 0, "findings": [], "truncated": False, "skipped": skipped}
    binding = package_mapping.resolve_object_tables(
        tree, tables=package_mapping.read_source_tables(tree), strict_declared=False,
    )
    try:
        findings = hub_packages.package_structure_findings(
            manager, project_id, binding=binding, allow_unbound=True,
            ontology_id=hub_packages.ontology_id(project_id), tree=tree,
        )
    except WorkspaceError as exc:
        code = exc.code if exc.code != "workspace_error" else "package_export_failed"
        findings = [{"code": code, "message": str(exc)}]
    findings = [{"level": "error", **item} for item in findings]
    return {
        "blocking": len(findings), "findings": findings[:CHECK_LIMIT],
        "truncated": len(findings) > CHECK_LIMIT, "skipped": None,
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
    *,
    token: str = "",
    subject: str = "",
    request_id: str = "",
    acknowledged_by: str = "",
    verified_revision: str = "",
    delegated: dict | None = None,
) -> dict:
    """用户发布知识包：按提交（缺省主线最新）冻结只读树，经 Hub 分配版本号、上传、提交，返回发布记录。

    同一 request_id 重放得到同一个 Hub 版本；新的 request_id 总是新版本（同一提交也一样），版本号只认 Hub。
    降级发布须可追溯：记录谁、何时发布，以及哪些对象因此未绑定数据表。启用求解契约的包要确认求解能力
    尚未经端到端验证（acknowledged_by），或给出已验证过的同一修订（verified_revision）。
    dest_path 给定时把这次发布的包复制过去。
    """
    from .. import hub_packages

    root = manager.project_root(project_id)
    commit = resolve_commit(root, commit)
    receipt = hub_packages.publish(
        manager, project_id, request_id or uuid.uuid4().hex, token, subject, allow_unbound,
        commit=commit, author=author, acknowledged_by=acknowledged_by,
        verified_revision=verified_revision, delegated=delegated,
    )
    record = _release_record(manager, project_id, receipt)
    if receipt.get("hub_members"):
        # 共享用户的 Hub 授权同步结果（develop !590）：失败时页面提示去「知识包共享」重试
        record["hub_members"] = receipt["hub_members"]
    if dest_path is not None:
        shutil.copyfile(release_package(manager, project_id, receipt["version"], token=token)[0], dest_path)
    return record


def _release_record(manager, project_id: str, receipt: dict) -> dict:
    """Hub 回执对应的本地发布记录；本地记录没写成（标签或留存失败）时只给 Hub 回执里的字段。"""
    hub = {key: receipt.get(key) for key in HUB_FIELDS}
    last = manager.get_project(project_id).get("last_package_publish") or {}
    if last.get("release_id") == receipt.get("release_id"):
        return {**last, **hub, "local_record_pending": bool(receipt.get("local_record_pending"))}
    for entry in releases(manager, project_id):
        if (entry.get("hub") or {}).get("release_id") == receipt.get("release_id"):
            return {
                "at": entry["released_at"], "by": entry["released_by"], "degraded": entry["degraded"],
                "unbound_count": len(entry["unbound_objects"]), "unbound_objects": entry["unbound_objects"],
                "version": entry["version"], "commit": entry["commit"], "tag": entry["tag"],
                "format": entry["format"], "sha256": (entry["package"] or {}).get("sha256"),
                "size": (entry["package"] or {}).get("size"), **hub,
                "local_record_pending": bool(receipt.get("local_record_pending")),
            }
    return {"version": receipt.get("version"), **hub, "local_record_pending": True}


HUB_FIELDS = ("ontology_id", "release_id", "package_digest", "committed_at")


def _unbound_objects(binding: dict, allow_unbound: bool) -> list[str]:
    """降级发布时未绑定数据表的对象，与适配器的 manifest 同口径。"""
    if not allow_unbound:
        return []
    return sorted({item["object"] for item in binding.get("issues") or [] if item["kind"] != "declared_unverifiable"})


def record_hub_release(manager, project_id: str, receipt: dict, task: dict, archive: Path) -> dict:
    """Hub 提交后的本地记录（hub_packages 在请求锁内调用）：留存上传的那份包、打标签、写最近一次发布。

    留存与标签都幂等，同一请求重放不重复写；更早的请求重放不覆盖更新的最近一次发布。
    """
    version = receipt["version"]
    if not versioning.VERSION_RE.match(version or ""):
        raise WorkspaceError(f"Hub 版本号不是正式版本：{version}", 502, "hub_error")
    tag = versioning.tag_name(version)
    commit = task["commit"]
    author = task.get("author") or ""
    degraded = _unbound_objects(task["binding"], task["allow_unbound"])
    hub = {key: receipt.get(key) for key in HUB_FIELDS}
    released_at = int(time.time())
    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        stored = release_store.get(manager.data_dir, project_id, version, commit)
        stored = stored[1] if stored else None
        archive = Path(archive)
        if stored is None and archive.is_file() and release_store.digest(archive) == receipt.get("package_digest"):
            stored = release_store.save(manager.data_dir, project_id, version, archive, {
                "commit": commit, "tag": tag, "released_at": released_at, "released_by": author,
                "degraded": bool(degraded), "unbound_count": len(degraded),
                "unbound_objects": degraded[:CHECK_LIMIT], **hub,
            })
        existing = _run_git(root, ("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}")).stdout.strip()
        if not existing:
            _git(root, "tag", "-a", tag, "-m", versioning.release_message(version, author, degraded, hub), commit)
        elif existing != commit:
            # 旧版本地发布留下的同名标签：Hub 版本照常有效，只是历史页上不另打标签
            logging.getLogger(__name__).warning("发布标签 %s 已指向别的提交，未重打", tag)
        uncommitted = uncommitted_package_changes(root)
        record = {
            "at": released_at,
            "by": author,
            "degraded": bool(degraded),
            "unbound_count": len(degraded),
            "unbound_objects": degraded[:CHECK_LIMIT],
            "version": version,
            "commit": commit,
            "tag": tag,
            "format": (stored or {}).get("format"),
            "sha256": receipt.get("package_digest"),
            "size": (stored or {}).get("size"),
            "uncommitted": uncommitted[:CHECK_LIMIT],
            "uncommitted_count": len(uncommitted),
            **hub,
        }
        last = manager._read_meta(root).get("last_package_publish") or {}
        if str(last.get("committed_at") or "") <= str(receipt.get("committed_at") or ""):
            manager._update_meta_unlocked(root, last_package_publish=record)
    return record


# ---- 发布记录、按版本取包 ----


def _tag_entries(root: Path) -> list[tuple[str, str, str, str]]:
    out = _git(
        root, "for-each-ref", f"refs/tags/{versioning.TAG_PREFIX}",
        "--format=%(refname:short)%1f%(*objectname)%(objectname)%1f%(taggerdate:unix)%1f%(contents)%1e",
    )
    entries = []
    for record in out.split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) == 4 and versioning.parse_tag(parts[0]):
            entries.append((parts[0], parts[1][:40], parts[2], parts[3]))
    return entries


def releases(manager, project_id: str) -> list[dict]:
    """发布记录：release/* 标签，按发布时间从新到旧。

    版本号来自 Hub（标签名），标签说明给出发布人、是否降级与 Hub 包身份；留存记录补上格式、包摘要与
    拉取记录（留存缺失时为 None，第一次下载补存后才有）。旧版本地发布的标签没有 Hub 身份（hub 为 None）。
    """
    root = manager.project_root(project_id)
    stored = release_store.records(manager.data_dir, project_id)
    entries = []
    for tag, commit, tagged_at, message in _tag_entries(root):
        number = versioning.parse_tag(tag)
        version = versioning.text(number)
        by, degraded = versioning.parse_release_message(message)
        package = stored.get(version)
        if package and package.get("commit") != commit:
            package = None
        hub = versioning.parse_hub(message)
        if hub and package and package.get("package_digest"):
            hub["package_digest"] = package["package_digest"]
        entries.append({
            "sort": (int(tagged_at) if tagged_at.isdigit() else 0, number),
            "version": version,
            "tag": tag,
            "commit": commit,
            "released_at": int(tagged_at) if tagged_at.isdigit() else None,
            "released_by": by,
            "degraded": degraded,
            "unbound_objects": list((package or {}).get("unbound_objects") or []),
            "format": package.get("format") if package else None,
            "package": {"sha256": package["sha256"], "size": package["size"]} if package else None,
            "pulls": list((package or {}).get("pulls") or []),
            "hub": hub,
        })
    entries.sort(key=lambda item: item.pop("sort"), reverse=True)
    return entries


def release_package(
    manager, project_id: str, version: str, variant: str = VARIANT_STANDARD, *, token: str = "",
) -> tuple[Path, dict]:
    """按版本取发布包：读发布时留存的那份（与 Hub 上的字节相同）。

    标准包留存缺失（数据目录丢失）时：给了登录令牌就从 Hub 下回缓存；否则按标签提交重新渲染一次并补存，
    记录标 backfilled（字节可能与 Hub 上的不同）。图模式变体只在本地按同一提交渲染并留存。
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
    message = _git(root, "for-each-ref", f"refs/tags/{tag}", "--format=%(contents)")
    by, degraded = versioning.parse_release_message(message)
    hub = versioning.parse_hub(message)
    with manager._project_lock(project_id):
        stored = release_store.get(manager.data_dir, project_id, version, commit, variant)
        if stored is not None:
            return stored
        with tempfile.TemporaryDirectory(prefix="onto-release-render-") as scratch:
            dest = Path(scratch) / "package.zip"
            record = {"commit": commit, "tag": tag, "released_by": by, "degraded": degraded, **(hub or {})}
            if variant == VARIANT_STANDARD and hub and token:
                from .. import hub_packages

                receipt = hub_packages.download(hub["ontology_id"], hub["release_id"], token, dest)
                record["package_digest"] = receipt.get("package_digest")
            else:
                try:
                    with release_tree(root, commit) as tree:
                        src = _source(
                            manager, project_id, root, tree, commit, version,
                            variant=variant, allow_unbound=degraded,
                            package_id=(hub or {}).get("ontology_id", ""),
                        )
                        ADAPTER.render(dataclasses.replace(src, binding=resolve_binding(src)), dest)
                except WorkspaceError as exc:
                    raise WorkspaceError(
                        f"版本 {version} 没有留存的包，按发布标签重新生成失败：{exc}",
                        409, "release_package_unavailable",
                    ) from exc
                record["backfilled"] = variant == VARIANT_STANDARD
            release_store.save(manager.data_dir, project_id, version, dest, record, variant)
    return release_store.get(manager.data_dir, project_id, version, commit, variant)
