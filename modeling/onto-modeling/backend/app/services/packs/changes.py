"""插件变更作为变更集（详设 6.9 第 2 步、8.1 第 2 步、14#7；契约 3.10「插件变更的一次保存也是变更集，
校验通过即合入，接口上不可见」；ADR-0065）。

用户在项目里启用、升级、回滚、停用包，改的是 `packs.lock` 与 `flow.lock`：装配检查（兼容、名字、流程组装）就是
这类变更的门禁，过了就经写入事务合入主线，提交带 `Onto-Changeset`；变更集记录直接是已合入（`source="plugin"`），
撤回用变更集的 `revert_merged`（在主线写反向提交，之后锁文件又被改过时不撤）。

插件变更集不占「没关闭的变更集」那个位置（`store.record_closed`，不动 current）：提交前先记成已关闭、标
`committing`，提交后改成已合入；没提交成就留在已撤回。崩在提交之后、改记录之前的，启动核对按 `Onto-Changeset`
补记（`kernel/changesets/service.reconcile_committing`）。

助手的修改还没确认或撤回时，这次切换会改流程节点（flow.lock 变了）——节点的写入范围、待确认节点都按流程算，
换流程会让那组修改的范围和状态对不上——回 `423 write_scope_locked`，先确认或撤回；不改流程的切换照常合入、
照常记变更集。
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from . import lockfile
from .service import PackError

_log = logging.getLogger(__name__)


def begin(manager, project_id: str, *, purpose: str, pack: str, kind: str, flow_changed: bool) -> dict | None:
    """切换前记一个插件变更集（调用方持项目锁）：编号要先有，提交的 trailer 带它；助手的变更集没关闭时见模块说明。"""
    from ...kernel.changesets import service as changesets
    from ...kernel.changesets import store

    root = manager.project_root(project_id)
    if not (root / ".git").is_dir():
        return None
    if flow_changed and store.current(root) is not None:
        raise PackError("write_scope_locked", "助手的修改还没确认生效或撤回，先处理完再改流程", 423,
                        changesets.scope_lock(manager, project_id))
    from .. import mainline

    return store.record_closed(root, source="plugin", build_mode=None, purpose=purpose, state="discarded",
                               committing=True, base=mainline.state(root)["head"], pack=pack, switch=kind,
                               paths=[lockfile.LOCK_REL])


def finish(manager, project_id: str, record: dict | None, commit: str) -> dict | None:
    """切换提交之后：变更集记成已合入；没有提交（锁文件没变）时留在已撤回。"""
    from ...kernel.changesets import store

    if record is None:
        return None
    root = manager.project_root(project_id)
    now = time.time()
    if not commit:
        return store.update(root, record["id"], state="discarded", closed_at=now, empty=True, committing=False)
    from .. import mainline

    paths = _changed(root, commit)
    label = mainline.label(mainline.count(root, commit))
    return store.update(root, record["id"], state="merged", closed_at=now, paths=paths, committing=False,
                        changes=[["M", rel] for rel in paths],
                        merged={"commit": commit, "version_label": label, "at": now})


def abort(manager, project_id: str, record: dict | None) -> None:
    from ...kernel.changesets import store

    if record is not None:
        store.update(manager.project_root(project_id), record["id"], state="discarded", closed_at=time.time(),
                     committing=False)


def _changed(root: Path, commit: str) -> list[str]:
    result = subprocess.run(["git", "-c", "core.quotepath=false", "show", "--name-only", "--format=", commit],
                            cwd=root, capture_output=True, text=True, timeout=30)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _last_lock_commit(root: Path) -> str:
    result = subprocess.run(["git", "log", "-1", "--format=%H", "--", lockfile.LOCK_REL],
                            cwd=root, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def latest_for(manager, project_id: str, pack: str) -> dict | None:
    """这个包最近一次、还没撤回、而且仍是锁文件最后一次改动的插件变更集（PL-10 撤销用）。"""
    from ...kernel.changesets import service as changesets

    root = manager.project_root(project_id)
    last = _last_lock_commit(root)
    for record in changesets.records(manager, project_id, states=["merged"], sources=["plugin"]):
        if record.get("pack") != pack or record.get("reverted"):
            continue
        return record if (record.get("merged") or {}).get("commit") == last else None
    return None


def revert(manager, project_id: str, record: dict, *, actor: str) -> dict:
    """撤回一个插件变更集：变更集的反向提交（锁文件与 flow.lock 回到之前）。"""
    from ...kernel.changesets import service as changesets

    return changesets.revert_merged(manager, project_id, record["id"], actor=actor)
