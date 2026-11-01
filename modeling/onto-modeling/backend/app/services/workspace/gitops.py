"""工作区的 git 操作：子进程封装、残留锁处理、只提交指定路径、元数据与工具状态停止跟踪。"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .errors import WorkspaceError
from .layout import UNTRACK_COMMIT_MESSAGE, UNTRACKED_GITIGNORE_ENTRIES, UNTRACKED_STATE_PATHS, _GITIGNORE_STATE_HEADER

_log = logging.getLogger(__name__)

# 启动时清理的 git 锁残留；运行期遇锁时，锁超过该时长才视为残留
GIT_LOCK_FILES = ("index.lock", "HEAD.lock", "packed-refs.lock")
STALE_GIT_LOCK_SECONDS = 120


# 后端自己的 git 命令不跑工作区的钩子：项目的 core.hooksPath 指向 agent 提交收口的内核钩子
# （kernel/writepath/agent_commits），那是给 agent 的提交用的
_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


def _run_git(
    cwd: Path, args: tuple, env: dict | None = None, input: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_NO_HOOKS, *args], cwd=cwd, capture_output=True, text=True, timeout=60,
        env=env, input=input,
    )


def _git(cwd: Path, *args: str, env: dict | None = None, input: str | None = None) -> str:
    result = _run_git(cwd, args, env, input)
    # git 被超时杀掉、引擎被硬终止时会留下锁；锁够旧才当残留删掉重试一次，
    # 新锁可能属于正在进行的 git 操作，照原样报错
    if result.returncode != 0 and _remove_stale_lock(cwd, result.stderr):
        result = _run_git(cwd, args, env, input)
    if result.returncode != 0:
        raise WorkspaceError(f"git {args[0]} 失败: {result.stderr.strip()}", 500)
    return result.stdout


# git 报锁冲突时把锁文件路径放在引号里；不按提示原文匹配，界面语言不同也认得出
_GIT_LOCK_PATH_RE = re.compile(r"['‘\"]([^'’\"\n]+\.lock)['’\"]")


def _remove_stale_lock(cwd: Path, stderr: str) -> bool:
    match = _GIT_LOCK_PATH_RE.search(stderr or "")
    if not match:
        return False
    lock = Path(match.group(1))
    if not lock.is_absolute():
        lock = Path(cwd) / lock
    try:
        git_dir = (Path(cwd) / ".git").resolve()
        if not lock.resolve().is_relative_to(git_dir) or lock.is_symlink():
            return False
        age = time.time() - lock.stat().st_mtime
        if age <= STALE_GIT_LOCK_SECONDS:
            return False
        lock.unlink()
    except OSError:
        return False
    _log.warning("删除残留的 git 锁（已存在 %d 秒）后重试：%s", int(age), lock)
    return True


def remove_git_locks(root: Path) -> list[Path]:
    """删除 root/.git 下的锁文件（index、HEAD、packed-refs 与 refs 下的 *.lock）。"""
    git_dir = Path(root) / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return []
    candidates = [git_dir / name for name in GIT_LOCK_FILES]
    refs = git_dir / "refs"
    if refs.is_dir():
        candidates.extend(sorted(refs.rglob("*.lock")))
    removed = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def _by(author: str) -> str:
    return f" by {author}" if author else ""


def _changed_paths(root: Path, paths) -> list[str]:
    """paths 范围内有改动（含未跟踪、已删除）的文件，仓库相对路径。

    不加可选锁：只读 status 不刷新索引，不和 agent 正在进行的 git 操作抢 index.lock；
    路径按字面匹配，资料文件名里的 [ ] * 不当通配符。
    """
    out = _git(
        root, "--no-optional-locks", "--literal-pathspecs", "status", "--porcelain", "-z",
        "--untracked-files=all", "--", *paths,
    )
    changed = []
    records = iter(out.split("\0"))
    for record in records:
        if len(record) < 4:
            continue
        changed.append(record[3:])
        if record[0] in "RC":
            next(records, None)  # 改名/复制条目后面跟着原路径
    return changed


def _commit_paths(root: Path, paths: list[str], message: str) -> str:
    """只提交 paths 涉及的改动，索引里 agent 已暂存的其他改动不带进来。

    兼容入口：实现收进写入事务（kernel/writepath，方案 C1），按提交说明前缀补来源 trailer。
    新代码用 ChangeTx。没有改动时不提交、返回空串，否则返回新提交。调用方持项目锁。
    """
    from ...kernel.writepath.tx import commit_compat

    return commit_compat(Path(root), list(paths), message)


def _head_commit(root: Path) -> str:
    return _git(root, "rev-parse", "--verify", "HEAD").strip()


def _commit_count(root: Path, rev: str = "HEAD") -> int:
    return int(_git(root, "rev-list", "--count", rev).strip())


_EXCLUDE_STATE_PATHSPECS = tuple(f":(exclude){path}" for path in UNTRACKED_STATE_PATHS)


def _gitignore_missing(text: str) -> list[str]:
    present = {line.strip().strip("/") for line in text.splitlines()}
    return [entry for entry in UNTRACKED_GITIGNORE_ENTRIES if entry.strip("/") not in present]


def _with_gitignore_entries(text: str) -> str:
    missing = _gitignore_missing(text)
    if not missing:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + _GITIGNORE_STATE_HEADER + "".join(f"{entry}\n" for entry in missing)


def _ensure_gitignore_entries(root: Path) -> bool:
    """工作区 .gitignore 补齐元数据与工具状态的排除项（不动已有内容）。"""
    path = Path(root) / ".gitignore"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = _with_gitignore_entries(current)
    if updated == current:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _untrack_state_in_index(root: Path) -> None:
    """只从索引摘除元数据与工具状态，工作区文件保留。"""
    _ensure_gitignore_entries(root)
    _git(
        root, "rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--",
        *UNTRACKED_STATE_PATHS,
    )


def _untrack_state_commit(root: Path) -> bool:
    """在 HEAD 上提交「停止跟踪元数据与工具状态」，只含这几个路径与 .gitignore；幂等。

    提交树由临时索引从 HEAD 构造，不经过真实索引：工作区里已暂存、未暂存的其他改动
    都不会被带进这个提交。HEAD 在构造期间被移动时 update-ref 校验失败，什么都不改。
    """
    root = Path(root)
    if not (root / ".git").is_dir():
        return False
    head = _run_git(root, ("rev-parse", "--verify", "-q", "HEAD")).stdout.strip()
    if not head:
        return False
    tracked = _git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", *UNTRACKED_STATE_PATHS)
    shown = _run_git(root, ("show", "HEAD:.gitignore"))
    head_gitignore = shown.stdout if shown.returncode == 0 else ""
    new_gitignore = _with_gitignore_entries(head_gitignore)
    if not tracked.strip() and new_gitignore == head_gitignore:
        # HEAD 已迁移；agent 若又把元数据暂存进索引，只从索引摘掉
        if _git(root, "ls-files", "--", *UNTRACKED_STATE_PATHS).strip():
            _untrack_state_in_index(root)
        else:
            _ensure_gitignore_entries(root)
        return False

    blob = ""
    if new_gitignore != head_gitignore:
        blob = _git(root, "hash-object", "-w", "--stdin", input=new_gitignore).strip()
    with tempfile.TemporaryDirectory(prefix="onto-untrack-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(root, "read-tree", head, env=env)
        _git(
            root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
            *UNTRACKED_STATE_PATHS, env=env,
        )
        if blob:
            _git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},.gitignore", env=env)
        tree = _git(root, "write-tree", env=env).strip()
    commit = _git(root, "commit-tree", tree, "-p", head, "-m", UNTRACK_COMMIT_MESSAGE).strip()
    _git(root, "update-ref", "-m", UNTRACK_COMMIT_MESSAGE, "HEAD", commit, head)

    # 真实索引跟上新 HEAD：摘掉这几个路径；.gitignore 被单独暂存过时不覆盖暂存内容
    _git(
        root, "rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--",
        *UNTRACKED_STATE_PATHS,
    )
    if blob:
        staged = _run_git(root, ("rev-parse", "-q", "--verify", ":.gitignore")).stdout.strip()
        old_blob = _run_git(root, ("rev-parse", "-q", "--verify", f"{head}:.gitignore")).stdout.strip()
        if staged == old_blob:
            _git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},.gitignore")
        path = root / ".gitignore"
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        path.write_text(
            new_gitignore if current == head_gitignore else _with_gitignore_entries(current),
            encoding="utf-8",
        )
    return True
