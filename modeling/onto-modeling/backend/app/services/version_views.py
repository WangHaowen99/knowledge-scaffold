"""版本详情、某版本的文件、对比、回退预览（契约 V-02～V-05）：都是对主线 git 历史的只读投影。

改了什么按主线上一个版本（first parent）比；条目与插件产物的认法和变更集一样（`changesets.describe`），
一句话的说明同变更集明细（`entity_diff`）。
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ..services.workspace.errors import WorkspaceError
from ..services.workspace.gitops import _EXCLUDE_STATE_PATHSPECS, _run_git

# git 的空树：建项目的第一个版本和它比
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CHANGES_MAX = 500
FILES_MAX = 5000
# V-03 下载：未压缩内容上限、按批读 blob、超过多少落盘
ZIP_BYTES_MAX = 256 * 1024 * 1024
ZIP_BATCH = 200
ZIP_SPOOL_BYTES = 16 * 1024 * 1024
DIFF_BYTES_MAX = 512 * 1024
# 版本里的系统文件：能力锁另列（回退预览的 packs_lock_changes），不当文件改动列出
SYSTEM_PREFIXES = (".onto/",)
_HEX = re.compile(r"[0-9a-fA-F]{6,40}")


def _git(root: Path, *args: str) -> str:
    proc = _run_git(root, args)
    if proc.returncode != 0:
        raise WorkspaceError("版本不存在", 400, "invalid_commit")
    return proc.stdout


def resolve(root: Path, rev: str) -> str:
    """版本号（6～40 位十六进制或 HEAD~n）→ 完整提交号；不在仓库里时 invalid_commit。"""
    if not (_HEX.fullmatch(rev or "") or re.fullmatch(r"HEAD(~[0-9]+)?", rev or "")):
        raise WorkspaceError("版本号不合法", 400, "invalid_commit")
    proc = _run_git(root, ("rev-parse", "--verify", "-q", f"{rev}^{{commit}}"))
    if proc.returncode != 0 or not proc.stdout.strip():
        raise WorkspaceError("版本不存在", 400, "invalid_commit")
    return proc.stdout.strip()


def _parent(root: Path, commit: str) -> str:
    proc = _run_git(root, ("rev-parse", "--verify", "-q", f"{commit}^1"))
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else EMPTY_TREE


def name_status(root: Path, old: str, new: str) -> list[tuple[str, str]]:
    """两个版本之间改动的路径（不含项目元数据与工具状态），[(A|M|D, 路径)]。"""
    out = _git(root, "-c", "core.quotePath=false", "diff", "--name-status", "--no-renames", "-z", old, new,
               "--", ".", *_EXCLUDE_STATE_PATHSPECS)
    records = out.split("\0")
    return [(status[:1], rel) for status, rel in zip(records[0::2], records[1::2]) if rel]


def cat_files(root: Path, rev: str, rels: list[str]) -> dict[str, bytes]:
    """一次 git cat-file 读一批文件；rev 下没有的路径不在结果里。"""
    from ..kernel.artifacts.service import _cat_files

    if rev == EMPTY_TREE:
        return {}
    return _cat_files(root, rev, rels)


# ---- V-02 ----


def changes(manager, root: Path, commit: str) -> list[dict]:
    """这个版本相对主线上一个版本改了什么：[{target, change, detail, review}]。"""
    from ..kernel.changesets import entity_diff
    from ..kernel.changesets import service as changesets

    parent = _parent(root, commit)
    paths = [(status, rel) for status, rel in name_status(root, parent, commit)
             if not rel.startswith(SYSTEM_PREFIXES)][:CHANGES_MAX]
    described = changesets.describe(manager, root, paths)
    rels = [item["path"] for item in described["items"]]
    before, after = cat_files(root, parent, rels), cat_files(root, commit, rels)
    model = changesets._model(root)
    out = []
    for item in described["items"]:
        old, new = before.get(item["path"]), after.get(item["path"])
        if item["group"] in model.ENTITY_KINDS:
            info = entity_diff.changes(model, item["group"], old, new)
        else:
            info = entity_diff.file_changes(old, new)
        out.append({"target": item["target"], "change": item["change"], "detail": info["summary"], "review": None})
    return out


# ---- V-03 ----


def files(root: Path, commit: str, prefix: str) -> list[dict]:
    """某个版本里 prefix 下的文件 [{path, size}]（按路径排序，最多 FILES_MAX 个）。"""
    args = ["-c", "core.quotePath=false", "ls-tree", "-r", "-l", "-z", commit]
    if prefix:
        args += ["--", prefix]
    out = _git(root, *args)
    items = []
    for record in out.split("\0"):
        meta, _, rel = record.partition("\t")
        parts = meta.split()
        if not rel or len(parts) < 4 or parts[1] != "blob":
            continue
        if rel.split("/", 1)[0] in (".onto-web.json", ".ontology-state") or rel.startswith(".onto/run/"):
            continue
        items.append({"path": rel, "size": int(parts[3]) if parts[3].isdigit() else 0})
    items.sort(key=lambda item: item["path"])
    return items[:FILES_MAX]


def zip_files(root: Path, commit: str, prefix: str):
    """某个版本 prefix 下的文件打成 zip（路径保持仓库相对路径），返回读到开头的临时文件（大了落盘，不整份放内存）。

    按批读 blob；超过上限回 payload_too_large；有文件读不出来（git 超时等）回 service_unavailable，不给缺文件的 zip。"""
    listed = files(root, commit, prefix)
    if sum(item["size"] for item in listed) > ZIP_BYTES_MAX:
        raise WorkspaceError("这个版本的文件太多，不能一次下载", 413, "payload_too_large",
                             {"limit_bytes": ZIP_BYTES_MAX})
    out = tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_BYTES)
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for start in range(0, len(listed), ZIP_BATCH):
                batch = [item["path"] for item in listed[start:start + ZIP_BATCH]]
                blobs = cat_files(root, commit, batch)
                if len(blobs) != len(batch):
                    raise WorkspaceError("读不出这个版本的文件，稍后重试", 503, "service_unavailable")
                for rel in batch:
                    archive.writestr(rel, blobs[rel])
    except BaseException:
        out.close()
        raise
    out.seek(0)
    return out


# ---- V-04 ----


def text_at(root: Path, commit: str, rel: str) -> str | None:
    """某版本里这份文件的文本；没有这个文件时 None。"""
    proc = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=root, capture_output=True, timeout=60)
    if proc.returncode != 0:
        return None
    data = proc.stdout[:DIFF_BYTES_MAX]
    return data.decode("utf-8", errors="replace")


def hunks(left: str, right: str) -> list[dict]:
    """按行比较的改动块（行号从 1 起；纯新增时 left_lines 为 0，left_start 是插在哪一行之前）。"""
    a = left.split("\n") if left else []
    b = right.split("\n") if right else []
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        out.append({"left_start": i1 + 1, "left_lines": i2 - i1, "right_start": j1 + 1, "right_lines": j2 - j1})
    return out


# ---- V-05 ----


def _lock_versions(root: Path, rev: str) -> dict[str, str]:
    from .packs import lockfile

    blob = cat_files(root, rev, [lockfile.LOCK_REL]).get(lockfile.LOCK_REL)
    if not blob:
        return {}
    try:
        lock = lockfile.PackLock.from_dict(json.loads(blob.decode("utf-8")))
    except ValueError:
        return {}
    return {entry.name: entry.version for entry in lock.enabled()}


def _pack_label(name: str, version: str | None) -> str:
    """能力的显示名（清单 label）；包仓库里找不到这个版本时用包名。"""
    from ..api import deps

    try:
        repository = deps.workspaces.packs.repository
        version = version if version and repository.has(name, version) else (repository.versions(name) or [None])[0]
        return (repository.manifest(name, version).label or name) if version else name
    except Exception:  # noqa: BLE001 - 显示名取不到不影响预览
        return name


def revert_preview(root: Path, target: str, head: str) -> dict:
    """回退到 target 会动哪些文件、撤回哪些版本、能力锁怎么变（与 V-06 的整树恢复同一口径）。"""
    from . import mainline
    from .workspace.versions import rollback_changes, rollback_counts

    changed = rollback_changes(root, target, head)
    if not changed["restore"] and not changed["delete"]:
        raise WorkspaceError("当前业务内容与该版本一致，无需回退", 400, "nothing_to_revert")
    files_out = sorted(({"path": rel, "change": change} for change in ("restore", "delete")
                        for rel in changed[change] if not rel.startswith(SYSTEM_PREFIXES)),
                       key=lambda item: item["path"])
    target_n, head_n = mainline.count(root, target), mainline.count(root, head)
    before, now = _lock_versions(root, target), _lock_versions(root, head)
    packs = [{"pack": name, "label": _pack_label(name, now.get(name) or before.get(name)),
              "from": now.get(name), "to": before.get(name)}
             for name in sorted(set(before) | set(now)) if before.get(name) != now.get(name)]
    return {
        "target": {"commit": target, "version_label": mainline.label(target_n)},
        "next_version_label": mainline.label(head_n + 1),
        "expected_head": head,
        "changes": rollback_counts(changed),
        "files": files_out,
        "undone_versions": [mainline.label(n) for n in range(target_n + 1, head_n + 1)],
        "packs_lock_changes": packs,
    }
