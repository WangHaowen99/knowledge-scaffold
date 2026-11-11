"""工作区版本：读历史版本文件、整树回退、版本列表与用户标记。"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import shutil

from .errors import WorkspaceError
from .gitops import _git, _run_git
from .layout import META_FILE

# 发布标签 release/<主.次.补丁>（publish/versioning 打的标签，版本列表据此标出发布过的提交）
_RELEASE_TAG_RE = re.compile(r"release/(\d+\.\d+\.\d+)")

# 回退只恢复业务内容（ADR-0094）：仓库配置、项目元数据、运行状态、助手装配保持当前；
# 目录 index.md 是派生文件，由写入事务按恢复后的文件重新生成，不单独算改动
ROLLBACK_EXCLUDED_TOP = frozenset({
    ".git", ".gitignore", ".gitattributes", ".gitmodules", ".claude", ".ontology-state", "tmp",
    META_FILE, "CLAUDE.md", "README.md",
})
ROLLBACK_EXCLUDED_PREFIXES = (".onto/run/",)


def rollback_managed(rel: str) -> bool:
    """这个路径随回退恢复吗：预览、执行、未提交改动检查同一口径。"""
    from .files import FilesMixin

    if not rel or rel.split("/", 1)[0] in ROLLBACK_EXCLUDED_TOP or rel.startswith(ROLLBACK_EXCLUDED_PREFIXES):
        return False
    return rel not in {f"{directory}/index.md" for directory, _title in FilesMixin.INDEX_DIRS}


def rollback_changes(root: Path, target: str, head: str = "HEAD") -> dict[str, list[str]]:
    """回退到 target 要动的路径：restore 写回目标版本里的内容（其中 create 是现在没有的），
    delete 删掉目标版本之后才有的。"""
    records = _git(root, "-c", "core.quotePath=false", "diff", "--name-status", "--no-renames", "-z",
                   target, head, "--").split("\0")
    out: dict[str, list[str]] = {"restore": [], "delete": [], "create": []}
    for status, rel in zip(records[0::2], records[1::2]):
        if rollback_managed(rel):
            out["delete" if status == "A" else "restore"].append(rel)
            if status == "D":
                out["create"].append(rel)
    return out


def rollback_counts(changes: dict[str, list[str]]) -> dict:
    """develop 的口径：{added, modified, deleted, total}（回退确认框「将新增 a、修改 m、删除 d 个业务文件」）。"""
    created = set(changes["create"])
    out = {"added": list(changes["create"]), "modified": [rel for rel in changes["restore"] if rel not in created],
           "deleted": list(changes["delete"])}
    out["total"] = sum(len(value) for value in out.values())
    return out


def managed_dirty_paths(root: Path) -> list[str]:
    """主线工作区里没提交、或已暂存的业务路径（回退前必须为空）。"""
    records = _git(root, "-c", "core.quotePath=false", "status", "--porcelain=v1", "-z",
                   "--untracked-files=all").split("\0")
    paths, index = set(), 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        if rollback_managed(record[3:]):
            paths.add(record[3:])
        if record[0] in "RC" or record[1] in "RC":  # 改名、复制：下一条是原路径
            previous = records[index] if index < len(records) else ""
            index += 1
            if rollback_managed(previous):
                paths.add(previous)
    return sorted(paths)


# 回退后作废的派生状态：由被替换的内容得出的结论（验收回执、核验记录、进度、复核草稿）
DERIVED_STATE_FILES = (
    "eval.json", "progress.json", "research-review.json", "research-inventory.json", "research-baseline.json",
    "script-verifications.json", "solver-validations.json",
)
DERIVED_STATE_DIRS = ("artifact-reviews", "studio-review")
DIRTY_MESSAGE = "项目存在未提交或暂存的修改，请先完成当前编辑"
NOTHING_MESSAGE = "当前业务内容与该版本一致，无需回退"


def invalidate_derived_state(root: Path) -> list[str]:
    """删掉 .ontology-state 里由回退前内容得出的结论，返回没删掉的名字。"""
    state = root / ".ontology-state"
    failed = []
    for name in DERIVED_STATE_FILES:
        try:
            (state / name).unlink(missing_ok=True)
        except OSError:
            failed.append(name)
    for name in DERIVED_STATE_DIRS:
        path = state / name
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        except OSError:
            failed.append(name)
    return failed


def _remove_entry(path: Path) -> None:
    """删掉路径本身：符号链接不跟随，目录整棵删。"""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)

# 提交说明前缀 → 版本来源（契约 7.6 VersionSource）；没有 Onto-Source trailer 的存量提交用它推断
_SOURCE_PREFIXES = (
    ("revert:", "revert"),
    ("web:", "web"),
    ("knowledge:", "web"),
    ("studio:", "studio"),
    ("mcp:", "mcp"),
    ("import:", "import"),
    ("clone:", "import"),
    ("init:", "plugin"),
    ("chore:", "plugin"),
    ("feedback:", "plugin"),
)
# trailer 里的 Onto-Source 取值 → 界面口径（7.17：agent 对应 assistant）
_TRAILER_SOURCES = {"agent": "assistant"}
_TRAILER_RE = re.compile(r"^(Onto-[A-Za-z]+):\s*(.+?)\s*$", re.M)
_BY_RE = re.compile(r" by ([A-Za-z0-9_.-]{1,64})$")


def _trailers(body: str) -> dict:
    return {key: value for key, value in _TRAILER_RE.findall(body or "")}


def _subject_author(subject: str) -> str:
    """Web 保存的提交说明以「 by 用户名」结尾（gitops._by）。"""
    match = _BY_RE.search(subject or "")
    return match.group(1) if match else ""


def _semver(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def version_source(subject: str, trailers: dict | None = None) -> str:
    source = (trailers or {}).get("Onto-Source")
    if source:
        return _TRAILER_SOURCES.get(source, source)
    lowered = (subject or "").lower()
    for prefix, value in _SOURCE_PREFIXES:
        if lowered.startswith(prefix):
            return value
    return "assistant"


class VersionsMixin:
    def file_at_revision(self, project_id: str, rev: str, rel_path: str, max_bytes: int = 512 * 1024) -> dict:
        """读某个版本的文件（契约 F-06）：版本号不合法 invalid_commit，该版本没有这个文件 file_not_found。"""
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}|HEAD(~[0-9]+)?", rev or ""):
            raise WorkspaceError("无效的版本号", 400, "invalid_commit")
        root = self.project_root(project_id)
        target = self._resolve_inside(root, rel_path)
        rel = target.relative_to(root.resolve()).as_posix()
        if _run_git(root, ("cat-file", "-e", f"{rev}^{{commit}}")).returncode != 0:
            raise WorkspaceError("版本不存在", 400, "invalid_commit")
        shown = _run_git(root, ("show", f"{rev}:{rel}"))
        if shown.returncode != 0:
            raise WorkspaceError("该版本没有这个文件", 404, "file_not_found")
        content = shown.stdout
        truncated = len(content.encode("utf-8")) > max_bytes
        if truncated:
            content = content.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        commit = _git(root, "rev-parse", f"{rev}^{{commit}}").strip()
        return {"path": rel, "rev": rev, "commit": commit, "content": content, "truncated": truncated}

    def revert_target(self, root: Path, commit: str) -> str:
        """回退目标 → 完整提交号：版本号不合法、不存在、不在主线历史上都是 invalid_commit，就是当前版本时
        nothing_to_revert。"""
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit or ""):
            raise WorkspaceError("无效的版本号", 400, "invalid_commit")
        if _run_git(root, ("cat-file", "-e", f"{commit}^{{commit}}")).returncode != 0:
            raise WorkspaceError("版本不存在或已不可用", 400, "invalid_commit")
        target = _git(root, "rev-parse", f"{commit}^{{commit}}").strip()
        if target == _git(root, "rev-parse", "HEAD").strip():
            raise WorkspaceError("当前已是该版本，无需回退", 400, "nothing_to_revert")
        if _run_git(root, ("merge-base", "--is-ancestor", target, "HEAD")).returncode != 0:
            raise WorkspaceError("只能回退到当前版本历史中的提交", 400, "invalid_commit")
        return target

    def revert_to(self, project_id: str, commit: str, author: str = "", expected_head: str = "",
                  busy_check=None) -> dict:
        """把业务内容整份恢复为指定版本并生成新提交（历史保留，可再回退；契约 V-06、方案 C1、ADR-0094）。

        经写入事务：只动、只提交该版本与 HEAD 之间不同的业务路径（之后新增的一并删除）。仓库配置、元数据、
        运行状态不随回退恢复（rollback_managed）。持项目锁后依次挡：busy_check（调用方的并发检查）、
        expected_head 不符（head_moved）、主线工作区有没提交或暂存的改动（workspace_dirty）。提交后作废派生状态，
        删不掉的在结果里带 warning。
        """
        from ...kernel.writepath import ChangeTx, Origin

        with ChangeTx(self, project_id, Origin("revert", author), summary=f"回退到 {(commit or '')[:8]}",
                      expected_head=expected_head or None) as tx:
            root = tx.root
            if busy_check is not None:
                busy_check(root)
            if expected_head and tx.base_head != expected_head:
                raise WorkspaceError("项目已产生新版本，请重新预览后再回退", 409, "head_moved", {"head": tx.base_head})
            target = self.revert_target(root, commit)
            dirty = managed_dirty_paths(root)
            if dirty:
                raise WorkspaceError(DIRTY_MESSAGE, 409, "workspace_dirty", {"paths": dirty[:20]})
            if _git(root, "diff", "--cached", "--name-only", "--").strip():
                # 暂存的业务改动上面已挡；暂存的是不随回退的路径（元数据等）时也不能被带进回退提交
                raise WorkspaceError("项目存在已暂存的元数据修改，请先完成当前编辑", 409, "workspace_dirty")
            changes = rollback_changes(root, target)
            if not changes["restore"] and not changes["delete"]:
                raise WorkspaceError(NOTHING_MESSAGE, 400, "nothing_to_revert")
            tx.touch_tracked(*changes["delete"], *changes["restore"])
            try:
                # 先删后写：同一路径在两个版本里一个是文件一个是目录时，顺序反了会写进去又删掉
                for rel in changes["delete"]:
                    _remove_entry(root / rel)
                if changes["restore"]:
                    # 只写工作区、不动真实索引：提交由写入事务用临时索引做，失败时索引里不留目标版本
                    _git(root, "--literal-pathspecs", "restore", f"--source={target}", "--worktree",
                         "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(changes["restore"]))
                from .. import mainline

                result = tx.commit(f"revert: 回退到版本 {mainline.count(root, target)}（{target[:8]}）")
            except BaseException:
                self._undo_revert(root, changes)
                raise
            if not result.commit:
                raise WorkspaceError(NOTHING_MESSAGE, 400, "nothing_to_revert")
            failed = invalidate_derived_state(root)
        out = {"commit": result.commit, "reverted_to": target, "changes": rollback_counts(changes)}
        if failed:
            out["warning"] = "部分派生状态未能清理，请重新运行相关检查"
        return out

    @staticmethod
    def _undo_revert(root: Path, changes: dict[str, list[str]]) -> None:
        """回退没提交成：业务路径回到 HEAD（回退前工作区是干净的，HEAD 就是原样）。写入事务随后按快照再恢复一遍。"""
        created = set(changes["create"])
        heads = [rel for rel in changes["delete"] + changes["restore"] if rel not in created]
        for rel in changes["create"] + heads:
            path = root / rel
            # 目标版本里才有的删掉；HEAD 里是文件、现在成了目录的（文件和目录互换过）也删掉，再从 HEAD 写回
            if rel in created or (path.is_dir() and not path.is_symlink()):
                try:
                    _remove_entry(path)
                except OSError:
                    pass
        if heads:
            _run_git(root, ("--literal-pathspecs", "restore", "--source=HEAD", "--worktree",
                            "--pathspec-from-file=-", "--pathspec-file-nul"), input="\0".join(heads))

    # ---- 版本 ----

    def versions(self, project_id: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        """主线版本（只计合入主线的提交，--first-parent，契约 3.9），从新到旧；limit、offset 分页（历史时间线）。

        版本号 n 从 1 开始（建项目的基线提交是 v0.1，历史里叫「迭代 1」），分页和之后的保存都不改变已有版本的号；
        version 与 n 相同、current 标出当前版本（develop 的迭代条目字段）。来源优先取提交 trailer Onto-Source，
        没有 trailer 的存量提交按提交说明的前缀推断（版本列表据此筛选）。时间为 epoch 秒。
        """
        root = self.project_root(project_id)
        out = _git(
            root, "log", "--first-parent", "--pretty=format:%H%x1f%ct%x1f%an%x1f%s%x1f%b%x1e",
        )
        tags = self._tags_by_commit(root)
        records = [record.strip("\n") for record in out.split("\x1e") if record.strip("\n")]
        total = len(records)
        entries = []
        end = len(records) if limit is None else offset + max(limit, 0)
        for index, record in enumerate(records[offset:end], start=offset):
            parts = record.split("\x1f")
            if len(parts) != 5:
                continue
            commit, ts, author, subject, body = parts
            commit_tags = tags.get(commit, [])
            trailers = _trailers(body)
            releases = [match.group(1) for tag in commit_tags if (match := _RELEASE_TAG_RE.fullmatch(tag["name"]))]
            entries.append({
                "commit": commit,
                "n": total - index,
                "version": total - index,
                "current": index == 0,
                "time": int(ts) if ts.isdigit() else None,
                "author": trailers.get("Onto-Author") or _subject_author(subject) or author,
                "message": subject,
                "source": version_source(subject, trailers),
                "trailer": trailers,
                "tags": [tag for tag in commit_tags if not tag["name"].startswith("release/")],
                "release": max(releases, key=_semver) if releases else None,
            })
        return entries

    @staticmethod
    def _tags_by_commit(root: Path) -> dict[str, list[dict]]:
        out = _git(
            root,
            "for-each-ref",
            "refs/tags",
            "--format=%(refname:short)%1f%(*objectname)%(objectname)%1f%(contents:subject)",
        )
        tags: dict[str, list[dict]] = {}
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                # 注释 tag 时 %(*objectname) 是所指 commit，轻量 tag 时为空、objectname 即 commit
                commit = parts[1][:40]
                tags.setdefault(commit, []).append({"name": parts[0], "label": parts[2]})
        return tags

    def tag_version(self, project_id: str, commit: str, label: str) -> dict:
        label = (label or "").strip()
        if not label:
            raise WorkspaceError("标记名称不能为空")
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit or ""):
            raise WorkspaceError("无效的版本号", 400, "invalid_commit")
        root = self.project_root(project_id)
        if _run_git(root, ("cat-file", "-e", f"{commit}^{{commit}}")).returncode != 0:
            raise WorkspaceError("版本不存在", 400, "invalid_commit")
        commit = _git(root, "rev-parse", f"{commit}^{{commit}}").strip()
        tag_name = f"mark-{int(time.time())}-{uuid.uuid4().hex[:4]}"
        with self._project_lock(project_id):  # 方案 C1：打标签也拿项目锁
            _git(root, "tag", "-a", tag_name, "-m", label[:120], commit)
        return {"name": tag_name, "label": label, "commit": commit}

    def delete_tag(self, project_id: str, tag_name: str) -> dict:
        if not re.fullmatch(r"mark-[0-9]+-[0-9a-f]{4}", tag_name):
            raise WorkspaceError("标记不存在，或不是用户标记", 404, "tag_not_found")
        root = self.project_root(project_id)
        if _run_git(root, ("rev-parse", "-q", "--verify", f"refs/tags/{tag_name}")).returncode != 0:
            raise WorkspaceError("标记不存在，或不是用户标记", 404, "tag_not_found")
        with self._project_lock(project_id):
            _git(root, "tag", "-d", tag_name)
        return {"deleted": tag_name}
