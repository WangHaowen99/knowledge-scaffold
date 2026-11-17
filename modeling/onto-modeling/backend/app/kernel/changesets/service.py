"""变更集服务（详设 8.2、4.3；方案 C7～C12；契约 3.10 CS-01～CS-07、EP-03、EP-04）。

陪伴建模下一个变更集是助手的一轮修改（F3 起不再等用户确认生效，用户 11-03 定；变更集是用户看不到的内部机制）：

    （副本里有了改动）──→ open（助手在跑）──→ pending_confirm（这一轮结束，合入前的一瞬）──自动合入──→ merged
    pending_confirm ──合入时实体冲突──→ conflict（进待处理）──选好保留哪一边──→ merged
    merged ──撤回这一轮──→ merged + reverted（主线上写反向提交，回到上一个版本）

- 观察（`observe`）：一轮结束（和跑的过程中节流地）看一眼副本，有改动就开或更新当前变更集；
- 收尾（`settle`）：一轮结束（成功、出错、被打断）、下一轮开始前、启动时把没关闭的变更集合入成一个版本；
- 合入（`merge`）：`merge-tree --write-tree` 叠到最新主线 → 按实体检测冲突 → 合成树上跑检查（只看新增问题，
  只记录不拦，发布时才拦）→ 经写入事务（ChangeTx）在主线写一个 squash 提交，trailer 写在这个合入提交上；
- 撤回这一轮（`revert_round`）：合入过的写反向提交（之后又被改过的不硬撤）；冲突中还没合入的重置副本；
- 撤回已合入的变更集（`revert_merged`，也给 P3 自主构建撤回一步）：在主线上写反向提交；
- 写入范围锁（`scope_lock`）：助手在跑时节点声明的 `produces`，加副本里已改的实体与产物，加等你决定的
  决策卡改动预览里列出的条目，持续到这一轮合入（冲突时到选好保留哪边）。

会话侧的状态（助手在不在跑、哪条对话、正在执行的节点、决策卡预览）由宿主登记的数据源给
（`register_activity_provider`），内核不依赖会话池。对话里的卡片与提示行由调用方在事件循环里追加
（`conversation_records`）：会话的订阅队列不是线程安全的。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from ...services import workspace_labels
from ...services.workspace.errors import WorkspaceError
from ...services.workspace.gitops import _git, _run_git
from . import entity_diff, gates, store, worktree

_log = logging.getLogger(__name__)

CHANGES_MAX = 500
GROUP_LABELS = {"object": "对象", "operation": "操作", "rule": "规则", "metric": "指标", "script": "脚本",
                "scenario": "场景", "artifact": "插件产物", "other": "其他"}
GROUP_ORDER = tuple(GROUP_LABELS)
OBSERVE_EVERY_SECONDS = 5.0
_last_observed: dict[str, float] = {}


# ---------------------------------------------------------------------------
# 登记点：会话侧的状态、变化通知
# ---------------------------------------------------------------------------

_activity_provider: Callable[[str], dict] | None = None
_listeners: list[Callable[[str, str, dict], None]] = []


def register_activity_provider(provider: Callable[[str], dict] | None) -> None:
    """会话宿主登记：项目 → {running, conversation_id, exec: 执行记录|None, affected: [EntryRef|ArtifactRef]}。"""
    global _activity_provider
    _activity_provider = provider


def activity(project_id: str) -> dict:
    empty = {"running": False, "conversation_id": None, "exec": None, "affected": []}
    if _activity_provider is None:
        return empty
    try:
        return {**empty, **(_activity_provider(project_id) or {})}
    except Exception:  # noqa: BLE001 - 取不到按助手没在跑处理
        _log.exception("取会话状态失败（项目 %s）", project_id)
        return empty


def subscribe(listener: Callable[[str, str, dict], None]) -> None:
    """变化通知：listener(项目, 事件, 数据)；事件取 changeset（变更集变了）、scope（写入范围可能变了）。"""
    if listener not in _listeners:
        _listeners.append(listener)


def _notify(project_id: str, event: str, data: dict) -> None:
    for listener in list(_listeners):
        try:
            listener(project_id, event, data)
        except Exception:  # noqa: BLE001 - 通知是增强信息
            _log.exception("变更集通知出错（%s）", event)


# ---------------------------------------------------------------------------
# 副本
# ---------------------------------------------------------------------------


_KEEP_OVERRIDE = object()


def copy_for_turn(manager, project_id: str, root: Path, *, claude_override=_KEEP_OVERRIDE) -> Path:
    """一轮开始前：上一轮没走到收尾的修改先合入（引擎被硬杀、会话被回收），建或修副本、让它跟上主线，
    返回助手的工作目录。

    claude_override：这一轮副本的 .claude 指向哪（ADR-0087）：插件开发装配目录，或 None 回到建模的装配；
    不给时不动（Studio 复核等别处借用副本时，不能把插件对话那一轮的链接改回去）。

    建不了副本（不是 git 仓库、git 出错）时退回主线工作区并记日志：助手照旧能用，只是没有隔离。
    """
    root = Path(root)
    try:
        settle(manager, project_id)
    except Exception:  # noqa: BLE001 - 合不进去的留在变更集里，不挡这一轮
        _log.warning("项目 %s 上一轮遗留的修改没合入", project_id, exc_info=True)
    try:
        with manager._project_lock(project_id):
            if claude_override is not _KEEP_OVERRIDE:
                worktree.set_override(root, claude_override)
            copy = worktree.ensure(root)
            worktree.sync(root, copy)
        return copy
    except Exception:  # noqa: BLE001
        _log.exception("项目 %s 的助手副本不可用，这一轮在主线工作区里跑", project_id)
        return root


def existing_copy(root: Path) -> Path | None:
    copy = worktree.copy_path(root)
    return copy if (copy / ".git").is_file() else None


def staging_dir(manager, project_id: str) -> Path:
    """后端给助手放材料的地方（Studio 复核草稿，方案 C15）：在副本里，助手才看得到；没有副本时用主线工作区。"""
    root = manager.project_root(project_id)
    return copy_for_turn(manager, project_id, root)


# ---------------------------------------------------------------------------
# 描述改动
# ---------------------------------------------------------------------------


def _model(root: Path):
    from .. import metamodel

    try:
        return metamodel.for_project(root)
    except Exception:  # noqa: BLE001
        return metamodel.mm


def _entry_ref(kind: str, key: str) -> dict:
    return {"kind": kind, "id": f"{kind}:{key}", "key": key, "name": key, "label": workspace_labels.entry_label(kind, key)}


def _file_ref(rel: str) -> dict:
    """冲突、撤回挡住时列出的非条目文件：名字用业务名，认不出的写「其他文件」，不出文件名。"""
    name = workspace_labels.file_label(rel) or "其他文件"
    return {"kind": "file", "id": rel, "key": rel, "name": name, "label": name}


def _artifact_types(manager, root: Path) -> list:
    from ..artifacts.registry import types_for_project

    try:
        return types_for_project(manager, root)
    except Exception:  # noqa: BLE001 - 产物登记读不出时只按条目与文件描述
        return []


def _titled(root: Path, rel: str, ref: dict, types: list) -> None:
    """产物的业务名用文件里的标题（知识条目的 name 等），原来是文件键（如英文的文件名）；读不出（已删、解析不了）时不变。"""
    from ..artifacts import parsers

    atype = next((item for item in types if item.type == ref.get("type")), None)
    if atype is None or not (root / rel).is_file():
        return
    try:
        parsed = parsers.parse_file(atype, root, rel)
    except Exception:  # noqa: BLE001 - 标题只是展示信息，读不出照用文件键
        return
    title = next((item.title for item in parsed if item.key == ref.get("key") and item.title), "")
    if title and title != ref.get("key"):
        ref["title"] = title
        ref["label"] = f"{atype.title}「{title}」" if atype.title else f"「{title}」"


def describe(manager, root: Path, changes: list[tuple[str, str]]) -> dict:
    """改动的路径 → {entries, artifacts, paths, items}；items 是 [{group, change, target, path}]。"""
    model = _model(root)
    types = _artifact_types(manager, root)
    entries, artifacts, items = [], [], []
    seen_entries, seen_artifacts = set(), set()
    for status, rel in changes[:CHANGES_MAX]:
        change = {"A": "add", "D": "del"}.get(status, "mod")
        info = workspace_labels.describe(rel, model, types)
        if info is None:
            # 认不出的文件：不出文件名（界面上合成「另有 N 处改动」），路径只留在 path 里
            items.append({"group": "other", "change": change, "path": rel,
                          "target": {"type": "file", "key": rel, "title": "", "label": ""}})
            continue
        group, ref = info["group"], info["target"]
        if "kind" in ref and ref["id"] not in seen_entries:
            seen_entries.add(ref["id"])
            entries.append(ref)
        elif ref.get("type") != "file" and "kind" not in ref and (ref["type"], ref["key"]) not in seen_artifacts:
            _titled(root, rel, ref, types)
            seen_artifacts.add((ref["type"], ref["key"]))
            artifacts.append(ref)
        items.append({"group": group, "change": change, "target": ref, "path": rel})
    return {"entries": entries, "artifacts": artifacts, "paths": [rel for _status, rel in changes[:CHANGES_MAX]],
            "items": items}


def _escape(rel: str) -> str:
    """路径当 fnmatch 模式用时，把方括号、星号、问号转义成字面量。"""
    return "".join(f"[{char}]" if char in "[]*?" else char for char in rel)


# ---------------------------------------------------------------------------
# 观察
# ---------------------------------------------------------------------------


def _node_trailer(record: dict | None) -> str | None:
    node = (record or {}).get("node") or {}
    node_id = node.get("id") or ""
    if not node_id or node_id.startswith("command."):
        return None
    version = str(node.get("version") or "").rpartition("@")[2]
    return f"{node_id}@{version}" if version else node_id


def observe(manager, project_id: str, *, running: bool, conversation_id: str | None = None,
            exec_record: dict | None = None, checks: bool = False, throttle: bool = False) -> dict | None:
    """看一眼副本：有改动就开（或更新）当前变更集，返回它；没有改动返回 None。

    checks=True 时（一轮结束）在副本的树上跑一遍门禁（相对变更集的基准只看新增问题），结果记进变更集。
    throttle=True 时（跑的过程中）同一项目 OBSERVE_EVERY_SECONDS 内只看一次。
    """
    now = time.monotonic()
    if throttle and now - _last_observed.get(project_id, 0.0) < OBSERVE_EVERY_SECONDS:
        return None
    _last_observed[project_id] = now
    root = manager.project_root(project_id)
    copy = existing_copy(root)
    if copy is None:
        return None
    with manager._project_lock(project_id):
        if not running:
            # 每轮收尾把副本里新出的验收回执带回主线：只跑了验证、没改文件的一轮也要带（审查第 11 条）
            worktree.collect_state(root, copy)
            # 一轮结束：这一轮建的、指向工作区外的符号链接删掉，不进快照、不让后端的读接口跟着走
            worktree.drop_escaping_links(copy)
            worktree.drop_escaping_links(root)
        base = worktree.merge_base(root, copy)
        snap = worktree.snapshot(copy)
        changes = [(status, rel) for status, rel in (worktree.diff(root, base, snap) if base else [])
                   if not worktree.is_protected(rel)]
        record = store.current(root)
        if not changes:
            if record is not None and record["state"] in ("open", "pending_confirm") and record.get("tree"):
                # 助手把改动又改回去了：这个变更集是空的，悄悄关掉。先占着、还没改过的（自主构建的一步）不关
                closed = store.update(root, record["id"], state="discarded", closed_at=time.time(), empty=True)
                _notify(project_id, "changeset", closed)
            return None
        described = describe(manager, root, changes)
        fields = {
            "base": base, "head": worktree.rev(copy), "tree": snap,
            "entries": described["entries"], "artifacts": described["artifacts"], "paths": described["paths"],
            "changes": [[status, rel] for status, rel in changes[:CHANGES_MAX]],
            "entity_count": len(described["entries"]) + len(described["artifacts"]),
        }
        if conversation_id:
            fields["conversation_id"] = conversation_id
        if record is None or record["state"] != "conflict":
            fields["state"] = "open" if running else "pending_confirm"
        previous_state = record["state"] if record else None
        previous_scope = (record.get("entries"), record.get("artifacts"), record.get("paths")) if record else None
        if record is None:
            record = store.create(root, purpose=_purpose(exec_record), **fields)
        else:
            record = store.update(root, record["id"], **fields)
        if exec_record:
            nodes = list(record.get("nodes") or [])
            node_id = (exec_record.get("node") or {}).get("id")
            if node_id and not node_id.startswith("command.") and node_id not in nodes:
                nodes.append(node_id)
            execs = list(record.get("execs") or [])
            if exec_record.get("id") and exec_record["id"] not in execs:
                execs.append(exec_record["id"])
            updates = {"nodes": nodes, "execs": execs, "node_trailer": _node_trailer(exec_record)}
            if not record.get("purpose"):
                updates["purpose"] = _purpose(exec_record)
            record = store.update(root, record["id"], **updates)
    if checks and (record.get("checks") or {}).get("tree") != snap:
        try:
            result = gates.compare(root, worktree.tree_of(root, base), snap)
            record = store.update(root, record["id"], checks={**result, "tree": snap})
        except Exception:  # noqa: BLE001 - 检查跑不起来不挡这一轮的收尾
            _log.warning("项目 %s 的变更集检查没跑起来", project_id, exc_info=True)
    scope = (record.get("entries"), record.get("artifacts"), record.get("paths"))
    if previous_state != record["state"] or previous_scope != scope or checks:
        _notify(project_id, "changeset", record)
    return record


def open_changeset(manager, project_id: str, *, source: str = "assistant", build_mode: str = "L2",
                   purpose: str = "", conversation_id: str | None = None, run_id: str | None = None,
                   slice_id: str | None = None) -> dict:
    """先占一个变更集（P3 自主构建每个单元开始前调：source="autobuild"、build_mode="L4"、run_id、slice_id 记单元）。

    已有没关闭的变更集时返回它（同一项目同一时刻只有一个）；之后 `observe` 往里记改动。
    """
    root = manager.project_root(project_id)
    with manager._project_lock(project_id):
        found = store.current(root)
        if found is not None:
            return found
        copy = existing_copy(root)
        base = worktree.merge_base(root, copy) if copy is not None else worktree.rev(root)
        record = store.create(root, source=source, build_mode=build_mode, purpose=purpose, base=base,
                              conversation_id=conversation_id, run_id=run_id, slice_id=slice_id,
                              state="open")
    _notify(project_id, "changeset", record)
    return record


def _purpose(exec_record: dict | None) -> str:
    label = (exec_record or {}).get("command_label")
    return f"助手执行「{label}」的修改" if label else "助手的修改"


def current(manager, project_id: str) -> dict | None:
    return store.current(manager.project_root(project_id))


def get(manager, project_id: str, changeset_id: str) -> dict:
    record = store.get(manager.project_root(project_id), changeset_id)
    if record is None:
        raise WorkspaceError("变更集不存在", 404, "changeset_not_found")
    return record


def records(manager, project_id: str, *, states: list[str] | None = None,
            sources: list[str] | None = None) -> list[dict]:
    return [record for record in store.all_records(manager.project_root(project_id))
            if (not states or record.get("state") in states) and (not sources or record.get("source") in sources)]


# ---------------------------------------------------------------------------
# 接口投影（契约 7.6 Changeset、CS-02、CS-03、4.7 round_changes）
# ---------------------------------------------------------------------------


def _iso(value) -> str | None:
    from ...services.session_host.views import iso

    return iso(value)


def _label(root: Path, rev: str | None = None) -> str:
    from ...services import mainline

    return mainline.label(mainline.count(root, rev or "HEAD"))


def view(record: dict) -> dict:
    merged = record.get("merged")
    return {
        "id": record["id"],
        "source": record.get("source") or "assistant",
        "build_mode": record.get("build_mode"),
        "purpose": record.get("purpose") or "助手的修改",
        "state": record["state"],
        "base": record.get("base") or "",
        "conversation_id": record.get("conversation_id"),
        "run_id": record.get("run_id"),
        "slice_id": record.get("slice_id"),
        "created_at": _iso(record.get("created_at")),
        "updated_at": _iso(record.get("updated_at")),
        "merged": {"commit": merged["commit"], "version_label": merged["version_label"], "at": _iso(merged["at"])}
        if merged else None,
        "entity_count": int(record.get("entity_count") or 0),
    }


def _next_label(root: Path) -> str:
    from ...services import mainline

    return mainline.label(mainline.count(root) + 1)


def _item_checks(record: dict, path: str) -> str:
    findings = (record.get("checks") or {}).get("findings") or []
    levels = {item.get("level") for item in findings if item.get("path") == path}
    return "failed" if "error" in levels else "warning" if "warning" in levels else "passed"


def _content_pair(root: Path, copy: Path | None, record: dict, rel: str) -> tuple[bytes | None, bytes | None]:
    base = record.get("base") or ""
    tree = record.get("tree") or ""
    old = worktree.read_blob(root, base, rel) if base else None
    if record.get("state") == "merged" and record.get("merged"):
        commit = record["merged"]["commit"]
        return worktree.read_blob(root, f"{commit}^", rel), worktree.read_blob(root, commit, rel)
    new = worktree.read_blob(root, tree, rel) if tree else None
    return old, new


def _changes_of(manager, root: Path, record: dict) -> list[dict]:
    pairs = [(status, rel) for status, rel in record.get("changes") or []]
    return describe(manager, root, pairs)["items"]


def detail(manager, project_id: str, changeset_id: str) -> dict:
    """CS-02：变更集加分组明细、检查结论、确认后的版本号。"""
    root = manager.project_root(project_id)
    record = get(manager, project_id, changeset_id)
    model = _model(root)
    copy = existing_copy(root)
    groups: dict[str, list] = {}
    for item in _changes_of(manager, root, record):
        old, new = _content_pair(root, copy, record, item["path"])
        if item["group"] in model.ENTITY_KINDS:
            info = entity_diff.changes(model, item["group"], old, new)
        else:
            info = entity_diff.file_changes(old, new)
        groups.setdefault(item["group"], []).append({
            "target": item["target"], "change": item["change"], "detail": info["summary"],
            "checks": _item_checks(record, item["path"]),
        })
    checks = record.get("checks") or {}
    return {
        **view(record),
        "groups": [{"kind": kind, "label": GROUP_LABELS[kind], "items": groups[kind]}
                   for kind in GROUP_ORDER if kind in groups],
        "checks": {"errors": int(checks.get("errors") or 0), "warnings": int(checks.get("warnings") or 0),
                   "findings": list(checks.get("findings") or [])},
        "next_version_label": record["merged"]["version_label"] if record.get("merged") else _next_label(root),
    }


def _target_matches(item: dict, target: str) -> bool:
    ref = item["target"]
    if "id" in ref:
        return ref["id"] == target
    return f"{ref.get('type')}/{ref.get('key')}" == target or ref.get("key") == target


def entity_diff_view(manager, project_id: str, changeset_id: str, target: str = "") -> dict:
    """CS-03：逐个实体的差异（章节 → DiffLine）。"""
    root = manager.project_root(project_id)
    record = get(manager, project_id, changeset_id)
    model = _model(root)
    copy = existing_copy(root)
    items = []
    for item in _changes_of(manager, root, record):
        if target and not _target_matches(item, target):
            continue
        old, new = _content_pair(root, copy, record, item["path"])
        if item["group"] in model.ENTITY_KINDS:
            info = entity_diff.changes(model, item["group"], old, new)
        else:
            info = entity_diff.file_changes(old, new)
        items.append({"target": item["target"], "change": item["change"],
                      "sections": [{"title": section["title"], "lines": section["lines"]} for section in info["sections"]]})
    return {"items": items}


def round_rows(items: list[dict]) -> tuple[dict[str, list], int]:
    """describe 的 items → 卡片上按组的行、改动总数。动词由前端的「新增 / 修改 / 删除」标签表示：条目写类型和链接
    （契约示例「对象 [[门店]]」），其余写业务名；认不出的文件合成最后一行「另有 N 处改动」（change 为空，不出文件名）。"""
    groups: dict[str, list] = {}
    more = 0
    for item in items:
        ref = item["target"]
        if not ref.get("label"):
            more += 1
            continue
        label = GROUP_LABELS.get(item["group"]) if ref.get("kind") and "type" not in ref else None
        groups.setdefault(item["group"], []).append({
            "change": item["change"], "target": ref, "text": f"{label} [[{ref['name']}]]" if label else ref["label"],
            "comparable": item["change"] == "mod",
        })
    if more:
        groups.setdefault("other", []).append({"change": None, "target": None, "text": workspace_labels.more_text(more),
                                               "comparable": False, "count": more})
    return groups, len(items)


def round_card(manager, project_id: str, record: dict, card_id: str) -> dict:
    """「这一轮改了什么」卡（契约 4.7 round_changes，F3）：这一轮改了什么、存成的版本、检查提示；按钮只有撤回这一轮
    与查看改动（前端）。检查只提示：待建的链接单独说，其余按「几处什么问题」说，不出「检查有 N 个新错误」。"""
    root = manager.project_root(project_id)
    groups, total = round_rows(_changes_of(manager, root, record))
    checks = record.get("checks") or {}
    state = record["state"]
    reverted = record.get("reverted") or {}
    card_state = ("reverted" if reverted else state if state in ("merged", "discarded", "conflict") else "pending")
    merged = record.get("merged") or {}
    labels = {"merged": merged.get("version_label") or "", "reverted": "已撤回", "discarded": "已撤回",
              "conflict": "有冲突", "pending": "未生效"}
    keys = {"merged": "confirmed", "reverted": "skipped", "discarded": "skipped", "conflict": "conflict",
            "pending": "pending"}
    # 说明槽位不下发（UI 文案口径 ⑤）；增量更新那一轮不带副标题
    return {
        "card_id": card_id,
        "card_type": "round_changes",
        "eyebrow": "这一轮改了什么",
        "title": f"改了 {total} 处",
        "subtitle": "" if record.get("change_list_id") else record.get("purpose") or "",
        "status": {"key": keys.get(card_state, "pending"), "label": labels.get(card_state, "未生效")},
        "plugin": None,
        "open": None,
        "changeset_id": record["id"],
        "change_list_id": record.get("change_list_id"),
        # pending（指向按段规划、还没建的内容的链接数）随分段去掉，恒为 0
        "checks": {"errors": int(checks.get("errors") or 0), "warnings": int(checks.get("warnings") or 0),
                   "pending": 0},
        "hints": ([{"tone": "warn", "text": "助手没提交完，平台代存了这一轮，检查没过的在待处理里"}]
                  if record.get("collected") and int(checks.get("errors") or 0) else []) + check_hints(checks),
        "groups": [{"kind": kind, "label": GROUP_LABELS[kind], "items": groups[kind]}
                   for kind in GROUP_ORDER if kind in groups],
        "state": card_state,
        "merged_version_label": merged.get("version_label"),
        "reverted_version_label": reverted.get("version_label"),
        "inbox_item_id": f"changeset_conflict:{record['id']}" if card_state == "conflict" else None,
        "from": record.get("base"),
        "to": record.get("head"),
    }


def check_hints(checks: dict) -> list[dict]:
    """检查结果 → 卡片上的提示 [{tone: warn, text}]：找不到对象的链接、其余按类别数错误。"""
    hints = []
    findings = checks.get("findings") or []
    links = [item for item in findings if item.get("code") == gates.LINK_CATEGORY]
    if links:
        hints.append({"tone": "warn", "text": f"{len(links)} 处链接找不到对象"})
    counts: dict[str, int] = {}
    for item in findings:
        if item.get("level") == "error" and item.get("code") != gates.LINK_CATEGORY:
            label = str(item.get("label") or item.get("code") or "检查")
            counts[label] = counts.get(label, 0) + 1
    hints += [{"tone": "warn", "text": f"{count} 处{label}"} for label, count in counts.items()]
    return hints


def card_records(manager, project_id: str, record: dict, *, new_card: bool = False) -> list[tuple[str, dict]]:
    """要追加到对话里的卡片记录 [(对话, 记录)]：一个变更集（一轮）只有一张卡——还没有卡且 new_card 时放一张新卡，
    已有的卡原地更新（card.updated）。卡片编号记进变更集。"""
    conversation_id = record.get("conversation_id")
    if not conversation_id:
        return []
    root = manager.project_root(project_id)
    cards = list(record.get("cards") or [])
    if new_card and not cards:
        card_id = f"rc-{record['id']}-1"
        record = store.update(root, record["id"], cards=[card_id]) or record
        return [(conversation_id, {"type": "card", "payload": round_card(manager, project_id, record, card_id)})]
    return [(conversation_id, {"type": "card_updated", "payload": round_card(manager, project_id, record, card_id)})
            for card_id in cards]


def notice_record(record: dict, kind: str, text: str) -> tuple[str, dict] | None:
    conversation_id = record.get("conversation_id")
    if not conversation_id:
        return None
    version = (record.get("reverted") if kind == "discarded" else None) or record.get("merged") or {}
    return conversation_id, {"type": "notice", "payload": {
        "kind": kind, "text": text, "version_label": version.get("version_label"), "changeset_id": record["id"],
        "align": "center"}}


# ---------------------------------------------------------------------------
# 合入、撤回、解决冲突
# ---------------------------------------------------------------------------


def _require_open(manager, project_id: str, changeset_id: str, *, allow_conflict: bool = True,
                  check_running: bool = True) -> dict:
    record = get(manager, project_id, changeset_id)
    if record["state"] in ("merged", "discarded"):
        raise WorkspaceError("这一轮的修改已经生效或撤回", 409, "changeset_closed", {"state": record["state"]})
    if record["state"] == "conflict" and not allow_conflict:
        raise WorkspaceError("这一轮和你的修改有冲突，先选好保留哪一边", 409, "merge_conflict",
                             {"entities": (record.get("conflict") or {}).get("entities") or [],
                              "inbox_item_id": f"changeset_conflict:{record['id']}"})
    state = activity(project_id) if check_running else {}
    if state.get("running") and record.get("source", "assistant") == "assistant":
        raise WorkspaceError("助手还在跑，先打断或等它停下", 409, "changeset_in_progress",
                             {"conversation_id": state.get("conversation_id") or record.get("conversation_id")})
    return record


def _merge_tree(root: Path, main: str, head: str) -> tuple[str, list[str]]:
    """三方合并出的树与有文本冲突的路径。"""
    result = _run_git(root, ("-c", "core.quotePath=false", "merge-tree", "--write-tree", "--name-only",
                             "--no-messages", main, head))
    lines = result.stdout.splitlines()
    if result.returncode not in (0, 1) or not lines:
        raise WorkspaceError(f"合成变更集失败：{result.stderr.strip()}", 500)
    return lines[0].strip(), [line.strip() for line in lines[1:] if line.strip()]


def _tree_with(root: Path, tree: str, picks: dict[str, str]) -> str:
    """在 tree 上把 picks 里的路径换成指定版本（{路径: 提交}，提交里没有这个文件就删掉）。"""
    if not picks:
        return tree
    with tempfile.TemporaryDirectory(prefix="onto-merge-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(root, "read-tree", tree, env=env)
        for rel, commit in picks.items():
            line = _run_git(root, ("--literal-pathspecs", "ls-tree", commit, "--", rel)).stdout.strip()
            if line:
                meta, _, _name = line.partition("\t")
                mode, _kind, blob = meta.split()
                _git(root, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=env)
            else:
                _git(root, "--literal-pathspecs", "update-index", "--force-remove", "--", rel, env=env)
        return _git(root, "write-tree", env=env).strip()


def _conflict_check(manager, root: Path, base: str, main: str, head: str,
                    textual: list[str]) -> tuple[list[str], list[dict]]:
    """主线与变更集都改到的实体（两边改成不一样的）加上有文本冲突的其他文件；返回 (路径, 目标)。"""
    model = _model(root)
    main_paths = {rel for _status, rel in worktree.diff(root, base, main)}
    cs_paths = {rel for _status, rel in worktree.diff(root, base, head)}
    both = main_paths & cs_paths
    entity_inputs = {}
    for rel in both:
        entry = model.entry_key(rel)
        if entry:
            kind = entry.partition(":")[0]
            entity_inputs[rel] = (kind, worktree.read_blob(root, base, rel), worktree.read_blob(root, main, rel),
                                  worktree.read_blob(root, head, rel))
    conflicted = set(entity_diff.conflicts(model, entity_inputs))
    # git 报了文本冲突的路径一律算冲突（审查第 4 条）：实体口径看不出的改动（空行、标题）照样会带出冲突标记
    conflicted |= set(textual)
    targets = []
    for rel in sorted(conflicted):
        entry = model.entry_key(rel)
        if entry:
            kind, _, key = entry.partition(":")
            targets.append(_entry_ref(kind, key))
        else:
            targets.append(_file_ref(rel))
    return sorted(conflicted), targets


def merge(manager, project_id: str, changeset_id: str, *, actor: str, expected_head: str | None = None,
          choices: dict[str, str] | None = None, source: str | None = None, needs_review: bool = False,
          message: str | None = None, auto: bool = False) -> dict:
    """合入：一轮结束时的自动合入（`settle`，auto=True：会话宿主在这一轮收尾时调，不再查助手在不在跑）、
    选好冲突后重试（CS-07）、自主构建每一步自动合入（source="autobuild"、needs_review=True）。

    choices：合入冲突时用户选的 {路径或条目 ID: "mainline"|"changeset"}（CS-07）。
    检查（规则检查、插件声明的提交门禁）只记进变更集的 checks，不挡合入（F3）。
    全程持项目锁（审查第 5 条）：拿到锁后重读变更集确认仍没关闭，合成、门禁、提交、重置副本、记 merged 都在
    这把锁里，和撤回、另一次确认、一轮开始（`copy_for_turn`）互斥。
    合成结果和主线一样时不写提交，`commit` 为 None（审查第 3、10 条）。
    返回 {state, commit, version_label, merged_at, record}；错误按契约 2.4 抛 WorkspaceError。
    """
    root = manager.project_root(project_id)
    with manager._project_lock(project_id):
        record = _require_open(manager, project_id, changeset_id, check_running=not auto)
        copy = existing_copy(root)
        if copy is None:
            raise WorkspaceError("助手副本不在了，这一轮的修改没法生效", 409, "changeset_closed", {"state": record["state"]})
        main = worktree.rev(root)
        if expected_head and expected_head != main:
            raise WorkspaceError("有新的版本，请刷新后重试", 409, "head_moved", {"head": main})
        # 助手没提交完（打断、出错、提交被检查拦下后没修完）时平台代存：这些改动没经过提交前的检查，
        # 合入检查照样只提示，问题记进改动卡与待处理（F8）
        collected = bool(worktree.commit_pending(copy, "chore: 收拢这一轮没提交的改动"))
        head = worktree.rev(copy)
        merged_tree, result = _merged_tree(manager, project_id, root, record, main, head, choices,
                                           forbid_deletes=source == "autobuild")
        commit, hinted = _write_merge(manager, project_id, root, record, main, merged_tree, actor=actor,
                                      source=source, message=message)
        if hinted:
            result = {**result, "errors": int(result.get("errors") or 0)
                      + sum(1 for item in hinted if item["level"] == "error"),
                      "findings": [*hinted, *(result.get("findings") or [])][:200]}
        version_label = _label(root, commit or main)
        worktree.collect_state(root, copy)
        worktree.reset(root, copy, worktree.rev(root))
        now = time.time()
        record = store.update(root, record["id"], state="merged", closed_at=now, conflict=None,
                              merged={"commit": commit, "version_label": version_label, "at": now,
                                      **({} if commit else {"noop": True})},
                              needs_review=needs_review, checks={**result, "tree": record.get("tree")},
                              collected=collected) or record
    _notify(project_id, "changeset", record)
    _audit(actor, project_id, "changeset.merge", record)
    return {"state": "merged", "commit": commit, "version_label": version_label, "merged_at": _iso(now),
            "record": record}


def _merged_tree(manager, project_id: str, root: Path, record: dict, main: str, head: str,
                 choices: dict[str, str] | None, *, forbid_deletes: bool = False) -> tuple[str, dict]:
    """合成树：三方合并 → 按实体与文本检测冲突（选了哪一边就取哪一边）→ 受保护路径一律取主线 → 门禁。"""
    base = _git(root, "merge-base", main, head).strip() or main
    merged_tree, textual = _merge_tree(root, main, head)
    conflicted, targets = _conflict_check(manager, root, base, main, head, textual)
    picks: dict[str, str] = {}
    if conflicted:
        chosen = _normalize_choices(conflicted, targets, choices or {})
        missing = [target for rel, target in zip(conflicted, targets) if rel not in chosen]
        if missing:
            store.update(root, record["id"], state="conflict",
                         conflict={"paths": conflicted, "entities": targets, "main": main, "head": head,
                                   "at": time.time()})
            _notify(project_id, "changeset", store.get(root, record["id"]))
            raise WorkspaceError("这一轮和你的修改有冲突，已放进待处理，选好保留哪一边", 409, "merge_conflict",
                                 {"entities": targets, "inbox_item_id": f"changeset_conflict:{record['id']}"})
        picks = {rel: (main if keep == "mainline" else head) for rel, keep in chosen.items()}
    # 资料区、锁文件不采用副本的版本（审查第 1 条）：副本里关了稀疏规则时 git 会把资料当成删了
    for _status, rel in worktree.diff(root, main, _tree_with(root, merged_tree, picks)):
        if worktree.is_protected(rel):
            picks[rel] = main
    merged_tree = _tree_with(root, merged_tree, picks)
    if forbid_deletes:
        # 自主构建硬性不做「删除已确认的内容」（详设 1.6 #2、8.5）：主线上已有的本体文件被删、被改名，这一步不合入
        removed = [rel for status, rel in worktree.diff(root, main, merged_tree)
                   if status == "D" and rel.startswith("ontology/") and not _derived(rel)]
        if removed:
            raise WorkspaceError("自主构建不能删除已确认的内容", 422, "delete_forbidden", {"paths": removed[:50]})
    try:
        result = gates.compare(root, worktree.tree_of(root, main), merged_tree)
    except Exception:  # noqa: BLE001 - 检查跑不起来不挡合入（只提示），记一笔
        _log.warning("项目 %s 合入 %s 时检查没跑起来", project_id, record["id"], exc_info=True)
        result = {"errors": 0, "warnings": 0, "findings": [], "unavailable": True}
    return merged_tree, result


def _write_merge(manager, project_id: str, root: Path, record: dict, main: str, merged_tree: str, *,
                 actor: str, source: str | None, message: str | None) -> tuple[str | None, list[dict]]:
    """把合成树写成主线上的一个 squash 提交（经写入事务，trailer 在这个提交上）；和主线一样时不写，提交号为 None。
    返回 (提交, 插件声明的提交门禁的问题)：门禁只提示（F3），问题按 Finding 记进变更集。

    写入意图标成整段回滚（审查第 6 条）：写到一半崩溃，启动恢复把这些路径回到主线，不补提交半段。
    """
    from ..writepath import ChangeTx, Origin

    changes = worktree.diff(root, main, merged_tree)
    if not changes:
        return None, []
    owner = str((manager.get_project(project_id) or {}).get("owner") or "") or actor
    origin_source = source or ("autobuild" if record.get("source") == "autobuild" else "agent")
    origin = Origin(origin_source, owner, changeset=record["id"], node=record.get("node_trailer"))
    with ChangeTx(manager, project_id, origin, summary=f"合入 {record['id']}", expected_head=main,
                  rollback_on_crash=True) as tx:
        for status, rel in changes:
            if status == "D":
                tx.remove(rel)
                continue
            data = worktree.read_blob(root, merged_tree, rel)
            if data is not None:
                tx.write(rel, data)
        committed = tx.commit(message or f"assistant: {record.get('purpose') or '助手的修改'}（{record['id']}）")
    if committed.commit:
        from ...governance import telemetry

        telemetry.count("onto_merges_total", source=origin_source)  # 运维可观测「合入」卡（O-02）
    hinted = [{"code": str(item.get("gate") or item.get("code") or "gate"), "level": item.get("level") or "error",
               "label": str(item.get("gate_label") or item.get("label") or item.get("gate") or "插件检查"),
               "message": str(item.get("message") or "")[:500], "path": str(item.get("path") or "")}
              for item in committed.findings]
    return committed.commit or None, hinted


def _normalize_choices(paths: list[str], targets: list[dict], choices: dict[str, str]) -> dict[str, str]:
    by_target = {target["id"]: rel for rel, target in zip(paths, targets)}
    chosen = {}
    for key, keep in choices.items():
        if keep not in ("mainline", "changeset"):
            raise WorkspaceError("keep 只能是 mainline 或 changeset", 400, "invalid_argument",
                                 {"fields": {"choices": "keep"}})
        rel = by_target.get(key) or (key if key in paths else None)
        if rel:
            chosen[rel] = keep
    return chosen


def resolve(manager, project_id: str, changeset_id: str, choices: list[dict], *, actor: str) -> dict:
    """CS-07：选好保留哪一边后重试合入。"""
    record = get(manager, project_id, changeset_id)
    if record["state"] != "conflict":
        raise WorkspaceError("这一轮的修改没有待解决的冲突", 409, "changeset_closed", {"state": record["state"]})
    mapping = {}
    for item in choices or []:
        if not isinstance(item, dict) or not item.get("target"):
            raise WorkspaceError("choices 每项要有 target 与 keep", 400, "invalid_argument", {"fields": {"choices": "item"}})
        mapping[str(item["target"])] = str(item.get("keep") or "")
    return merge(manager, project_id, changeset_id, actor=actor, choices=mapping)


# 一轮收尾、启动时合入之后由宿主登记的通告方把卡片更新进对话：announcer(项目, 变更集记录)
_announcer: Callable[[str, dict], None] | None = None


def register_announcer(announcer: Callable[[str, dict], None] | None) -> None:
    global _announcer
    _announcer = announcer


def settle(manager, project_id: str, *, actor: str | None = None, announce: bool = False,
           turn_over: bool = False) -> dict | None:
    """把没关闭的变更集合入成一个版本（F3：一轮结束、出错、被打断都合入，下一轮开始前、启动时补合遗留的）。

    只管助手的变更集，和自主构建已经结束的运行留下的那一步（按它合入的口径：需复核、不许删已确认的内容）；运行没结束
    的归运行器。冲突时记 conflict（进待处理）、不抛。返回 {state, record, version_label}；没有要合入的返回 None。
    announce 时合入后交给登记的通告方更新对话里的卡片（启动时用；会话宿主自己出卡）。
    turn_over：调用方知道这一轮已经结束（会话宿主收尾、下一轮开始前）；否则助手还在跑时不动（Studio 复核、执行清单
    这类入口也会经 `copy_for_turn` 走到这里，不能把跑到一半的修改合进去）。
    """
    root = manager.project_root(project_id)
    record = store.current(root)
    if record is None:
        return None
    if not turn_over and record.get("source", "assistant") == "assistant" and activity(project_id).get("running"):
        return None
    if record["state"] == "conflict":
        return {"state": "conflict", "record": record, "version_label": None}
    autobuild = record.get("source") == "autobuild"
    if autobuild and _run_active(project_id, record.get("run_id")):
        return None
    if not record.get("tree"):
        # 先占着、还没改出东西（自主构建的一步）：关掉，版本不变
        if autobuild:
            store.update(root, record["id"], state="discarded", closed_at=time.time(), empty=True)
        return None
    owner = actor or str((manager.get_project(project_id) or {}).get("owner") or "") or "system"
    try:
        result = merge(manager, project_id, record["id"], actor=owner, auto=True,
                       source="autobuild" if autobuild else None, needs_review=autobuild)
    except WorkspaceError as exc:
        if exc.code == "merge_conflict":
            found = store.get(root, record["id"]) or record
            _announce(project_id, found, announce)
            return {"state": "conflict", "record": found, "version_label": None}
        if exc.code == "changeset_closed":
            return None
        raise
    _announce(project_id, result["record"], announce)
    return {"state": "merged", "record": result["record"], "version_label": result["version_label"]}


def _announce(project_id: str, record: dict, announce: bool) -> None:
    if not announce or _announcer is None:
        return
    try:
        _announcer(project_id, record)
    except Exception:  # noqa: BLE001 - 卡片是增强信息
        _log.warning("项目 %s 的变更集 %s 卡片没更新", project_id, record.get("id"), exc_info=True)


def revert_round(manager, project_id: str, changeset_id: str, *, actor: str) -> dict:
    """撤回这一轮（CS-05，F3）：合入过的写反向提交，回到这一轮之前的内容，另存一个版本；之后别处的修改保留，
    之后又改到这一轮的同一条时不硬撤（409 merge_conflict，说明是哪几条）。冲突中还没合入的照旧撤回副本。

    返回 {state: reverted|discarded, commit, version_label, record}。"""
    record = get(manager, project_id, changeset_id)
    if record["state"] in store.OPEN_STATES:
        out = discard(manager, project_id, changeset_id, actor=actor)
        return {**out, "commit": None, "version_label": None}
    if record["state"] != "merged" or not (record.get("merged") or {}).get("commit"):
        raise WorkspaceError("这一轮没有生效过的修改", 409, "changeset_closed", {"state": record["state"]})
    if record.get("reverted"):
        raise WorkspaceError("这一轮已经撤回过", 409, "changeset_closed", {"state": "reverted"})
    if activity(project_id).get("running"):
        raise WorkspaceError("助手还在跑，先打断或等它停下", 409, "changeset_in_progress",
                             {"conversation_id": activity(project_id).get("conversation_id")})
    root = manager.project_root(project_id)
    try:
        out = revert_merged(manager, project_id, changeset_id, actor=actor, source="revert")
    except WorkspaceError as exc:
        if exc.code != "merge_conflict":
            raise
        names = "、".join(str(item.get("name") or item.get("key")) for item in (exc.details or {}).get("entities") or [])
        raise WorkspaceError(f"「{names}」在这一轮之后又改过，撤回会连同后来的修改一起撤掉，这一轮没有撤回", 409,
                             "merge_conflict", exc.details) from exc
    try:
        from ...services.flow import change_lists

        change_lists.on_changeset_closed(root, record["id"], "discarded")
    except Exception:  # noqa: BLE001 - 清单状态是附加信息
        _log.warning("撤回 %s 后变更点清单没回到编辑中", record["id"], exc_info=True)
    record = store.get(root, changeset_id) or record
    _audit(actor, project_id, "changeset.revert", record)
    return {"state": "reverted", "commit": out["commit"], "version_label": out["version_label"], "record": record}


def discard(manager, project_id: str, changeset_id: str, *, actor: str) -> dict:
    """撤回还没合入的修改（合入冲突时、自主构建放弃一步）：副本分支与副本重置到主线，主线不受影响。"""
    root = manager.project_root(project_id)
    with manager._project_lock(project_id):
        record = _require_open(manager, project_id, changeset_id)  # 锁里重读：合入可能刚做完（审查第 5 条）
        copy = existing_copy(root)
        if copy is not None:
            worktree.reset(root, copy)
            # 这一轮的命令锁与写入范围一起清掉，守卫与节点状态不再按它算（审查第 9 条）
            worktree.clear_run_state(copy)
        record = store.update(root, record["id"], state="discarded", closed_at=time.time(), conflict=None) or record
    _notify(project_id, "changeset", record)
    _audit(actor, project_id, "changeset.discard", record)
    return {"state": "discarded", "record": record}


REVERT_PREFIX = "revert: 撤回 "


def revert_merged(manager, project_id: str, changeset_id: str, *, actor: str, source: str = "revert",
                  keep: tuple[str, ...] = ()) -> dict:
    """撤回一个已经合入的变更集（P3 自主构建撤回一步）：在主线上写它的反向提交。

    合入之后别的提交又改到了同一实体时不撤（`merge_conflict`，details 列出实体），免得把后来的修改一起撤掉。
    keep 里的路径不撤（自主构建撤回一步时的建模蓝图：它是资料分析的产物）。
    反向提交已经在主线上、记录还没标撤回（写完提交就出错）时补标，不再写一次。返回 {commit, version_label}。
    """
    from ..writepath import ChangeTx, Origin

    root = manager.project_root(project_id)
    record = get(manager, project_id, changeset_id)
    merged = record.get("merged") or {}
    commit = merged.get("commit")
    if record["state"] != "merged" or not commit:
        raise WorkspaceError("只有已生效的修改能撤回", 409, "changeset_closed", {"state": record["state"]})
    if record.get("reverted"):
        raise WorkspaceError("这个变更集已经撤回过", 409, "changeset_closed", {"state": "reverted"})
    done = reverse_commits(root, since=commit).get(record["id"])
    if done is not None:
        return mark_reverted(manager, project_id, record["id"], done)
    parent = f"{commit}^"
    paths = [(status, rel) for status, rel in worktree.diff(root, parent, commit) if not _derived(rel) and rel not in keep]
    with ChangeTx(manager, project_id, Origin(source, actor), summary=f"撤回 {record['id']}",
                  rollback_on_crash=True) as tx:
        later = {rel for _status, rel in worktree.diff(root, commit, tx.base_head)}
        model = _model(root)
        clashes = []
        for _status, rel in paths:
            # 合入之后这个文件又被改过（哪怕只是标题、空行）：反向提交会把后来的修改一起盖掉，不撤（审查第 17 条）
            if rel in later and worktree.read_blob(root, commit, rel) != worktree.read_blob(root, tx.base_head, rel):
                entry = model.entry_key(rel)
                clashes.append(_entry_ref(*entry.split(":", 1)) if entry else _file_ref(rel))
        if clashes:
            raise WorkspaceError("生效之后这些条目又被改过，不能单独撤回", 409, "merge_conflict",
                                 {"entities": clashes, "inbox_item_id": None})
        for _status, rel in paths:
            data = worktree.read_blob(root, parent, rel)
            if data is None:
                tx.remove(rel)
            else:
                tx.write(rel, data)
        # 派生文件（index.md、脚本描述符）不按旧版本恢复：写入事务按撤回后的条目重新生成
        result = tx.commit(f"{REVERT_PREFIX}{record['id']}（{record.get('purpose') or '助手的修改'}）")
    return mark_reverted(manager, project_id, record["id"], result.commit or tx.base_head)


def mark_reverted(manager, project_id: str, changeset_id: str, commit: str) -> dict:
    """变更集记为已撤回（反向提交 commit）。返回 {commit, version_label}。"""
    root = manager.project_root(project_id)
    label = _label(root, commit)
    store.update(root, changeset_id, reverted={"commit": commit, "version_label": label, "at": time.time()})
    _notify(project_id, "changeset", store.get(root, changeset_id))
    return {"commit": commit, "version_label": label}


def reverse_commits(root: Path, *, since: str | None = None) -> dict[str, str]:
    """主线上的反向提交 {变更集: 提交}：给了 since 取它之后的全部，否则取最近 50 个（--first-parent）。"""
    commits = _commits_since(root, since) if since else _recent_commits(root)
    found: dict[str, str] = {}
    for sha, body in commits:
        if body.startswith(REVERT_PREFIX):
            changeset_id = body[len(REVERT_PREFIX):].partition("（")[0].strip()
            found.setdefault(changeset_id, sha)
    return found


def _derived(rel: str) -> bool:
    """派生文件：目录的 index.md、脚本的描述符（由 .py 生成）。"""
    from ...services.workspace.layout import _is_script_descriptor

    return Path(rel).name.lower() == "index.md" or _is_script_descriptor(Path(rel))


def reconcile_merged(manager, root: Path) -> str | None:
    """启动时核对（审查第 6 条）：主线上已经有这个变更集的合入提交（`Onto-Changeset`），记录却还没关——崩在
    提交之后、记 merged 之前。补记 merged、把回执带回、副本重置到主线。返回补记的变更集编号。"""
    record = store.current(root)
    if record is None:
        return None
    commit = _commit_of(_recent_commits(root), record["id"])
    if commit is None:
        return None
    copy = existing_copy(root)
    if copy is not None:
        worktree.collect_state(root, copy)
        worktree.reset(root, copy, worktree.rev(root))
    now = time.time()
    # 自主构建的一步：合入时本该带的「需复核」一起补上，并照常通知（节点记成确认过并标需复核，审查 C）
    updated = store.update(root, record["id"], state="merged", closed_at=now, conflict=None,
                           needs_review=record.get("source") == "autobuild",
                           merged={"commit": commit, "version_label": _label(root, commit), "at": now})
    _log.warning("项目 %s 的变更集 %s 已合入主线但没记下，启动时补记", root.name, record["id"])
    if updated is not None:
        _notify(Path(root).name, "changeset", updated)
    return record["id"]


def reconcile_committing(root: Path) -> list[str]:
    """启动时核对先记成已关闭、再提交的变更集（插件变更集，带 `committing`）：主线上有它的提交就补记已合入，
    没有就是没提交成，留在已撤回。返回补记成已合入的编号。"""
    pending = [record for record in store.all_records(root) if record.get("committing")]
    if not pending:
        return []
    commits = _recent_commits(root)
    merged = []
    for record in pending:
        commit = _commit_of(commits, record["id"])
        if commit is None:
            store.update(root, record["id"], committing=False)
            continue
        now = time.time()
        store.update(root, record["id"], state="merged", committing=False, closed_at=now,
                     merged={"commit": commit, "version_label": _label(root, commit), "at": now})
        _log.warning("项目 %s 的变更集 %s 已提交但没记成已合入，启动时补记", root.name, record["id"])
        merged.append(record["id"])
    return merged


def _recent_commits(root: Path) -> list[tuple[str, str]]:
    """主线最近 50 个提交（--first-parent）：[(提交, 说明)]。"""
    out = _run_git(root, ("log", "--first-parent", "-50", "--format=%H%x1f%B%x1e")).stdout
    pairs = []
    for chunk in out.split("\x1e"):
        commit, _, body = chunk.strip("\n").partition("\x1f")
        if commit:
            pairs.append((commit, body))
    return pairs


def _commits_since(root: Path, commit: str) -> list[tuple[str, str]]:
    """主线上 commit 之后的提交（--first-parent）：[(提交, 说明)]。"""
    out = _run_git(root, ("log", "--first-parent", "--format=%H%x1f%B%x1e", f"{commit}..HEAD")).stdout
    pairs = []
    for chunk in out.split("\x1e"):
        sha, _, body = chunk.strip("\n").partition("\x1f")
        if sha:
            pairs.append((sha, body))
    return pairs


def _commit_of(commits: list[tuple[str, str]], changeset_id: str) -> str | None:
    """带这个变更集 trailer 的提交；按整行比，cs-1 不会认成 cs-12 的提交。"""
    line = f"Onto-Changeset: {changeset_id}"
    return next((commit for commit, body in commits if line in (item.strip() for item in body.splitlines())), None)


def startup_repair(manager) -> dict | None:
    """启动恢复步骤 changeset_copies：核对副本（worktree.repair_all）、补记已合入的变更集、清掉副本里上次留下的
    命令锁与写入范围（启动时没有命令在跑，审查第 9 条）。"""
    report = worktree.repair_all(manager)
    reconciled, settled, conflicts = [], [], []
    for root in manager._project_dirs():
        try:
            with manager._project_lock(root.name):
                if reconcile_merged(manager, root):
                    reconciled.append(root.name)
                reconcile_committing(root)
                copy = existing_copy(root)
                if copy is not None:
                    worktree.clear_run_state(copy)
            # 升级迁移与崩溃恢复（F3）：副本里已经提交、还没记下的改动先记进变更集，遗留的待确认变更集合入；
            # 有冲突的进待处理。自主构建没结束的运行由它启动后续跑时自己合入
            record = store.current(root)
            if record is not None and record.get("source") == "autobuild" \
                    and _run_active(root.name, record.get("run_id")):
                continue
            if copy is not None:
                observe(manager, root.name, running=False)
            outcome = settle(manager, root.name, announce=True)
            if outcome and outcome["state"] == "merged":
                settled.append(root.name)
            elif outcome and outcome["state"] == "conflict":
                conflicts.append(root.name)
        except Exception:  # noqa: BLE001 - 单个项目核对不了不挡启动
            _log.warning("项目 %s 的变更集核对失败", root.name, exc_info=True)
    found_any = bool(reconciled or settled or conflicts)
    if found_any and report is None:
        parts = [f"补记了 {len(reconciled)} 个已合入的变更集" if reconciled else "",
                 f"合入了 {len(settled)} 个项目遗留的修改" if settled else "",
                 f"{len(conflicts)} 个项目遗留的修改有冲突，已放进待处理" if conflicts else ""]
        report = {"id": "changeset_copies", "title": "助手副本", "result": "cleared", "tone": "fix", "failed": [],
                  "detail": "；".join(part for part in parts if part),
                  "projects": [{"project_id": name, "count": 1, "paths": []}
                               for name in dict.fromkeys(reconciled + settled + conflicts)]}
    return report


def _audit(actor: str, project_id: str, action: str, record: dict) -> None:
    try:
        from ...services import audit

        merged = record.get("merged") or {}
        audit.record(type="changeset", action=action, actor=actor or "system", project_id=project_id,
                     summary=f"{record['id']} {record.get('purpose') or ''}".strip(),
                     trailer={"Onto-Changeset": record["id"]} if merged else None,
                     detail={"changeset": record["id"], "state": record["state"], "commit": merged.get("commit")})
    except Exception:  # noqa: BLE001 - 审计失败不影响合入
        _log.warning("变更集审计记录失败", exc_info=True)


# ---------------------------------------------------------------------------
# 写入范围锁（详设 4.3；方案 C10、D11；契约 7.6 ScopeLock、CS-06、EP-03）
# ---------------------------------------------------------------------------


def empty_scope() -> dict:
    return {"state": "none", "changeset_id": None, "conversation_id": None, "run_id": None, "slice": None,
            "node": None, "entries": [], "artifacts": [], "paths": [], "reason_text": "", "since": None}


def node_patterns(root: Path, node_ids: list[str]) -> list[str]:
    """节点声明的输出与写入范围（produces、writes），按 fnmatch 用（`**` 当 `*`）。旧节点编号按 core 的别名认
    （拆开之前的建模 core.model 认成提取操作，它的范围盖住原来建模的）。"""
    from ...services.flow import model as flow_model

    try:
        flow = flow_model.load_for(root)
    except Exception:  # noqa: BLE001
        return []
    aliases = flow.get("aliases") or {}
    node_ids = [aliases.get(node_id, node_id) for node_id in node_ids]
    patterns = []
    for node in flow.get("nodes") or []:
        if node.get("id") not in node_ids:
            continue
        for key in ("produces", "writes"):
            for item in node.get(key) or []:
                pattern = str(item.get("path") if isinstance(item, dict) else item).replace("**", "*")
                if pattern and pattern not in patterns:
                    patterns.append(pattern)
    return patterns


def scope_lock(manager, project_id: str) -> dict:
    """当前的写入范围锁；没有助手变更集、助手也没在跑时是 state="none" 的空锁。

    锁节点声明的范围（produces、writes），再加上这一轮已经改过的、等你决定的决策卡预览列出的。`slice` 恒为 null
    （不再分段，契约只加不删）。"""
    try:
        root = manager.project_root(project_id)
    except Exception:  # noqa: BLE001 - 项目不在
        return empty_scope()
    record = store.current(root)
    state = activity(project_id)
    running = bool(state.get("running"))
    if record is None and not running:
        return empty_scope()
    exec_record = state.get("exec") or {}
    node = exec_record.get("node") or {}
    node_ids = list((record or {}).get("nodes") or [])
    if running and node.get("id") and node["id"] not in node_ids:
        node_ids.append(node["id"])
    entries = list((record or {}).get("entries") or [])
    artifacts = list((record or {}).get("artifacts") or [])
    for ref in state.get("affected") or []:
        bucket = entries if "id" in ref else artifacts
        if not any(existing.get("id", (existing.get("type"), existing.get("key"))) ==
                   ref.get("id", (ref.get("type"), ref.get("key"))) for existing in bucket):
            bucket.append(ref)
    paths = node_patterns(root, node_ids) + [_escape(rel) for rel in (record or {}).get("paths") or []]
    nothing_recorded = not entries and not artifacts and not paths
    if nothing_recorded and not running:
        return empty_scope()
    if running:
        label = node.get("title") or "这一步"
        reason = f"助手正在{('执行「' + label + '」') if node.get('title') else '改这个项目'}，这一轮结束后才能编辑"
        lock_state = "assistant_running"
    else:
        # 一轮结束就合入：只剩合入冲突（等你在待处理里选）和合入前的一瞬
        reason = ("这一轮和你的修改有冲突，在待处理里选好保留哪边后才能编辑"
                  if (record or {}).get("state") == "conflict" else "这一轮的修改正在保存")
        lock_state = "pending_confirm"
    if _autobuild_unmerged(record) and _run_active(project_id, record.get("run_id")):
        # 自主构建的一步（契约 2.4、EP-03）：锁到它自动合入为止
        lock_state = "autobuild_slice"
        reason = "自主构建正在做这一步"
    since = (record or {}).get("created_at") or exec_record.get("started_at")
    return {
        "state": lock_state,
        "changeset_id": (record or {}).get("id"),
        "conversation_id": (record or {}).get("conversation_id") or state.get("conversation_id"),
        "run_id": (record or {}).get("run_id") if lock_state == "autobuild_slice" else None,
        "slice": None,
        "node": {"id": node["id"], "version": node.get("version") or ""} if running and node.get("id") else None,
        "entries": entries,
        "artifacts": artifacts,
        "paths": list(dict.fromkeys(paths)),
        "reason_text": reason,
        "since": _iso(since) if since else None,
    }


def _autobuild_unmerged(record: dict | None) -> bool:
    """自主构建的一步，还没合入（开着或等确认）。"""
    return record is not None and record.get("source") == "autobuild" and record.get("state") in ("open", "pending_confirm")


def _run_active(project_id: str, run_id: str | None) -> bool:
    """自主构建的这次运行还没结束（运行结束后遗留的那一步由 `settle` 合入）。"""
    from ...services.autobuild import store as runs

    active = runs.active(project_id)
    return active is not None and active["run_id"] == run_id


def locked(lock: dict, *, entry: str | None = None, artifact: tuple[str, str] | None = None,
           path: str | None = None) -> bool:
    """某个条目、产物或路径在不在锁里。"""
    if not lock or lock.get("state") in (None, "none"):
        return False
    if entry and any(ref.get("id") == entry for ref in lock.get("entries") or []):
        return True
    if artifact and any((ref.get("type"), ref.get("key")) == artifact for ref in lock.get("artifacts") or []):
        return True
    if path and any(fnmatch.fnmatchcase(path, pattern) for pattern in lock.get("paths") or []):
        return True
    return False


def target_lock(manager, project_id: str, target: str) -> dict:
    """CS-06 `?target=`：只问它锁没锁；锁了给整份锁，没锁给空锁。"""
    lock = scope_lock(manager, project_id)
    if lock["state"] == "none":
        return lock
    root = manager.project_root(project_id)
    path = None
    entry = artifact = None
    if ":" in target and "/" not in target.split(":", 1)[0]:
        entry = target
        kind, _, key = target.partition(":")
        model = _model(root)
        found = next((etype for etype in model.ENTITY_TYPES if etype.kind == kind), None)
        if found is not None:
            path = f"{found.directory}/{key}{found.suffix}"
    elif "/" in target:
        art_type, _, key = target.partition("/")
        artifact = (art_type, key)
        atype = next((item for item in _artifact_types(manager, root) if item.type == art_type), None)
        if atype is not None:
            path = atype.path_template.replace("{key}", key.split("--")[0])
    return lock if locked(lock, entry=entry, artifact=artifact, path=path) else empty_scope()


def paths_in_scope(manager, project_id: str, rels: list[str], lock: dict | None = None) -> tuple[dict, list[str]]:
    """写入事务要写的路径里落在写入范围内的（ChangeTx 统一检查，命中 423）；lock 给了就不重算。"""
    lock = lock if lock is not None else scope_lock(manager, project_id)
    if lock["state"] == "none":
        return lock, []
    model = _model(manager.project_root(project_id))
    hits = []
    for rel in rels:
        entry = model.entry_key(rel)
        if locked(lock, entry=entry, path=rel):
            hits.append(rel)
    return lock, hits


# ---------------------------------------------------------------------------
# 删除项目
# ---------------------------------------------------------------------------


def purge(root: Path) -> None:
    """删除项目：变更集记录与副本目录一起清掉（工作区自己的 .git/worktrees 随工作区删除）。"""
    import shutil

    store.purge(root)
    shutil.rmtree(worktree.copy_path(root).parent, ignore_errors=True)
    _last_observed.pop(Path(root).name, None)
