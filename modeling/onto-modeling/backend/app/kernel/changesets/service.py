"""变更集服务（详设 8.2、4.3；方案 C7～C12；契约 3.10 CS-01～CS-07、EP-03、EP-04）。

陪伴建模下一个变更集从助手开始改起，到用户确认生效或撤回为止，中间可以跨好几轮：

    （副本里有了改动）──→ open（助手在跑）⇄ pending_confirm（这一轮结束）
    pending_confirm ──确认生效──→ merged        ──撤回这一轮──→ discarded
    pending_confirm ──合入时实体冲突──→ conflict ──选好保留哪一边──→ merged

- 观察（`observe`）：一轮结束（和跑的过程中节流地）看一眼副本，有改动就开或更新当前变更集；
- 合入（`merge`）：`merge-tree --write-tree` 叠到最新主线 → 按实体检测冲突 → 合成树上跑门禁（只看新增错误）
  → 经写入事务（ChangeTx）在主线写一个 squash 提交，trailer 写在这个合入提交上；
- 撤回（`discard`）：副本分支与副本重置到主线，主线不受影响；
- 撤回已合入的变更集（`revert_merged`，给 P3 自主构建撤回单片）：在主线上写反向提交；
- 写入范围锁（`scope_lock`）：助手在跑时节点声明的 `produces`，加副本里已改的实体与产物，加等你决定的
  决策卡改动预览里列出的条目，持续到确认生效或撤回。

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

from ...services.workspace.errors import WorkspaceError
from ...services.workspace.gitops import _git, _run_git
from . import entity_diff, gates, store, worktree

_log = logging.getLogger(__name__)

CHANGES_MAX = 500
GROUP_LABELS = {"object": "对象", "operation": "操作", "rule": "规则", "metric": "指标", "script": "脚本",
                "scenario": "场景", "artifact": "插件产物", "other": "其他文件"}
GROUP_ORDER = tuple(GROUP_LABELS)
OBSERVE_EVERY_SECONDS = 5.0
# 不是条目、也不是插件产物、但用户认得的文件：卡片上按业务名显示
FILE_TITLES = {"research/blueprint.json": "建模蓝图"}
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


def copy_for_turn(manager, project_id: str, root: Path) -> Path:
    """一轮开始前：建或修副本、让它跟上主线，返回助手的工作目录。

    建不了副本（不是 git 仓库、git 出错）时退回主线工作区并记日志：助手照旧能用，只是没有隔离。
    """
    root = Path(root)
    try:
        with manager._project_lock(project_id):
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
    return {"kind": kind, "id": f"{kind}:{key}", "key": key, "name": key}


def _artifact_types(manager, root: Path) -> list:
    from ..artifacts.registry import types_for_project

    try:
        return types_for_project(manager, root)
    except Exception:  # noqa: BLE001 - 产物登记读不出时只按条目与文件描述
        return []


def describe(manager, root: Path, changes: list[tuple[str, str]]) -> dict:
    """改动的路径 → {entries, artifacts, paths, items}；items 是 [{group, change, target, path}]。"""
    model = _model(root)
    types = _artifact_types(manager, root)
    entries, artifacts, items = [], [], []
    seen_entries, seen_artifacts = set(), set()
    for status, rel in changes[:CHANGES_MAX]:
        change = {"A": "add", "D": "del"}.get(status, "mod")
        entry = model.entry_key(rel)
        if entry:
            kind, _, key = entry.partition(":")
            ref = _entry_ref(kind, key)
            if entry not in seen_entries:
                seen_entries.add(entry)
                entries.append(ref)
            items.append({"group": kind, "change": change, "target": ref, "path": rel})
            continue
        atype = next((item for item in types if item.key_from_path(rel)), None)
        if atype is not None:
            key = atype.key_from_path(rel)
            ref = {"type": atype.type, "key": key, "title": key}
            if (atype.type, key) not in seen_artifacts:
                seen_artifacts.add((atype.type, key))
                artifacts.append(ref)
            group = "scenario" if atype.type.endswith(".scenario") else "artifact"
            items.append({"group": group, "change": change, "target": ref, "path": rel})
            continue
        items.append({"group": "other", "change": change, "path": rel,
                      "target": {"type": "file", "key": rel, "title": FILE_TITLES.get(rel) or rel.rsplit("/", 1)[-1]}})
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
        if not running and changes and _assign_to_slice(copy, exec_record, changes):
            # 这一片新建的条目记进蓝图的这一片（B4d）：蓝图也改了，重新取一次快照
            snap = worktree.snapshot(copy)
            changes = [(status, rel) for status, rel in worktree.diff(root, base, snap)
                       if not worktree.is_protected(rel)]
        record = store.current(root)
        if not changes:
            if record is not None and record["state"] in ("open", "pending_confirm") and record.get("tree"):
                # 助手把改动又改回去了：这个变更集是空的，悄悄关掉。先占着、还没改过的（自主构建的一片）不关
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
            slice_ref, step = exec_record.get("slice"), exec_record.get("step")
            if isinstance(slice_ref, dict) and slice_ref.get("id") and step:
                # 按切片的节点：记下这个变更集做了哪一片哪一步（确认生效时这一片这一步记为确认过）
                pairs = [list(pair) for pair in record.get("slice_steps") or []]
                if [slice_ref["id"], step] not in pairs:
                    pairs.append([slice_ref["id"], step])
                updates["slice_steps"] = pairs
                if not record.get("slice_id"):
                    updates["slice_id"] = slice_ref["id"]
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


def _assign_to_slice(copy: Path, exec_record: dict | None, changes: list[tuple[str, str]]) -> bool:
    """按切片建模时，这一轮新建、还不属于任何一片的对象、操作、规则记进这一片（副本里的蓝图）。"""
    slice_ref = (exec_record or {}).get("slice")
    if not isinstance(slice_ref, dict) or not slice_ref.get("id") or slice_ref["id"] == "all":
        return False
    from ...services.flow import blueprint

    model = _model(copy)
    entries = []
    for status, rel in changes:
        key = model.entry_key(rel) if status == "A" else None
        if key and key.split(":", 1)[0] in ("object", "operation", "rule"):
            kind, _, stem = key.partition(":")
            entries.append((kind, model.decode_filename(stem)))
    if not entries:
        return False
    module = blueprint.script(copy)
    try:
        doc = module.load(copy)
        if doc is None or module.mode(doc) != "sliced":
            return False
        doc, added = module.assign(doc, slice_ref["id"], entries)
        if added:
            module.save(copy, doc)
        return bool(added)
    except (OSError, module.BlueprintError):
        _log.warning("新条目记进切片 %s 失败", slice_ref.get("id"), exc_info=True)
        return False


def open_changeset(manager, project_id: str, *, source: str = "assistant", build_mode: str = "L2",
                   purpose: str = "", conversation_id: str | None = None, run_id: str | None = None,
                   slice_id: str | None = None) -> dict:
    """先占一个变更集（P3 自主构建每片开始前调：source="autobuild"、build_mode="L4"、run_id、slice_id）。

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


def round_card(manager, project_id: str, record: dict, card_id: str) -> dict:
    """「这一轮改了什么」卡（契约 4.7 round_changes）：变更集的全部改动、检查结论、确认后的版本号。"""
    root = manager.project_root(project_id)
    groups: dict[str, list] = {}
    for item in _changes_of(manager, root, record):
        ref = item["target"]
        name = ref.get("name") or ref.get("title") or ref.get("key")
        verb = {"add": "新增", "mod": "修改", "del": "删除"}[item["change"]]
        groups.setdefault(item["group"], []).append({
            "change": item["change"], "target": ref, "text": f"{verb}「{name}」", "comparable": item["change"] == "mod",
        })
    total = sum(len(items) for items in groups.values())
    checks = record.get("checks") or {}
    errors, warnings = int(checks.get("errors") or 0), int(checks.get("warnings") or 0)
    state = record["state"]
    card_state = "merged" if state == "merged" else "discarded" if state == "discarded" else "pending_confirm"
    title = f"改了 {total} 处，" + ("检查全部通过" if not errors and not warnings else
                                   f"检查有 {errors} 个新错误" if errors else f"检查有 {warnings} 个新警告")
    merged = record.get("merged") or {}
    # 说明槽位不下发（UI 文案口径 ⑤）：只读说明由前端按写入范围算；增量更新那一轮不带副标题
    return {
        "card_id": card_id,
        "card_type": "round_changes",
        "eyebrow": "这一轮改了什么",
        "title": title,
        "subtitle": "" if record.get("change_list_id") else record.get("purpose") or "",
        "status": {"key": {"merged": "confirmed", "discarded": "skipped"}.get(card_state, "pending"),
                   "label": {"merged": "已确认生效", "discarded": "已撤回"}.get(card_state, "待确认")},
        "plugin": None,
        "open": None,
        "changeset_id": record["id"],
        "change_list_id": record.get("change_list_id"),
        "checks": {"errors": errors, "warnings": warnings},
        "groups": [{"kind": kind, "label": GROUP_LABELS[kind], "items": groups[kind]}
                   for kind in GROUP_ORDER if kind in groups],
        "next_version_label": merged.get("version_label") or _next_label(root),
        "state": card_state,
        "merged_version_label": merged.get("version_label"),
        "remaining_failures": errors,
        "from": record.get("base"),
        "to": record.get("head"),
    }


def card_records(manager, project_id: str, record: dict, *, new_card: bool = False) -> list[tuple[str, dict]]:
    """要追加到对话里的卡片记录 [(对话, 记录)]：new_card 时这一轮放一张新卡，并把这个变更集之前的卡更新到
    最新内容（card.updated），按钮按同一个变更集作用。卡片编号记进变更集。"""
    conversation_id = record.get("conversation_id")
    if not conversation_id:
        return []
    root = manager.project_root(project_id)
    cards = list(record.get("cards") or [])
    out = []
    if new_card:
        card_id = f"rc-{record['id']}-{len(cards) + 1}"
        cards.append(card_id)
        record = store.update(root, record["id"], cards=cards) or record
        out.append((conversation_id, {"type": "card", "payload": round_card(manager, project_id, record, card_id)}))
        previous = cards[:-1]
    else:
        previous = cards
    for card_id in previous:
        out.append((conversation_id, {"type": "card_updated", "payload": round_card(manager, project_id, record, card_id)}))
    return out


def notice_record(record: dict, kind: str, text: str) -> tuple[str, dict] | None:
    conversation_id = record.get("conversation_id")
    if not conversation_id:
        return None
    merged = record.get("merged") or {}
    return conversation_id, {"type": "notice", "payload": {
        "kind": kind, "text": text, "version_label": merged.get("version_label"), "changeset_id": record["id"],
        "align": "center"}}


# ---------------------------------------------------------------------------
# 确认生效、撤回、解决冲突
# ---------------------------------------------------------------------------


def _require_open(manager, project_id: str, changeset_id: str, *, allow_conflict: bool = True) -> dict:
    record = get(manager, project_id, changeset_id)
    if record["state"] in ("merged", "discarded"):
        raise WorkspaceError("这一轮的修改已经确认生效或撤回", 409, "changeset_closed", {"state": record["state"]})
    if record["state"] == "conflict" and not allow_conflict:
        raise WorkspaceError("确认生效时有冲突，先选好保留哪一边", 409, "merge_conflict",
                             {"entities": (record.get("conflict") or {}).get("entities") or [],
                              "inbox_item_id": f"changeset_conflict:{record['id']}"})
    state = activity(project_id)
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
            targets.append({"kind": "file", "id": rel, "key": rel, "name": rel.rsplit("/", 1)[-1]})
    return sorted(conflicted), targets


def merge(manager, project_id: str, changeset_id: str, *, actor: str, expected_head: str | None = None,
          choices: dict[str, str] | None = None, source: str | None = None, needs_review: bool = False,
          message: str | None = None) -> dict:
    """确认生效（CS-04）；自主构建（P3）检查通过后也调它自动合入（source="autobuild"、needs_review=True）。

    choices：合入冲突时用户选的 {路径或条目 ID: "mainline"|"changeset"}（CS-07）。
    全程持项目锁（审查第 5 条）：拿到锁后重读变更集确认仍没关闭，合成、门禁、提交、重置副本、记 merged 都在
    这把锁里，和撤回、另一次确认、一轮开始（`copy_for_turn`）互斥。
    合成结果和主线一样时不写提交，`commit` 为 None（审查第 3、10 条）。
    返回 {state, commit, version_label, merged_at, record}；错误按契约 2.4 抛 WorkspaceError。
    """
    root = manager.project_root(project_id)
    with manager._project_lock(project_id):
        record = _require_open(manager, project_id, changeset_id)
        copy = existing_copy(root)
        if copy is None:
            raise WorkspaceError("助手副本不在了，这一轮的修改没法生效", 409, "changeset_closed", {"state": record["state"]})
        main = worktree.rev(root)
        if expected_head and expected_head != main:
            raise WorkspaceError("有新的版本，请刷新后重试", 409, "head_moved", {"head": main})
        worktree.commit_pending(copy, "chore: 收拢这一轮没提交的改动")
        head = worktree.rev(copy)
        merged_tree, result = _merged_tree(manager, project_id, root, record, main, head, choices)
        commit = _write_merge(manager, project_id, root, record, main, merged_tree, actor=actor, source=source,
                              message=message)
        version_label = _label(root, commit or main)
        worktree.collect_state(root, copy)
        worktree.reset(root, copy, worktree.rev(root))
        now = time.time()
        record = store.update(root, record["id"], state="merged", closed_at=now, conflict=None,
                              merged={"commit": commit, "version_label": version_label, "at": now,
                                      **({} if commit else {"noop": True})},
                              needs_review=needs_review, checks={**result, "tree": record.get("tree")}) or record
    _notify(project_id, "changeset", record)
    _audit(actor, project_id, "changeset.merge", record)
    return {"state": "merged", "commit": commit, "version_label": version_label, "merged_at": _iso(now),
            "record": record}


def _merged_tree(manager, project_id: str, root: Path, record: dict, main: str, head: str,
                 choices: dict[str, str] | None) -> tuple[str, dict]:
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
            raise WorkspaceError("确认生效时有冲突，已放进待处理，选好保留哪一边再确认", 409, "merge_conflict",
                                 {"entities": targets, "inbox_item_id": f"changeset_conflict:{record['id']}"})
        picks = {rel: (main if keep == "mainline" else head) for rel, keep in chosen.items()}
    # 资料区、锁文件不采用副本的版本（审查第 1 条）：副本里关了稀疏规则时 git 会把资料当成删了
    for _status, rel in worktree.diff(root, main, _tree_with(root, merged_tree, picks)):
        if worktree.is_protected(rel):
            picks[rel] = main
    merged_tree = _tree_with(root, merged_tree, picks)
    try:
        result = gates.compare(root, worktree.tree_of(root, main), merged_tree)
    except gates.GateUnavailable as exc:
        raise WorkspaceError(f"门禁跑不起来：{exc}", 503, "service_unavailable", {"dependency": "gate"}) from exc
    if result["errors"]:
        store.update(root, record["id"], checks={**result, "tree": record.get("tree")})
        raise WorkspaceError(f"这一轮有 {result['errors']} 个新的错误，先修好再确认", 422, "gate_failed",
                             {"findings": [item for item in result["findings"] if item["level"] == "error"]})
    return merged_tree, result


def _write_merge(manager, project_id: str, root: Path, record: dict, main: str, merged_tree: str, *,
                 actor: str, source: str | None, message: str | None) -> str | None:
    """把合成树写成主线上的一个 squash 提交（经写入事务，trailer 在这个提交上）；和主线一样时不写，返回 None。

    写入意图标成整片回滚（审查第 6 条）：写到一半崩溃，启动恢复把这些路径回到主线，不补提交半片。
    """
    from ..writepath import ChangeTx, Origin

    changes = worktree.diff(root, main, merged_tree)
    if not changes:
        return None
    owner = str((manager.get_project(project_id) or {}).get("owner") or "") or actor
    origin_source = source or ("autobuild" if record.get("source") == "autobuild" else "agent")
    origin = Origin(origin_source, owner, changeset=record["id"], node=record.get("node_trailer"))
    with ChangeTx(manager, project_id, origin, summary=f"确认生效 {record['id']}", expected_head=main,
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
    return committed.commit or None


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


def discard(manager, project_id: str, changeset_id: str, *, actor: str) -> dict:
    """撤回这一轮（CS-05）：副本分支与副本重置到主线，主线不受影响。"""
    root = manager.project_root(project_id)
    with manager._project_lock(project_id):
        record = _require_open(manager, project_id, changeset_id)  # 锁里重读：确认生效可能刚做完（审查第 5 条）
        copy = existing_copy(root)
        if copy is not None:
            worktree.reset(root, copy)
            # 这一轮的命令锁与写入范围一起清掉，守卫与节点状态不再按它算（审查第 9 条）
            worktree.clear_run_state(copy)
        record = store.update(root, record["id"], state="discarded", closed_at=time.time(), conflict=None) or record
    _notify(project_id, "changeset", record)
    _audit(actor, project_id, "changeset.discard", record)
    return {"state": "discarded", "record": record}


def revert_merged(manager, project_id: str, changeset_id: str, *, actor: str, source: str = "revert") -> dict:
    """撤回一个已经合入的变更集（P3 自主构建「按片撤回」）：在主线上写它的反向提交。

    合入之后别的提交又改到了同一实体时不撤（`merge_conflict`，details 列出实体），免得把后来的修改一起撤掉。
    返回 {commit, version_label}。
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
    parent = f"{commit}^"
    paths = [(status, rel) for status, rel in worktree.diff(root, parent, commit) if not _derived(rel)]
    with ChangeTx(manager, project_id, Origin(source, actor), summary=f"撤回 {record['id']}",
                  rollback_on_crash=True) as tx:
        later = {rel for _status, rel in worktree.diff(root, commit, tx.base_head)}
        model = _model(root)
        clashes = []
        for _status, rel in paths:
            # 合入之后这个文件又被改过（哪怕只是标题、空行）：反向提交会把后来的修改一起盖掉，不撤（审查第 17 条）
            if rel in later and worktree.read_blob(root, commit, rel) != worktree.read_blob(root, tx.base_head, rel):
                entry = model.entry_key(rel)
                kind, _, key = (entry or f"file:{rel}").partition(":")
                clashes.append(_entry_ref(kind, key))
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
        result = tx.commit(f"revert: 撤回 {record['id']}（{record.get('purpose') or '助手的修改'}）")
    new = result.commit or tx.base_head
    label = _label(root, new)
    store.update(root, record["id"], reverted={"commit": new, "version_label": label, "at": time.time()})
    _notify(project_id, "changeset", store.get(root, record["id"]))
    return {"commit": new, "version_label": label}


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
    store.update(root, record["id"], state="merged", closed_at=now, conflict=None,
                 merged={"commit": commit, "version_label": _label(root, commit), "at": now})
    _log.warning("项目 %s 的变更集 %s 已合入主线但没记下，启动时补记", root.name, record["id"])
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


def _commit_of(commits: list[tuple[str, str]], changeset_id: str) -> str | None:
    """带这个变更集 trailer 的提交；按整行比，cs-1 不会认成 cs-12 的提交。"""
    line = f"Onto-Changeset: {changeset_id}"
    return next((commit for commit, body in commits if line in (item.strip() for item in body.splitlines())), None)


def startup_repair(manager) -> dict | None:
    """启动恢复步骤 changeset_copies：核对副本（worktree.repair_all）、补记已合入的变更集、清掉副本里上次留下的
    命令锁与写入范围（启动时没有命令在跑，审查第 9 条）。"""
    report = worktree.repair_all(manager)
    reconciled = []
    for root in manager._project_dirs():
        try:
            with manager._project_lock(root.name):
                if reconcile_merged(manager, root):
                    reconciled.append(root.name)
                reconcile_committing(root)
                copy = existing_copy(root)
                if copy is not None:
                    worktree.clear_run_state(copy)
        except Exception:  # noqa: BLE001 - 单个项目核对不了不挡启动
            _log.warning("项目 %s 的变更集核对失败", root.name, exc_info=True)
    if reconciled and report is None:
        report = {"id": "changeset_copies", "title": "助手副本", "result": "cleared", "tone": "fix", "failed": [],
                  "detail": f"补记了 {len(reconciled)} 个已合入的变更集",
                  "projects": [{"project_id": name, "count": 1, "paths": []} for name in reconciled]}
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
    """节点声明的输出与写入范围（produces、writes），按 fnmatch 用（`**` 当 `*`）。"""
    from ...services.flow import model as flow_model

    try:
        flow = flow_model.load_for(root)
    except Exception:  # noqa: BLE001
        return []
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


def _slice_scope(manager, project_id: str, root: Path, node_ids: list[str], slice_ids: dict[str, set]) -> tuple:
    """按切片的节点落到这几片的条目上（详设 4.3）：返回 (仍按声明范围的节点, 片的路径模式, 片的条目, 蓝图)。

    全量模式（没有蓝图或只有一片）、或不知道在哪一片时，按节点声明的整类条目。"""
    from ...services.flow import blueprint, model as flow_model

    wanted = {node_id: ids - {"all"} for node_id, ids in slice_ids.items() if ids - {"all"}}
    if not wanted:
        return node_ids, [], [], None
    try:
        flow = flow_model.load_for(root)
        doc = blueprint.effective_doc(manager, project_id)
        module = blueprint.script(root)
    except Exception:  # noqa: BLE001 - 读不出蓝图按整类条目锁，宁宽勿漏
        return node_ids, [], [], None
    if module.mode(doc) != "sliced":
        return node_ids, [], [], None
    per_slice = {node["id"] for node in flow.get("nodes") or [] if node.get("per_slice")}
    model = _model(root)
    paths, entries, rest = [], [], []
    for node_id in node_ids:
        ids = wanted.get(node_id)
        if node_id not in per_slice or not ids:
            rest.append(node_id)
            continue
        for slice_id in sorted(ids):
            item = module.find_slice(doc, slice_id)
            if item is None:
                continue
            paths += [_escape(rel) for rel in module.member_paths(item)]
            # 这一片的场景（场景走查一个操作一份，片里的「场景」一步写它们）
            paths += [_escape(f"ontology/testcase/{model.encode_filename(name)}.md") for name in item.get("operations") or []]
            for kind, names in module.members(item).items():
                entries += [{**_entry_ref(kind, model.encode_filename(name)), "name": name} for name in names]
    return rest, paths, entries, doc


def scope_lock(manager, project_id: str) -> dict:
    """当前的写入范围锁；没有助手变更集、助手也没在跑时是 state="none" 的空锁。

    按切片的节点（建模）只锁这一片的条目，不再锁整类（详设 4.3「落到当前切片」）：助手在跑时取执行记录的切片，
    这一轮结束后取变更集里做过的那几片；再加上这一轮已经改过的、等你决定的决策卡预览列出的。"""
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
    slice_ids: dict[str, set] = {}
    for slice_id, _step in (record or {}).get("slice_steps") or []:
        for node_id in node_ids:
            slice_ids.setdefault(node_id, set()).add(slice_id)
    current_slice = exec_record.get("slice") if isinstance(exec_record.get("slice"), dict) else None
    if running and node.get("id") and current_slice and current_slice.get("id"):
        slice_ids.setdefault(node["id"], set()).add(current_slice["id"])
    whole, slice_paths, slice_entries, doc = _slice_scope(manager, project_id, root, node_ids, slice_ids)
    entries = list((record or {}).get("entries") or [])
    artifacts = list((record or {}).get("artifacts") or [])
    for ref in [*slice_entries, *(state.get("affected") or [])]:
        bucket = entries if "id" in ref else artifacts
        if not any(existing.get("id", (existing.get("type"), existing.get("key"))) ==
                   ref.get("id", (ref.get("type"), ref.get("key"))) for existing in bucket):
            bucket.append(ref)
    paths = node_patterns(root, whole) + slice_paths + [_escape(rel) for rel in (record or {}).get("paths") or []]
    if not entries and not artifacts and not paths and not running:
        return empty_scope()
    if current_slice is None and doc is not None and (record or {}).get("slice_id"):
        found = next((item for item in doc.get("slices") or [] if item.get("id") == record["slice_id"]), None)
        current_slice = {"id": found["id"], "name": found["name"]} if found else None
    sliced = doc is not None and current_slice is not None
    if running:
        label = node.get("title") or "这一步"
        reason = (f"助手正在改「{current_slice['name']}」这一片，确认生效或撤回之前这些内容只读" if sliced else
                  f"助手正在{('执行「' + label + '」') if node.get('title') else '改这个项目'}，确认生效或撤回之前这些内容只读")
        lock_state = "assistant_running"
    else:
        reason = "这一轮的修改还没确认，确认生效或撤回之后才能编辑"
        lock_state = "pending_confirm"
    since = (record or {}).get("created_at") or exec_record.get("started_at")
    return {
        "state": lock_state,
        "changeset_id": (record or {}).get("id"),
        "conversation_id": (record or {}).get("conversation_id") or state.get("conversation_id"),
        "run_id": None,
        "slice": current_slice if sliced else None,
        "node": {"id": node["id"], "version": node.get("version") or ""} if running and node.get("id") else None,
        "entries": entries,
        "artifacts": artifacts,
        "paths": list(dict.fromkeys(paths)),
        "reason_text": reason,
        "since": _iso(since) if since else None,
    }


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
