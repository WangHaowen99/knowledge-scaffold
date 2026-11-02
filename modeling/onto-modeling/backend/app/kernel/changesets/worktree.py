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

import json
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
# 副本里改不得、合入时也不采用副本版本的路径：资料区是指向主线的链接，锁文件归插件运行时
PROTECTED = ("resource", ".onto")
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


def is_protected(rel: str) -> bool:
    """受保护的路径（资料区、锁文件目录）：副本里的改动不进变更集、合入时一律取主线的版本。"""
    first = str(rel).replace("\\", "/").lstrip("/").split("/", 1)[0]
    return first in PROTECTED


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


def _ours(root: Path) -> str | None:
    """副本是本项目仓库登记的 worktree 时返回它检出的分支名（分离 HEAD 时为 "HEAD"）；不是时 None。"""
    copy = copy_path(root)
    if not (copy / ".git").is_file():
        return None
    top = _run_git(copy, ("rev-parse", "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD"))
    if top.returncode != 0:
        return None
    lines = top.stdout.strip().splitlines()
    if len(lines) != 3:
        return None
    toplevel, common, branch = lines
    common_dir = Path(common) if os.path.isabs(common) else copy / common
    try:
        same_repo = common_dir.resolve() == (Path(root) / ".git").resolve()
        same_top = Path(toplevel).resolve() == copy.resolve()
    except OSError:
        return None
    return branch if same_repo and same_top else None


def healthy(root: Path) -> bool:
    """副本在、是本项目仓库登记的 worktree、检出的是副本分支。"""
    return _ours(root) == BRANCH


# ---------------------------------------------------------------------------
# 建、修
# ---------------------------------------------------------------------------


def ensure(root: Path) -> Path:
    """副本可用就核对稀疏规则、补齐链接后返回；不在副本分支上就接回去；不在或坏了才（接着原分支）重建。
    调用方持项目锁。"""
    root = Path(root)
    copy = copy_path(root)
    branch = _ours(root)
    if branch is not None and branch != BRANCH:
        _back_to_branch(root, copy)
        branch = _ours(root)
    if branch == BRANCH:
        _ensure_sparse(copy)
        _ensure_links(root, copy)
    else:
        _create(root, copy)
    drop_escaping_links(copy)
    drop_escaping_links(root)
    return copy


def _back_to_branch(root: Path, copy: Path) -> None:
    """副本在别的分支、分离 HEAD、合并或变基到一半：收住改动、接回副本分支，什么都不丢（审查第 2 条）。

    没提交的文件先在当前位置收成一个提交；当前位置包含副本分支就把分支移过去，被副本分支包含就直接切回，
    两边分叉就切回副本分支再把当前位置合进来——合不上时留一个 `refs/onto/rescued/<时间>` 引用，不丢提交。
    """
    for args in (("merge", "--abort"), ("rebase", "--abort"), ("cherry-pick", "--abort"), ("revert", "--abort"),
                 ("am", "--abort")):
        _run_git(copy, args)
    commit_pending(copy, "chore: 收拢副本里没提交的改动")
    current = rev(copy)
    branch_head = rev(root, f"refs/heads/{BRANCH}")
    ancestor = lambda older, newer: _run_git(copy, ("merge-base", "--is-ancestor", older, newer)).returncode == 0  # noqa: E731
    if not branch_head or (current and ancestor(branch_head, current)):
        git(copy, "checkout", "-q", "-B", BRANCH, current or "HEAD")
    elif not current or ancestor(current, branch_head):
        git(copy, "checkout", "-q", BRANCH)
    else:
        rescued = f"refs/onto/rescued/{int(time.time())}"
        git(copy, "update-ref", rescued, current)
        git(copy, "checkout", "-q", BRANCH)
        merged = _run_git(copy, ("merge", "-q", "--no-edit", "--no-ff", "-m", "chore: 接回副本里另一处的提交", current))
        if merged.returncode != 0:
            _run_git(copy, ("merge", "--abort"))
            _log.warning("项目 %s 的副本里另一处的提交接不回副本分支，留在 %s", root.name, rescued)
    _log.warning("项目 %s 的助手副本不在副本分支上，已接回", root.name)


def _ensure_sparse(copy: Path) -> None:
    """每轮核对：稀疏规则还在、资料条目仍标成不检出（助手关了稀疏时，git 经链接看到资料，会把它们当成删了）。"""
    enabled = _run_git(copy, ("config", "--worktree", "--get", "core.sparseCheckout")).stdout.strip() == "true"
    patterns = _run_git(copy, ("sparse-checkout", "list")).stdout.split()
    listed = _run_git(copy, ("-c", "core.quotePath=false", "ls-files", "-t", "--", "resource")).stdout.splitlines()
    if enabled and patterns == list(SPARSE_PATTERNS) and all(line.startswith("S ") for line in listed):
        return
    link = copy / "resource"
    target = os.readlink(link) if link.is_symlink() else None
    if link.is_symlink():
        link.unlink()  # 不经链接：重设稀疏规则时 git 只看副本自己的目录
    try:
        git(copy, "sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS)
        git(copy, "config", "--worktree", "sparse.expectFilesOutsideOfPatterns", "true")
        unmarked = [line[2:] for line in _run_git(copy, ("-c", "core.quotePath=false", "ls-files", "-t", "-z", "--",
                                                         "resource")).stdout.split("\0") if line and not line.startswith("S ")]
        if unmarked:
            git(copy, "--literal-pathspecs", "update-index", "--skip-worktree", "--", *unmarked)
    finally:
        if target is not None and not os.path.lexists(link):
            os.symlink(target, link)
    _log.warning("助手副本的稀疏规则被改过，已重设：%s", copy)


def _create(root: Path, copy: Path) -> None:
    if not (root / ".git").is_dir() or not rev(root):
        raise WorkspaceError("项目还没有版本库，不能建助手副本", 409, "changeset_unavailable")
    _run_git(root, ("worktree", "prune"))
    if copy.exists() or copy.is_symlink():
        # 建到一半崩溃、或被人改坏的副本：git 状态已经对不上。挪到旁边留着（里面可能有没提交的东西），不直接删
        if copy.is_symlink() or copy.is_file():
            copy.unlink()
        else:
            aside = copy.with_name(f"{COPY_NAME}.broken-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
            copy.rename(aside)
            _log.warning("项目 %s 的助手副本不可用，原目录挪到 %s 后重建", root.name, aside)
            _prune_broken(copy.parent)
        _run_git(root, ("worktree", "prune"))
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


BROKEN_KEEP = 3


def _prune_broken(directory: Path) -> None:
    """挪到旁边的坏副本只留最近几份。"""
    broken = sorted(path for path in directory.glob(f"{COPY_NAME}.broken-*") if path.is_dir())
    for path in broken[:-BROKEN_KEEP]:
        shutil.rmtree(path, ignore_errors=True)


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


# 工作区与副本里本来就有、指向根外的链接（装配目录、副本里的资料区）
KEEP_LINKS = (".claude", "resource", "CLAUDE.md", "README.md")


def drop_escaping_links(base: Path, keep: tuple[str, ...] = KEEP_LINKS) -> list[str]:
    """删掉工作区（或副本）里指向根外的符号链接，返回删掉的相对路径。

    助手能在工作区里建任意链接（守卫拦不住 `tar`、`cp -s` 这类写法），后端按路径读文件的接口跟着链接就会读到
    密钥、别的项目。只扫一遍目录树（不进链接目录、不读文件内容），跳过 `.git`；白名单里的顶层链接与指向根内的链接不动。
    """
    base = Path(base)
    real_base = os.path.realpath(base)
    removed: list[str] = []
    for current, dirs, files in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(current, base)
        if rel_dir == ".":
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in [*dirs, *files]:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                continue
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            real = os.path.realpath(path)
            if rel in keep or real == real_base or real.startswith(real_base + os.sep):
                continue
            try:
                os.unlink(path)
                removed.append(rel)
            except OSError:
                _log.warning("删不掉指向工作区外的符号链接：%s", path, exc_info=True)
    if removed:
        _log.warning("%s 里有 %d 个指向工作区外的符号链接，已删除：%s", base.name, len(removed), "、".join(removed[:10]))
    return removed


def _share_with_agent(copy: Path) -> None:
    """agent 用户要能写副本（切用户时）：沿用工作区的组与 setgid 规则。"""
    try:
        from ...governance import agent_identity

        agent_identity.share_writable(copy)
    except Exception:  # noqa: BLE001 - 权限整理失败只影响 agent 用户能否写，记日志
        _log.warning("助手副本权限整理失败：%s", copy, exc_info=True)


def seed_state(root: Path, copy: Path) -> None:
    """主线上比副本新的验收回执带进副本（建副本、跟上主线、撤回之后）；副本里更新的不被旧的盖掉。"""
    _copy_receipts(Path(root) / STATE_DIR, Path(copy) / STATE_DIR)


def collect_state(root: Path, copy: Path) -> list[str]:
    """把副本里更新的回执带回主线（每轮收尾、合入时）；返回带回的文件名。

    回执绑定内容指纹，不随变更集走：只跑了验证、没改文件的一轮也要带回去，否则下一轮就被主线上的旧回执盖掉。
    """
    return _copy_receipts(Path(copy) / STATE_DIR, Path(root) / STATE_DIR)


def _copy_receipts(source: Path, dest: Path) -> list[str]:
    """只拷比目标新的（按修改时间，拷时保留时间）：两边来回同步不会拿旧的盖新的。"""
    copied = []
    for name in RECEIPTS:
        src = source / name
        if src.is_symlink() or not src.is_file():
            continue
        target = dest / name
        try:
            data = src.read_bytes()
            if target.is_file() and not target.is_symlink():
                if target.read_bytes() == data or target.stat().st_mtime_ns >= src.stat().st_mtime_ns:
                    continue
            dest.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{name}.copy-tmp")
            tmp.write_bytes(data)
            stat = src.stat()
            os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(tmp, target)
            copied.append(name)
        except OSError:
            _log.warning("带回执失败：%s → %s", src, target)
    return copied


RUN_STATE = (".onto/run/active.json",)


def clear_run_state(copy: Path, commands: list[str] | None = None) -> None:
    """撤回、放弃、启动恢复时：副本里留下的命令锁与写入范围（`.onto/run/active.json`）一起清掉（审查第 9 条）。

    commands 给出时只清这几条命令的；否则全清。
    """
    copy = Path(copy)
    state = copy / STATE_DIR
    if commands is None:
        for lock in state.glob("lock-*.json") if state.is_dir() else []:
            lock.unlink(missing_ok=True)
        (copy / RUN_STATE[0]).unlink(missing_ok=True)
        return
    for command in commands:
        (state / f"lock-{command}.json").unlink(missing_ok=True)
    active = copy / RUN_STATE[0]
    try:
        data = json.loads(active.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    entries = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        active.unlink(missing_ok=True)
        return
    for command in commands:
        entries.pop(command, None)
    if entries:
        tmp = active.with_name(".active.clear.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, active)
    else:
        active.unlink(missing_ok=True)


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
            # 带着时间戳拷：git 靠索引的修改时间认出「同一秒里检出又改过」的文件，拷成新时间会把同样长度的改动当成没改
            shutil.copy2(gitdir / "index", index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        git(copy, "add", "-A", env=env)
        _keep_protected(copy, env)
        return git(copy, "write-tree", env=env).strip()


def _keep_protected(copy: Path, env: dict | None, *, mark_skip: bool = False) -> None:
    """受保护目录在索引里恢复成 HEAD 的样子（`git add -A` 之后调）：资料区是链接，稀疏规则被关时 git 会把链接
    后面的资料当成删了；锁文件归插件运行时。mark_skip：真实索引里资料条目重新标成不检出（审查第 1 条）。"""
    import subprocess

    base = ["git", "-c", "core.hooksPath=/dev/null"]
    run_env = env or None
    listed = subprocess.run([*base, "ls-tree", "-r", "-z", "HEAD", "--", *PROTECTED], cwd=copy, capture_output=True,
                            env=run_env, timeout=60)
    head_paths = {entry.split(b"\t", 1)[1] for entry in listed.stdout.split(b"\0") if b"\t" in entry}
    indexed = subprocess.run([*base, "ls-files", "-z", "--", *PROTECTED], cwd=copy, capture_output=True, env=run_env,
                             timeout=60)
    extra = [path for path in indexed.stdout.split(b"\0") if path and path not in head_paths]
    if extra:
        subprocess.run([*base, "update-index", "-z", "--force-remove", "--stdin"], cwd=copy, input=b"\0".join(extra),
                       capture_output=True, env=run_env, timeout=60)
    if listed.stdout:
        subprocess.run([*base, "update-index", "-z", "--index-info"], cwd=copy, input=listed.stdout,
                       capture_output=True, env=run_env, timeout=60)
    resources = [path for path in head_paths if path.startswith(b"resource/")]
    if mark_skip and resources:
        subprocess.run([*base, "update-index", "-z", "--skip-worktree", "--stdin"], cwd=copy,
                       input=b"\0".join(resources), capture_output=True, env=run_env, timeout=60)


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
    _keep_protected(copy, None, mark_skip=True)
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


def strip_copy_metadata(root: Path) -> None:
    """克隆、迁移导入带过来的 `.git` 里有源项目的副本登记与副本分支（没确认的改动）：一并去掉，
    新项目的副本从自己的主线重新建。"""
    root = Path(root)
    shutil.rmtree(root / ".git" / "worktrees", ignore_errors=True)
    if _branch_exists(root) and _run_git(root, ("symbolic-ref", "-q", "--short", "HEAD")).stdout.strip() != BRANCH:
        _run_git(root, ("branch", "-D", BRANCH))


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
    branch = _ours(root)
    if branch is not None and branch != BRANCH:
        _back_to_branch(root, copy)
        done.append("接回副本分支")
    if not healthy(root):
        if copy.exists() or _branch_exists(root):
            _create(root, copy)
            done.append("重建副本")
    else:
        _ensure_sparse(copy)
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
