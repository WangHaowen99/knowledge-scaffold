"""按提交取发布源（详设 ⑱、方案 A8）：git archive 导出只读树，核对与提交一致；列出不会进包的未提交改动。"""

from __future__ import annotations

import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .. import package_mapping
from ..workspace.errors import WorkspaceError
from ..workspace.gitops import _changed_paths, _git
from ..workspace.layout import RESOURCE_DIRECTIVES
from .layout import PACKAGE_SOURCE_DIRS, PACKAGE_SOURCE_PATHS


def is_package_source(path: str) -> bool:
    """仓库相对路径是否会被发布读取：PACKAGE_SOURCE_DIRS 下的文件，资料区只算用途清单、表格和表格的文本版
    （Excel 表头从文本版读，ADR-0076）。"""
    if path == RESOURCE_DIRECTIVES:
        return True
    if path.startswith("resource/text/") and path.endswith(".md"):
        return Path(path[:-3]).suffix.lower() in package_mapping.TABLE_SUFFIXES
    if path.startswith("resource/"):
        return Path(path).suffix.lower() in package_mapping.TABLE_SUFFIXES
    return any(path.startswith(f"{directory}/") for directory in PACKAGE_SOURCE_DIRS)


@contextmanager
def release_tree(root: Path, commit: str):
    """把提交里发布要读的路径（is_package_source）导出成临时只读树，退出时删除。

    git archive 打成 tar 再解包：tarfile 的 data 过滤器拒绝绝对路径、..、指向树外的链接
    与设备文件；解包后核对文件与提交逐一对得上（.gitattributes 的 export-ignore /
    export-subst 会让 git archive 漏文件或改内容）。
    """
    with tempfile.TemporaryDirectory(prefix="onto-release-") as scratch:
        tree = Path(scratch) / "tree"
        tree.mkdir()
        # 本体各目录整棵导出；资料区只取用途清单和表格，大体积的 PDF、图片不复制
        present = [
            path for path in _git(
                root, "--literal-pathspecs", "ls-tree", "--name-only", "-z", commit, "--",
                *PACKAGE_SOURCE_DIRS,
            ).split("\0") if path
        ]
        present += [
            path for path in _git(
                root, "--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z", commit, "--",
                "resource",
            ).split("\0") if path and is_package_source(path)
        ]
        if present:
            archive = Path(scratch) / "release.tar"
            _git(
                root, "--literal-pathspecs", "archive", "--format=tar", "-o", str(archive),
                commit, "--", *present,
            )
            try:
                with tarfile.open(archive) as tar:
                    tar.extractall(tree, filter="data")
            except tarfile.TarError as exc:  # FilterError 是 TarError 的子类
                raise WorkspaceError(
                    f"版本里有不安全的路径，无法发布：{exc}", code="release_tree_unsafe"
                ) from exc
            _verify_release_tree(root, commit, present, tree)
        yield tree


def _verify_release_tree(root: Path, commit: str, paths: list[str], tree: Path) -> None:
    listing = _git(root, "--literal-pathspecs", "ls-tree", "-r", "-l", "-z", commit, "--", *paths)
    for record in listing.split("\0"):
        if not record:
            continue
        info, path = record.split("\t", 1)
        mode, kind, _object, size = info.split()
        if kind != "blob" or mode == "120000":
            continue  # 符号链接由 data 过滤器把关
        target = tree / path
        if not target.is_file() or target.stat().st_size != int(size):
            raise WorkspaceError(
                f"{path} 导出后与版本内容不一致（请去掉 .gitattributes 里的 export-ignore / export-subst）",
                code="release_tree_mismatch",
            )


def uncommitted_package_changes(root: Path) -> list[str]:
    """发布要读的路径里没提交的改动：发布只用提交，这些改动不会进包。

    各目录的 index.md 是派生的清单，导出不读，不算；资料区的 PDF、图片发布不读，也不算。
    """
    return sorted(
        path for path in _changed_paths(root, PACKAGE_SOURCE_PATHS)
        if is_package_source(path) and Path(path).name.lower() != "index.md"
    )
