"""发布版本号（API 契约 3.20、8.1#54；contract-changes 2026-11-01 主控条；方案 A9）。

发布包版本 `0.{n}.0`，n 是发布提交的主线版本号（`--first-parent` 计数，界面上的 v0.{n}）。
版本号必须严格大于项目已有的全部 release 标签（按 semver 比较，含旧版的 0.1.N）；
不满足时取已有最大版本的补丁号 + 1。同一提交重复发布沿用原标签。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import mainline
from ..workspace.gitops import _git

TAG_PREFIX = "release/"
TAG_RE = re.compile(r"release/(\d+)\.(\d+)\.(\d+)")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

Version = tuple[int, int, int]


def parse_tag(name: str) -> Version | None:
    match = TAG_RE.fullmatch(name or "")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def text(version: Version) -> str:
    return ".".join(str(part) for part in version)


def tag_name(version: str) -> str:
    return f"{TAG_PREFIX}{version}"


def released(root: Path, commit: str = "") -> list[Version]:
    """已有 release/* 标签的版本（升序）；给了 commit 只看指向它的标签。"""
    args = ["tag", "--list", f"{TAG_PREFIX}*"]
    if commit:
        args += ["--points-at", commit]
    return sorted(version for name in _git(root, *args).split() if (version := parse_tag(name)))


def next_version(root: Path, commit: str) -> tuple[str, bool]:
    """(发布版本号, 该提交是否已发布过)。"""
    existing = released(root, commit)
    if existing:
        return text(existing[-1]), True
    candidate: Version = (0, mainline.count(root, commit), 0)
    everything = released(root)
    if everything and candidate <= everything[-1]:
        major, minor, patch = everything[-1]
        candidate = (major, minor, patch + 1)
    return text(candidate), False


def release_message(version: str, author: str, degraded: list[str]) -> str:
    lines = [f"发布 {version}", "", f"发布人：{author or '未登录用户'}"]
    if degraded:
        lines.append(f"降级发布：是（{len(degraded)} 个对象未绑定数据表）")
    else:
        lines.append("降级发布：否")
    return "\n".join(lines) + "\n"


_BY_RE = re.compile(r"^发布人：(.*)$", re.M)
_DEGRADED_RE = re.compile(r"^降级发布：是", re.M)


def parse_release_message(message: str) -> tuple[str, bool]:
    """发布标签说明（release_message 写的）→ (发布人, 是否降级发布)。"""
    by = _BY_RE.search(message or "")
    author = by.group(1).strip() if by else ""
    return ("" if author == "未登录用户" else author), bool(_DEGRADED_RE.search(message or ""))
