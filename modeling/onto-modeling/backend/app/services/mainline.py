"""主线版本号（API 契约 3.9、8.1#54；方案 C14）。

主线版本只计合入主线的提交：`git rev-list --first-parent` 的提交数 n，从 1 开始（建项目的
基线提交是 v0.1），界面显示 `v0.{n}`；发布包版本 `0.{n}.0` 与它一一对应（publish/versioning）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def label(n: int) -> str:
    return f"v0.{n}" if n > 0 else "v0.0"


def count(root: Path, rev: str = "HEAD") -> int:
    """rev 在主线上的版本号 n（first-parent 计数）；仓库还没有提交时为 0。"""
    out = _git(Path(root), "rev-list", "--count", "--first-parent", rev)
    return int(out) if out.isdigit() else 0


def state(root: Path) -> dict:
    head = _git(Path(root), "rev-parse", "--verify", "-q", "HEAD")
    n = count(root) if head else 0
    return {"head": head, "n": n, "version_label": label(n)}


def head_time(root: Path) -> int | None:
    out = _git(Path(root), "log", "-1", "--format=%ct")
    return int(out) if out.isdigit() else None


def first_parent_numbers(root: Path, limit: int | None = None) -> dict[str, int]:
    """主线提交 → 版本号 n（只含 first-parent 链上的提交）。"""
    args = ["rev-list", "--first-parent", "HEAD"]
    commits = _git(Path(root), *args).split()
    total = len(commits)
    numbers = {commit: total - index for index, commit in enumerate(commits)}
    if limit is not None:
        numbers = dict(list(numbers.items())[:limit])
    return numbers
