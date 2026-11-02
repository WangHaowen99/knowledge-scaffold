"""助手副本（详设 8.2「隔离」；方案 C7）：助手在 git worktree 副本里改，用户看不到，主线只由合入写。

- 路径每个项目固定：`<数据目录>/changesets/<项目>/assistant`。Claude 的续接记录按工作目录存，路径一变
  续接就丢；分支固定为 `onto/assistant`，副本删了重建时接着原分支（没确认的改动还在）。
- `resource/` 用 sparse-checkout 排除，再挂一条指向主线工作区 `resource/` 的链接：资料各副本共享、
  上传后立刻能读；写入由内核守卫拦（受保护目录），链接本身在副本自己的排除规则里，`git add -A` 带不进去
  （`sparse.expectFilesOutsideOfPatterns`：经链接能读到的资料不当成「出现在副本里的稀疏外文件」）。
- `.claude` 单独挂：指向主线工作区的 `.claude` 链接（链到链），装配切换一次两边同时生效；
  `CLAUDE.md`、`README.md` 同主线，是指向 `.claude/` 里的相对链接。
- `.ontology-state/`、`.onto/run/` 每个副本一份（不进 git）。建副本、撤回时把主线上的验收回执带过来，
  合入时把副本里新出的回执带回主线（回执绑定内容指纹，带过去不会误判）。

调用方持项目锁（`manager._project_lock`）：建、修、重置副本都会动主线仓库的 `.git/worktrees/`。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from ... import config
from ...services.workspace.errors import WorkspaceError
from ...services.workspace.gitops import STALE_GIT_LOCK_SECONDS, _git, _run_git

_log = logging.getLogger(__name__)

COPY_NAME = "assistant"
BRANCH = "onto/assistant"
SPARSE_PATTERNS = ("/*", "!/resource/")
EXCLUDE_FILE = "onto-exclude"
# 副本里不进 git 的链接（主线的 .gitignore 已排除 .claude、CLAUDE.md、README.md；resource 是稀疏外的目录名）
EXCLUDES = ("/resource", "/.claude", "/CLAUDE.md", "/README.md")
STATE_DIR = ".ontology-state"
# 建副本、撤回时从主线带过来，合入时带回主线的派生回执（锁、Studio 复核草稿、后端自己管的求解回执不带）
RECEIPTS = ("research-review.json", "research-inventory.json", "research-baseline.json", "script-verifications.json")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def data_dir_of(root: Path) -> Path:
    """主线工作区 `<数据目录>/workspaces/<项目>` 所在的数据目录（测试里的工作区管理器可能不在 config.DATA_DIR）。"""
    root = Path(root)
    return root.parent.parent if root.parent.name == "workspaces" else Path(config.DATA_DIR)


def copy_path(root: Path) -> Path:
    root = Path(root)
    return data_dir_of(root) / "changesets" / root.name / COPY_NAME


def is_copy(path: Path | None) -> bool:
    if path is None:
        return False
    path = Path(path)
    return path.name == COPY_NAME and path.parent.parent.name == "changesets"


def main_root(path: Path | None) -> Path | None:
    """副本路径 → 主线工作区；本来就是主线工作区（或别的目录）原样返回。"""
    if path is None or not is_copy(path):
        return path
    path = Path(path)
    return path.parent.parent.parent / "workspaces" / path.parent.name


def project_id_of(path: Path) -> str:
    """副本或主线工作区所属的项目 ID（目录名）。"""
    path = Path(path)
    return path.parent.name if is_copy(path) else path.name


def exists(root: Path) -> bool:
    return (copy_path(root) / ".git").is_file()


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git(path: Path, *args: str, env: dict | None = None) -> str:
    return _git(Path(path), *args, env=env)


def rev(path: Path, ref: str = "HEAD") -> str:
    result = _run_git(Path(path), ("rev-parse", "--verify", "-q", f"{ref}^{{commit}}"))
    return result.stdout.strip() if result.returncode == 0 else ""


def _branch_exists(root: Path) -> bool:
    return bool(rev(root, f"refs/heads/{BRANCH}"))


def _gitdir(copy: Path) -> Path | None:
    result = _run_git(copy, ("rev-parse", "--absolute-git-dir"))
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def healthy(root: Path) -> bool:
    """副本在、是本项目仓库登记的 worktree、检出的是副本分支。"""
    copy = copy_path(root)
    if not (copy / ".git").is_file():
        return False
    top = _run_git(copy, ("rev-parse", "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD"))
    if top.returncode != 0:
        return False
    lines = top.stdout.strip().splitlines()
    if len(lines) != 3:
        return False
    toplevel, common, branch = lines
    common_dir = Path(common) if os.path.isabs(common) else copy / common
    try:
        same_repo = common_dir.resolve() == (Path(root) / ".git").resolve()
        same_top = Path(toplevel).resolve() == copy.resolve()
    except OSError:
        return False
    return same_repo and same_top and branch == BRANCH


# ---------------------------------------------------------------------------
# 建、修
# ---------------------------------------------------------------------------


def ensure(root: Path) -> Path:
    """副本可用就补齐链接后返回；不在或坏了就（接着原分支）重建。调用方持项目锁。"""
    root = Path(root)
    copy = copy_path(root)
    if healthy(root):
        _ensure_links(root, copy)
        return copy
    _create(root, copy)
    return copy


def _create(root: Path, copy: Path) -> None:
    if not (root / ".git").is_dir() or not rev(root):
        raise WorkspaceError("项目还没有版本库，不能建助手副本", 409, "changeset_unavailable")
    _run_git(root, ("worktree", "prune"))
    if copy.exists() or copy.is_symlink():
        # 建到一半崩溃、或被人改坏的副本：git 状态已经对不上，留着只会让 worktree add 失败
        _log.warning("项目 %s 的助手副本不可用，重建：%s", root.name, copy)
        if copy.is_symlink() or copy.is_file():
            copy.unlink()
        else:
            shutil.rmtree(copy, ignore_errors=True)
    copy.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(root):
        # 接着原分支：上次没确认的改动还在
        git(root, "worktree", "add", "-f", "--no-checkout", str(copy), BRANCH)
    else:
        git(root, "worktree", "add", "-f", "--no-checkout", "-b", BRANCH, str(copy), "HEAD")
    git(copy, "sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS)
    git(copy, "config", "--worktree", "sparse.expectFilesOutsideOfPatterns", "true")
    gitdir = _gitdir(copy)
    if gitdir is not None:
        (gitdir / EXCLUDE_FILE).write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")
        git(copy, "config", "--worktree", "core.excludesFile", str(gitdir / EXCLUDE_FILE))
    # 先按稀疏规则检出（资料条目标成不检出），再挂资料链接
    git(copy, "read-tree", "-mu", "HEAD")
    _ensure_links(root, copy)
    seed_state(root, copy)
    _share_with_agent(copy)


def _link(path: Path, target: str) -> None:
    if path.is_symlink() and os.readlink(path) == target:
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    tmp = path.with_name(f".{path.name}.link-tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, path)


def _ensure_links(root: Path, copy: Path) -> None:
    for name in (".claude", "resource"):
        if os.path.lexists(root / name):
            _link(copy / name, os.path.relpath(root / name, copy))
    for name in ("CLAUDE.md", "README.md"):
        if os.path.lexists(root / name):
            _link(copy / name, f".claude/{name}")


def _share_with_agent(copy: Path) -> None:
    """agent 用户要能写副本（切用户时）：沿用工作区的组与 setgid 规则。"""
    try:
        from ...governance import agent_identity

        agent_identity.share_writable(copy)
    except Exception:  # noqa: BLE001 - 权限整理失败只影响 agent 用户能否写，记日志
        _log.warning("助手副本权限整理失败：%s", copy, exc_info=True)


def seed_state(root: Path, copy: Path) -> None:
    """主线上的验收回执带进副本（建副本、撤回之后）。"""
    _copy_receipts(Path(root) / STATE_DIR, Path(copy) / STATE_DIR)


def collect_state(root: Path, copy: Path) -> list[str]:
    """合入时把副本里新出的回执带回主线；返回带回的文件名。"""
    return _copy_receipts(Path(copy) / STATE_DIR, Path(root) / STATE_DIR)


def _copy_receipts(source: Path, dest: Path) -> list[str]:
    copied = []
    for name in RECEIPTS:
        src = source / name
        if src.is_symlink() or not src.is_file():
            continue
        target = dest / name
        try:
            data = src.read_bytes()
            if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{name}.copy-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            copied.append(name)
        except OSError:
            _log.warning("带回执失败：%s → %s", src, target)
    return copied


# ---------------------------------------------------------------------------
# 读副本的状态
# ---------------------------------------------------------------------------


def snapshot(copy: Path) -> str:
    """副本当前文件（含没提交、没跟踪的）的树对象；不碰副本的真实索引（拷一份索引来用）。"""
    copy = Path(copy)
    gitdir = _gitdir(copy)
    if gitdir is None:
        raise WorkspaceError("助手副本不可用", 500)
    with tempfile.TemporaryDirectory(prefix="onto-cs-") as scratch:
        index = Path(scratch) / "index"
        if (gitdir / "index").is_file():
            shutil.copyfile(gitdir / "index", index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        git(copy, "add", "-A", env=env)
        return git(copy, "write-tree", env=env).strip()


def merge_base(root: Path, copy: Path) -> str:
    result = _run_git(Path(root), ("merge-base", "HEAD", BRANCH))
    return result.stdout.strip() if result.returncode == 0 else rev(root)


def tree_of(path: Path, rev_: str) -> str:
    return git(path, "rev-parse", f"{rev_}^{{tree}}").strip()


def diff(path: Path, old: str, new: str) -> list[tuple[str, str]]:
    """两棵树（或提交）之间改动的文件：[(A|M|D, 路径)]，不认改名。"""
    out = git(path, "-c", "core.quotePath=false", "diff-tree", "-r", "-z", "--no-renames", "--name-status", old, new)
    records = out.split("\0")
    return [(status[:1], rel) for status, rel in zip(records[0::2], records[1::2]) if status and rel]


def dirty(copy: Path) -> bool:
    """副本里有没提交的改动（含没跟踪的文件）。"""
    out = _run_git(Path(copy), ("--no-optional-locks", "status", "--porcelain", "--untracked-files=all"))
    return out.returncode != 0 or bool(out.stdout.strip())


def read_blob(path: Path, treeish: str, rel: str) -> bytes | None:
    import subprocess

    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "--literal-pathspecs", "cat-file", "blob",
                             f"{treeish}:{rel}"], cwd=path, capture_output=True, timeout=60)
    return result.stdout if result.returncode == 0 else None


# ---------------------------------------------------------------------------
# 跟上主线、收拢、重置
# ---------------------------------------------------------------------------


def sync(root: Path, copy: Path) -> str:
    """一轮开始前让副本跟上主线（详设 8.2 风险应对「没有冲突时自动跟上主线」）。

    - 副本相对基准没有改动：直接重置到主线最新，返回 "reset"；
    - 有改动、没有没提交的文件、主线前进了：把主线合进副本分支（不冲突时），返回 "merged"；
    - 其余（有没提交的文件、会冲突、主线没动）不动，返回 "kept"。
    """
    main = rev(root)
    head = rev(copy)
    if not main or main == head:
        return "kept"
    base = merge_base(root, copy)
    snap = snapshot(copy)
    if base and snap == tree_of(root, base):
        git(copy, "reset", "-q", "--hard", main)
        seed_state(root, copy)
        return "reset"
    if base == main or dirty(copy):
        return "kept"
    probe = _run_git(copy, ("merge-tree", "--write-tree", "--name-only", "--no-messages", main, head))
    if probe.returncode != 0:
        return "kept"
    result = _run_git(copy, ("merge", "-q", "--no-edit", "--no-ff", "-m", "sync: 跟上主线", main))
    if result.returncode != 0:
        _run_git(copy, ("merge", "--abort"))
        _log.warning("项目 %s 的助手副本跟主线合不上：%s", root.name, result.stderr.strip())
        return "kept"
    return "merged"


def commit_pending(copy: Path, message: str) -> str:
    """副本里没提交的改动收成一个提交（确认生效之前）；没有改动返回空串。"""
    if not dirty(copy):
        return ""
    git(copy, "add", "-A")
    if not _run_git(copy, ("diff", "--cached", "--quiet")).returncode:
        return ""
    git(copy, "commit", "-q", "-m", message)
    return rev(copy)


def reset(root: Path, copy: Path, to: str | None = None) -> None:
    """撤回、合入之后：副本分支重置到主线（或指定提交），丢掉没提交的文件，回执重新从主线带。"""
    target = to or rev(root)
    _run_git(copy, ("merge", "--abort"))
    git(copy, "reset", "-q", "--hard", target)
    # 没跟踪的文件一并清掉；链接与 .ontology-state、.onto/run 在排除规则或 .gitignore 里，不受影响
    git(copy, "clean", "-fdq")
    seed_state(root, copy)


def remove(root: Path) -> None:
    """删掉副本与副本分支（删除项目时由删除流程整体处理，这里给测试与运维用）。"""
    copy = copy_path(root)
    _run_git(Path(root), ("worktree", "remove", "--force", str(copy)))
    shutil.rmtree(copy, ignore_errors=True)
    _run_git(Path(root), ("worktree", "prune"))
    _run_git(Path(root), ("branch", "-D", BRANCH))


# ---------------------------------------------------------------------------
# 启动恢复（详设 8.3；方案 C7「崩溃恢复要登记副本的核对与修复」）
# ---------------------------------------------------------------------------


def _clear_stale_locks(gitdir: Path) -> int:
    removed = 0
    for name in ("index.lock", "HEAD.lock"):
        lock = gitdir / name
        try:
            if lock.is_symlink() or time.time() - lock.stat().st_mtime <= STALE_GIT_LOCK_SECONDS:
                continue
            lock.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def repair(root: Path) -> str | None:
    """核对一个项目的副本：残留锁、登记丢了、目录坏了、链接指错。返回做了什么（没做返回 None）。"""
    root = Path(root)
    copy = copy_path(root)
    done = []
    gitdir = root / ".git" / "worktrees" / COPY_NAME
    if gitdir.is_dir() and _clear_stale_locks(gitdir):
        done.append("清残留锁")
    if not copy.exists() and not _branch_exists(root):
        if gitdir.is_dir():
            _run_git(root, ("worktree", "prune"))
            done.append("清登记")
        return "、".join(done) or None
    if not healthy(root):
        if copy.exists() or _branch_exists(root):
            _create(root, copy)
            done.append("重建副本")
    else:
        before = {name: os.readlink(copy / name) if (copy / name).is_symlink() else None
                  for name in (".claude", "resource", "CLAUDE.md", "README.md")}
        _ensure_links(root, copy)
        after = {name: os.readlink(copy / name) if (copy / name).is_symlink() else None for name in before}
        if before != after:
            done.append("补链接")
    return "、".join(done) or None


def repair_all(manager) -> dict | None:
    """启动恢复步骤 changeset_copies（O-35）：修了或修不了才进报告（同 agent_commit_hooks），都完好时返回 None。"""
    repaired, failed = [], []
    for root in manager._project_dirs():
        if not (root / ".git").is_dir():
            continue
        try:
            with manager._project_lock(root.name):
                action = repair(root)
        except Exception as exc:  # noqa: BLE001 - 单个项目修不了不挡启动
            failed.append(root.name)
            _log.warning("项目 %s 的助手副本修复失败：%s", root.name, exc)
            continue
        if action:
            repaired.append({"project_id": root.name, "count": 1, "paths": [action]})
    if failed:
        result, tone = "failed", "warn"
        detail = f"{len(failed)} 个项目的助手副本修不了，助手会退回在主线工作区里改"
    elif repaired:
        result, tone = "cleared", "fix"
        detail = f"修复了 {len(repaired)} 个项目的助手副本"
    else:
        return None
    return {"id": "changeset_copies", "title": "助手副本", "detail": detail, "result": result, "tone": tone,
            "projects": repaired[:50], "failed": failed}
