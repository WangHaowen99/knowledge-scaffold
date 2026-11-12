"""自主构建的复核（详设 8.5「结束」、4.7「自主构建复核」；契约 AB-10～AB-17）。

- 台账（AB-10）；接受（AB-11）、改判（AB-12：往运行对话发重做受影响部分的命令，运行还没结束就排到结束后）、
  一键接受高置信（AB-13）；
- 挂起的问题（AB-14）与回答（AB-15：陪伴模式下助手马上补上；也可以留到下一次自主构建）；
- 按段撤回（AB-17）：在主线上写这一段的反向提交，别的段不动；合入之后这一段的条目又被改过就不撤；
- 「需复核」标记：一段里的自动决定都复核过（没有决定的段，整次运行都复核完）就去掉这一段的标记，
  建模节点的标记在没有段还需复核时去掉；
- 复核被改掉的比例记进可观测（`onto_autobuild_reviews_total{outcome}`）。
"""

from __future__ import annotations

import time
from pathlib import Path

from ...api.errors import ApiError
from . import store, views

REVIEW_PAGE = {"page": "inbox", "label": "改判"}
ANSWER_PAGE = {"page": "inbox", "label": "回答挂起的问题"}


def _ws():
    from ...api import deps

    return deps.workspaces


def _run(project_id: str, run_id: str) -> dict:
    run = store.get(project_id, run_id)
    if run is None:
        raise ApiError("run_not_found", "运行不存在", 404)
    return run


def _threshold(run: dict) -> float:
    return float(((run.get("policy") or {}).get("advanced") or {}).get("confidence_threshold") or 0.8)


def ledger(project_id: str, run_id: str, *, review_state: str = "", below_threshold: bool | None = None) -> dict:
    """AB-10：撤回了的段上的决定不列。"""
    run = _run(project_id, run_id)
    entries = views.live_ledger(run)
    threshold = _threshold(run)

    def listed(entry: dict) -> bool:
        if review_state and entry.get("review_state") != review_state:
            return False
        return below_threshold is None or (float(entry.get("confidence") or 0) < threshold) == below_threshold

    items = [entry for entry in entries if listed(entry)]
    below = sum(1 for entry in entries if float(entry.get("confidence") or 0) < threshold)
    return {"items": [views.ledger_entry(entry) for entry in items],
            "counts": {"total": len(entries), "high_confidence": len(entries) - below, "below_threshold": below,
                       "assumptions": sum(1 for entry in entries if entry.get("assumption"))}}


def _entry(project_id: str, entry_id: str) -> tuple[dict, dict]:
    found = store.find_entry(project_id, entry_id)
    if found is None:
        raise ApiError("ledger_entry_not_found", "台账条目不存在", 404)
    return found


def _ensure_live(run: dict, slice_ref: dict | None) -> None:
    """撤回了的段上的决定与挂起问题不再接受、改判、回答（审查 G）：改判与补上会把撤掉的内容又做回来。"""
    slice_id = (slice_ref or {}).get("id")
    if slice_id and ((run.get("slices") or {}).get(slice_id) or {}).get("reverted"):
        raise ApiError("slice_reverted", "这一段已经撤回", 409, {"slice_id": slice_id})


def _count(outcome: str, project_id: str, count: int = 1) -> None:
    from ...governance import telemetry

    if count:
        telemetry.count("onto_autobuild_reviews_total", count, outcome=outcome, project=project_id)


def accept(project_id: str, entry_id: str) -> dict:
    """AB-11：接受这个决定；受影响产物的「需复核」随之去掉（这一段都复核过时）。"""
    run, found = _entry(project_id, entry_id)
    _ensure_live(run, found.get("slice"))

    def change(record: dict) -> None:
        for entry in record.get("ledger") or []:
            if entry["id"] == entry_id and entry.get("review_state") == "pending":
                entry["review_state"] = "accepted"
                entry["reviewed_at"] = time.time()

    run = store.update(project_id, run["run_id"], change)
    _count("accepted", project_id)
    clear_reviews(project_id, run)
    return views.ledger_entry(next(entry for entry in run["ledger"] if entry["id"] == entry_id))


def accept_high(project_id: str, run_id: str) -> dict:
    """AB-13：这次运行里还没复核、置信度达到阈值的一起接受。"""
    run = _run(project_id, run_id)
    threshold = _threshold(run)
    live = {entry["id"] for entry in views.live_ledger(run)}
    accepted = []

    def change(record: dict) -> None:
        for entry in record.get("ledger") or []:
            if (entry["id"] in live and entry.get("review_state") == "pending"
                    and float(entry.get("confidence") or 0) >= threshold):
                entry["review_state"] = "accepted"
                entry["reviewed_at"] = time.time()
                accepted.append(entry["id"])

    run = store.update(project_id, run_id, change)
    _count("accepted", project_id, len(accepted))
    clear_reviews(project_id, run)
    return {"accepted": len(accepted), "threshold": threshold}


def _option_label(item: dict, body: dict) -> str:
    custom = str(body.get("custom_text") or "").strip()
    if custom:
        return custom
    option = next((option for option in item.get("options") or [] if option.get("id") == body.get("option_id")), None)
    if option is None or option.get("custom"):
        raise ApiError("answer_invalid", "选项不存在，或要写上你的说法", 400)
    return option.get("label") or ""


def _unit(run: dict, unit_id: str | None) -> dict | None:
    return next((unit for unit in run.get("units") or [] if unit["id"] == unit_id), None)


def _command_for(run: dict, slice_ref: dict, step_id: str | None) -> str:
    """重做受影响部分用的命令：这一段这一步的命令（带分段）；不知道哪一步时用这一段的第一步。"""
    unit = _unit(run, (slice_ref or {}).get("id"))
    if unit is None:
        return "/ontology-extract-object"
    step = next((item for item in unit["steps"] if item["id"] == step_id), None) or unit["steps"][0]
    args = " ".join(f"{key}={value}" for key, value in step["args"].items())
    return f"{step['command']} {args}".strip()


async def send_command(project_id: str, run: dict, line: str, text: str, origin: dict, title: str,
                       *, task_id: str | None = None) -> dict:
    """往运行对话发一条命令（S-20 同样的任务对象）；运行结束之后才用。"""
    from .. import agent_sessions
    from ..session_host import command_tasks

    name = line.split()[0]
    conversation_id = run["conversation_id"]
    task = command_tasks.get(project_id, task_id) if task_id else None
    if task is None:
        task = command_tasks.create(project_id, origin=origin, title=title, conversation_id=conversation_id,
                                    command=name)
    prompt = f"{line}\n\n{text}"
    meta = {"display_text": prompt, "origin": {"kind": "inbox", "page": origin["page"], "label": origin["label"],
                                               "task_id": task["task_id"]},
            "task_id": task["task_id"], "command": name}
    try:
        await agent_sessions.manager.send(project_id, _ws().project_root(project_id), prompt, conversation_id,
                                          username=run.get("owner") or "", meta=meta)
    except Exception:
        command_tasks.update(project_id, task["task_id"], state="failed", result={"summary": "没能发到对话里", "refs": []})
        raise
    return command_tasks.view(command_tasks.update(project_id, task["task_id"], state="running") or task)


async def rejudge(project_id: str, entry_id: str, body: dict) -> dict:
    """AB-12：改判为选中的选项；运行还在跑时任务排队，运行结束后再发。"""
    from ..session_host import command_tasks

    run, entry = _entry(project_id, entry_id)
    _ensure_live(run, entry.get("slice"))
    label = _option_label(entry, body)

    def change(record: dict) -> None:
        for item in record.get("ledger") or []:
            if item["id"] == entry_id:
                item.update(review_state="rejudged", rejudged_to=label, reviewed_at=time.time())

    run = store.update(project_id, run["run_id"], change)
    _count("rejudged", project_id)
    entry = next(item for item in run["ledger"] if item["id"] == entry_id)
    title = f"改判：{entry.get('summary') or entry.get('question')}"
    active = store.active(project_id)
    if active is not None:
        # 这次（或之后又开的一次）运行还没结束：排到它结束之后再发
        task = command_tasks.create(project_id, origin=REVIEW_PAGE, title=title,
                                    conversation_id=run["conversation_id"], command=_command_for(run, entry["slice"],
                                                                                                  entry.get("step")).split()[0])
        store.update(project_id, active["run_id"], lambda record: record.setdefault("pending_rejudges", []).append(
            {"entry_id": entry_id, "task_id": task["task_id"], "run_id": run["run_id"]}))
        return {"entry": views.ledger_entry(entry), "command_task": command_tasks.view(task)}
    task = await send_rejudge(project_id, run["run_id"], entry_id)
    return {"entry": views.ledger_entry(entry), "command_task": task}


async def send_rejudge(project_id: str, run_id: str, entry_id: str, *, task_id: str | None = None) -> dict:
    run = _run(project_id, run_id)
    entry = next(item for item in run["ledger"] if item["id"] == entry_id)
    _ensure_live(run, entry.get("slice"))
    line = _command_for(run, entry["slice"], entry.get("step"))
    text = (f"改判：「{entry.get('question')}」原来按「{entry.get('chosen')}」做，改为「{entry.get('rejudged_to')}」。"
            "只重做受影响的部分。")
    return await send_command(project_id, run, line, text, REVIEW_PAGE,
                              f"改判：{entry.get('summary') or entry.get('question')}", task_id=task_id)


# ---------------------------------------------------------------------------
# 挂起的问题
# ---------------------------------------------------------------------------


def suspensions(project_id: str, run_id: str) -> dict:
    """AB-14：撤回了的段上的不列；上限 200。"""
    run = _run(project_id, run_id)
    return {"items": [views.suspension(item) for item in views.live_suspensions(run)][:200]}


async def answer(project_id: str, suspension_id: str, body: dict) -> dict:
    """AB-15：回答挂起的问题。陪伴模式下马上补上（往运行对话发这一段这一步的命令；有运行没结束时排到它结束后）；
    `defer_to_next_run` 留到下一次：带着的回答记下，下一次自主构建做到这一段时补做这一步、把回答注入给助手，
    做完才算回答过（审查 I）。留到下一次的还可以现在回答。"""
    from ..session_host import command_tasks

    found = store.find_suspension(project_id, suspension_id)
    if found is not None:
        _ensure_live(found[0], found[1].get("slice"))
    if found is None or found[1].get("state") not in ("open", "deferred"):
        raise ApiError("suspension_not_found", "挂起问题不存在或已回答", 404)
    run, item = found
    action = body.get("action")
    if action not in (None, "", "use_generic", "defer_to_next_run"):
        raise ApiError("invalid_argument", "action 取值不合法", 400, {"fields": {"action": "enum"}})
    chose = body.get("option_id") or str(body.get("custom_text") or "").strip()
    label = _option_label(item, body) if not action or (action == "defer_to_next_run" and chose) else ""
    state = "deferred" if action == "defer_to_next_run" else "answered"

    def change(record: dict) -> None:
        for entry in record.get("suspensions") or []:
            if entry["id"] == suspension_id:
                entry.update(state=state, answer=label or action, answered_at=time.time())

    run = store.update(project_id, run["run_id"], change)
    item = next(entry for entry in run["suspensions"] if entry["id"] == suspension_id)
    task = None
    if state == "answered":
        active = store.active(project_id)
        if active is not None:
            # 运行还没结束：补做排到它结束之后（同改判），这一段的写入范围这时归运行器
            queued = command_tasks.create(project_id, origin=ANSWER_PAGE, title=f"补上：{item.get('title')}",
                                          conversation_id=run["conversation_id"],
                                          command=_command_for(run, item["slice"], item.get("step")).split()[0])
            store.update(project_id, active["run_id"], lambda record: record.setdefault("pending_rejudges", []).append(
                {"suspension_id": suspension_id, "task_id": queued["task_id"], "run_id": run["run_id"]}))
            task = command_tasks.view(queued)
        else:
            task = await send_answer(project_id, run["run_id"], suspension_id)
    return {"suspension": views.suspension(item), "command_task": task}


async def send_answer(project_id: str, run_id: str, suspension_id: str, *, task_id: str | None = None) -> dict:
    """挂起的问题回答之后（陪伴模式）：往运行对话发这一段这一步的命令，把跳过的部分补上。"""
    run = _run(project_id, run_id)
    item = next(entry for entry in run["suspensions"] if entry["id"] == suspension_id)
    _ensure_live(run, item.get("slice"))
    line = _command_for(run, item["slice"], item.get("step"))
    reply = f"按通用做法处理「{item.get('title')}」。" if item.get("answer") == "use_generic" else \
        f"「{item.get('title')}」的回答：{item.get('answer')}。把之前跳过的部分补上。"
    return await send_command(project_id, run, line, reply, ANSWER_PAGE, f"补上：{item.get('title')}", task_id=task_id)


# ---------------------------------------------------------------------------
# 按段撤回
# ---------------------------------------------------------------------------


def revert_slice(project_id: str, run_id: str, slice_id: str, *, actor: str) -> dict:
    """AB-17：只动这一段——反向提交，恢复成运行开始前的样子，另存为新版本，其他分段不受影响。

    蓝图里这一段在这次合入时新记进去的条目一起去掉（审查 L），别的段合入时对蓝图的修改不动；这一段各步的确认记录
    恢复成运行开始前的（原来过期的仍是过期，审查 K）。反向提交已经写进主线、后面几步没做完时，再撤一次把它们补上，
    不再写一次（审查推断）。"""
    from ...kernel.changesets import service as changesets
    from ...kernel.changesets import store as changeset_store
    from ...services.workspace.errors import WorkspaceError
    from ..flow import blueprint

    run = _run(project_id, run_id)
    active = store.active(project_id)
    if active is not None:
        # 本次运行还在跑；或者又开了一次运行（它占着写入范围）：都等运行结束
        raise ApiError("autobuild_running", "自主构建运行中", 409, {"run_id": active["run_id"]})
    info = (run.get("slices") or {}).get(slice_id)
    unit = _unit(run, slice_id)
    if info is None or unit is None or unit["kind"] != "slice":
        raise ApiError("slice_not_revertible", "这一段不能撤回", 409, {"reason": "not_in_run"})
    if info.get("reverted"):
        raise ApiError("slice_not_revertible", "这一段已经撤回过", 409, {"reason": "already_reverted"})
    if not info.get("commit") or not info.get("changeset_id"):
        raise ApiError("slice_not_revertible", "这一段没有生效过", 409, {"reason": "not_merged"})
    ws = _ws()
    root = ws.project_root(project_id)
    done = (changeset_store.get(root, info["changeset_id"]) or {}).get("reverted")
    if done:
        result = {"commit": done.get("commit"), "version_label": done.get("version_label")}
    else:
        def members(parent: bytes | None, merged: bytes | None, current: bytes | None) -> bytes | None:
            return blueprint.drop_added_members(root, slice_id, parent, merged, current)

        try:
            result = changesets.revert_merged(ws, project_id, info["changeset_id"], actor=actor, source="revert",
                                              keep=(blueprint.BLUEPRINT_REL,),
                                              rewrite={blueprint.BLUEPRINT_REL: members})
        except WorkspaceError as exc:
            if exc.code == "merge_conflict":
                raise ApiError("slice_not_revertible", "后面的修改改到了这一段的条目，不能单独撤回", 409,
                               {"reason": "later_changes",
                                "entities": (exc.details or {}).get("entities") or []}) from exc
            if exc.code == "changeset_closed":
                raise ApiError("slice_not_revertible", "这一段不能撤回", 409, {"reason": "not_merged"}) from exc
            raise
    run = _after_revert(project_id, run_id, unit, result, actor)
    return {"commit": result["commit"], "version_label": result["version_label"],
            "reverted_to_version_label": run.get("base_version_label")}


def repair_reverts(project_id: str) -> int:
    """启动时（审查推断）：按段撤回的反向提交已经在主线上、记录没补完就停了机——按反向提交把变更集与运行记录补上。
    返回补上的段数。"""
    from ...kernel.changesets import service as changesets
    from ...kernel.changesets import store as changeset_store

    ws = _ws()
    root = ws.project_root(project_id)
    found = None
    fixed = 0
    for run in store.runs(project_id):
        if run.get("state") not in store.ENDED:
            continue
        for unit in run.get("units") or []:
            info = (run.get("slices") or {}).get(unit["id"]) or {}
            committed = info.get("commit") and info.get("changeset_id") and not info.get("reverted")
            if unit["kind"] != "slice" or not committed:
                continue
            done = (changeset_store.get(root, info["changeset_id"]) or {}).get("reverted")
            if not done:
                found = changesets.reverse_commits(root) if found is None else found
                if info["changeset_id"] not in found:
                    continue
                done = changesets.mark_reverted(ws, project_id, info["changeset_id"], found[info["changeset_id"]])
            _after_revert(project_id, run["run_id"], unit, done, "system")
            fixed += 1
    return fixed


def _after_revert(project_id: str, run_id: str, unit: dict, result: dict, actor: str) -> dict:
    """反向提交之后：这一段的确认记录恢复成运行开始前的、运行里记下撤回、去掉这一段的「需复核」。"""
    from ..flow import slices

    root = _ws().project_root(project_id)
    run = _run(project_id, run_id)
    info = run["slices"][unit["id"]]
    steps = [step["id"] for step in unit["steps"]] + ["check"]
    if "state_before" in info:
        slices.put_back(root, unit["id"], steps, info.get("state_before") or {})
    else:
        slices.forget(root, unit["id"], steps)  # 修复之前开始的运行没记运行前的样子

    def change(record: dict) -> None:
        record["slices"][unit["id"]]["reverted"] = {"commit": result["commit"],
                                                    "version_label": result["version_label"],
                                                    "at": time.time(), "by": actor}
        if record.get("report"):
            record["report"] = views.report(record)

    run = store.update(project_id, run_id, change)
    clear_reviews(project_id, run)
    return run


# ---------------------------------------------------------------------------
# 「需复核」标记
# ---------------------------------------------------------------------------


def clear_reviews(project_id: str, run: dict) -> list[str]:
    """一段里的自动决定都复核过（没有决定的段：整次运行都复核过）就去掉这一段的「需复核」；建模节点、主线上
    几步的标记在这次运行没有还需复核的段时去掉。返回去掉标记的段。"""
    from ..flow import model as flow_model
    from ..flow import state as node_state

    root = Path(_ws().project_root(project_id))
    entries = views.live_ledger(run)
    pending = {(entry.get("slice") or {}).get("id") for entry in entries if entry.get("review_state") == "pending"}
    decided = {(entry.get("slice") or {}).get("id") for entry in entries}
    cleared, still = [], []
    for unit in run.get("units") or []:
        info = (run.get("slices") or {}).get(unit["id"]) or {}
        if not info.get("commit") or info.get("reverted"):
            continue
        done = unit["id"] not in pending and (unit["id"] in decided or not pending)
        (cleared if done else still).append(unit)
    with node_state._lock:
        stored = node_state.load(root)
        changed = False
        for unit in cleared:
            records = (stored.get("slices") or {}).get(unit["id"]) or {}
            for record in records.values():
                review = record.get("needs_review") or {}
                if review.get("source") == "autobuild" and review.get("run_id") == run["run_id"]:
                    record.pop("needs_review")
                    changed = True
        if changed:
            node_state.save(root, stored)
    if not still:
        flow = flow_model.load_for(root)
        ids = {node["id"] for node in flow.get("nodes") or []}
        for item in node_state.reviews(root):
            if item.get("source") == "autobuild" and item.get("run_id") == run["run_id"] and item["node_id"] in ids:
                node_state.accept_review(root, item["node_id"])
    from .. import project_events

    project_events.schedule_flow_refresh(project_id)
    return [unit["id"] for unit in cleared]


def _flagged(run: dict) -> list[dict]:
    """这次运行里还标着「需复核」的段（与 clear_reviews 同一口径）。"""
    entries = views.live_ledger(run)
    pending = {(entry.get("slice") or {}).get("id") for entry in entries if entry.get("review_state") == "pending"}
    decided = {(entry.get("slice") or {}).get("id") for entry in entries}
    out = []
    for unit in run.get("units") or []:
        info = (run.get("slices") or {}).get(unit["id"]) or {}
        flagged = unit["id"] in pending or (unit["id"] not in decided and bool(pending))
        if info.get("commit") and not info.get("reverted") and flagged:
            out.append(info)
    return out


def entry_marks(project_id: str) -> dict[str, dict]:
    """条目 → 「需复核（自主构建）」标记 {source, run_id}：还标着需复核的段生效时改到的条目（Studio 表格与详情）。"""
    from ...kernel.changesets import store as changeset_store

    root = Path(_ws().project_root(project_id))
    marks: dict[str, dict] = {}
    for run in store.runs(project_id):
        for info in _flagged(run):
            record = changeset_store.get(root, info.get("changeset_id") or "") or {}
            for ref in record.get("entries") or []:
                marks.setdefault(ref.get("id"), {"source": "autobuild", "run_id": run["run_id"]})
    return marks


def review_ratio(project_id: str | None = None) -> float | None:
    """复核时被改掉的比例：改判 / 复核过的（可观测，详设 8.5）。"""
    runs = store.runs(project_id) if project_id else store.all_runs()
    reviewed = []
    for run in runs:
        reviewed += [entry for entry in run.get("ledger") or [] if entry.get("review_state") != "pending"]
    if not reviewed:
        return None
    return round(sum(1 for entry in reviewed if entry["review_state"] == "rejudged") / len(reviewed), 4)
