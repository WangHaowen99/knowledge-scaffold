"""工作区版本：读历史版本文件、整树回退、版本列表与用户标记。"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from .errors import WorkspaceError
# 发布标签 release/<主.次.补丁>（publish/versioning 打的标签，版本列表据此标出发布过的提交）
_RELEASE_TAG_RE = re.compile(r"release/(\d+\.\d+\.\d+)")

from .gitops import _EXCLUDE_STATE_PATHSPECS, _git, _run_git, _untrack_state_in_index

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

    def revert_to(self, project_id: str, commit: str, author: str = "") -> dict:
        """把工作区整树恢复为指定版本并生成新提交（历史保留，可再回退；契约 V-06、方案 C1）。

        经写入事务：只动、只提交该版本与 HEAD 之间不同的路径（该版本之后新增的文件一并删除），
        agent 还没提交的其他改动不带进回退提交。元数据与工具状态不随回退恢复：存量历史里它们还在
        git 中，取旧版会把所有者、项目名一起回滚。返回新提交与回退目标。
        """
        from ...kernel.writepath import ChangeTx, Origin

        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit):
            raise WorkspaceError("无效的版本号", 400, "invalid_commit")
        with ChangeTx(self, project_id, Origin("revert", author), summary=f"回退到 {commit[:8]}") as tx:
            root = tx.root
            if _run_git(root, ("cat-file", "-e", f"{commit}^{{commit}}")).returncode != 0:
                raise WorkspaceError("版本不存在", 400, "invalid_commit")
            target = _git(root, "rev-parse", f"{commit}^{{commit}}").strip()
            records = _git(
                root, "-c", "core.quotePath=false", "diff", "--name-status", "--no-renames", "-z",
                target, "HEAD", "--", ".", *_EXCLUDE_STATE_PATHSPECS,
            ).split("\0")
            restore, added = [], []
            for status, rel in zip(records[0::2], records[1::2]):
                (added if status == "A" else restore).append(rel)
            if not restore and not added:
                raise WorkspaceError("当前内容与该版本一致，无需回退", 400, "nothing_to_revert")
            tx.touch(*restore, *added)
            if restore:
                _git(root, "--literal-pathspecs", "checkout", target, "--", *restore)
            for rel in added:  # 目标版本之后新增的文件：整树恢复要删掉
                path = root / rel
                if path.is_file() or path.is_symlink():
                    path.unlink()
            if ".gitignore" in restore:
                # 旧版本的 .gitignore 可能没有元数据与工具状态的排除项：补齐，否则之后会被重新跟踪
                _untrack_state_in_index(root)
            result = tx.commit(f"revert: 回退到 {target[:8]}")
            if not result.commit:
                raise WorkspaceError("当前内容与该版本一致，无需回退", 400, "nothing_to_revert")
        return {"commit": result.commit, "reverted_to": target}

    # ---- 版本 ----

    def versions(self, project_id: str) -> list[dict]:
        """主线版本（只计合入主线的提交，--first-parent，契约 3.9），从新到旧。

        版本号 n 从 1 开始（建项目的基线提交是 v0.1）；来源优先取提交 trailer Onto-Source，
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
        for index, record in enumerate(records):
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
