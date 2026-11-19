"""自主构建运行器（方案 B12、B13；详设 8.5、8.4；契约 AB-03～AB-09、AB-19、7.15、EP-14～EP-17）。

按流程顺序一个节点一个单元（`plan`），每个单元：

1. 新开上下文（关掉这条对话的模型上下文、历史里记一个分界），长上下文自动压缩不会丢掉台账；
2. 先占一个变更集（`source=autobuild`、`build_mode=L4`），注入台账摘要，以项目所有者的身份发节点的主命令
   （`/ontology-extract-object` 等），按条件等这一轮出 result；
3. 查执行记录：做完了就往下走；没做完也没挂起就说「继续」，最多 `CONTINUE_LIMIT` 次；出错按执行记录续跑；
4. 自动合入（`changesets.merge(source="autobuild", needs_review=True)`）；合入前检查有新错误时让助手修，最多策略
   里的轮数，还有就照样合入（F3：检查只提示，发布时才拦），标需复核；要删除已确认的内容时挂起这一步、撤回这一步的
   修改；合入冲突停下（冲突进待处理）；
5. 合入之后是安全点：暂停、停在安全点、转为陪伴在这里生效。

运行只在做完、叫停、出错时结束，不按用量判停（F6）；发一步时配额用完就暂停在这一步，补足后续跑。

运行状态与台账在数据目录（`store`），结束时台账与运行报告写进工作区 `reports/autobuild/<运行号>/`（进 git）。运行在自己
的对话里（「自主构建 · 运行 #n」），进展、自动决定、挂起都显示成每个单元一张进展块。老运行（这一版之前开始的）的单元是按段
的「全部」× 几步，原样接着跑。

重启：运行记录里的进度是检查点；启动恢复把在跑的运行改判中断（`register_steps`），启动后自动续跑
（`resume_after_restart`），每一步按执行记录接着做。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid

from ...api.errors import ApiError
from .. import workspace_labels
from ..flow.need import OPTIONAL, need_kind
from . import budget, decide, hint, limits, plan, store, views

_log = logging.getLogger(__name__)

PREVIEW_TTL_S = 600
# 开始自主构建时撤回别的对话里没回答的问题：问题卡上写的原因
TAKE_OVER_REASON = "已交给自主构建，按「拿不准时」的策略处理"
CONTINUE_LIMIT = 3
ERROR_LIMIT = 2
WAIT_TICK_S = 5.0
REPORT_DIR = "reports/autobuild"
END_NOTICE = {"completed": "已完成", "stopped": "已在安全点停下", "converted": "已转为陪伴", "failed": "出错停下"}
NOTIFY_KIND = {"completed": "autobuild_finished", "converted": "autobuild_finished"}


def _done_by_upstream(project_id: str, unit: dict) -> bool:
    """可选节点走到时已经是「已确认」：上游顺带做完了（提取操作连规则、指标一起抽），这一步不必再做。
    开始时就已确认的节点不进运行；过期的、从别的对话接过来做到一半的、带着上次挂起问题补做的照做。"""
    if unit.get("kind") != "node" or unit.get("carried") or any("resume_from" in step for step in unit["steps"]):
        return False
    root = _ws().project_root(project_id)
    flow_doc = plan.flow(root)
    node = next((item for item in flow_doc.get("nodes") or [] if item["id"] == unit["id"]), None)
    if node is None or need_kind(node) != OPTIONAL:
        return False
    return plan.node_states(root, flow_doc).get(unit["id"]) == "confirmed"


class _Stop(Exception):
    """一步发不出去：配额用完（outcome=quota，运行暂停，补足后续跑）或会话出错（failed）。"""

    def __init__(self, outcome: str, reason: str = ""):
        super().__init__(reason or outcome)
        self.outcome = outcome
        self.reason = reason


def _ws():
    from ...api import deps

    return deps.workspaces


def _manager():
    from .. import agent_sessions

    return agent_sessions.manager


def _publish(project_id: str, event_type: str, data: dict) -> None:
    from .. import streams

    try:
        streams.publish_project(project_id, event_type, data)
    except Exception:  # noqa: BLE001 - 事件是增强信息
        _log.warning("项目流事件 %s 发不出去", event_type, exc_info=True)


def _record_publish_leftover(project_id: str, owner: str) -> None:
    """校验这一步合入后：主线上还会挡发布的问题记进待处理清单，查干净了删掉清单（ADR-0103）。"""
    from ..publish import readiness

    found = readiness.mainline_findings(_ws(), project_id)
    if found is not None:
        readiness.record_leftover(_ws(), project_id, found, owner)


def run_view(run: dict) -> dict:
    return views.run_view(run)


def _ran_step(record: dict, run: dict, step: dict, started: float) -> bool:
    """执行记录是这次运行在这个单元开始之后跑的这一步。"""
    if record.get("run_id") != run["run_id"] or record.get("command") != step["command"]:
        return False
    return float(record.get("started_at") or 0) >= started


def _stopped_for_reply(record: dict, flow: dict) -> bool:
    """执行停在等你回答（命令锁在「等你确认」「等你回复」，或这一轮已结束在等回复）：活干完了，只差回答。"""
    from ..session_host import exec_records

    if record.get("state") == "waiting":
        return True
    raw = (record.get("phase") or {}).get("raw")
    return exec_records.waiting_for_reply(raw, flow, record.get("command_name") or "")


def _blueprint_unanswered(ws, project_id: str) -> int:
    from ..flow import blueprint

    try:
        doc = blueprint.effective_doc(ws, project_id)
    except Exception:  # noqa: BLE001 - 预览的提示是增强信息
        return 0
    return sum(1 for question in (doc or {}).get("questions") or [] if not question.get("answer"))


class AutobuildRunner:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._previews: dict[str, dict] = {}
        self._admit_lock: asyncio.Lock | None = None
        # 按项目串行开始、控制（审查 H 与推断：两次「开始」、停止与续跑并发）；锁跟着事件循环换
        self._project_locks: dict[str, tuple[object, asyncio.Lock]] = {}

    # ------------------------------------------------------------------
    # 开始前预览（AB-03）与开始（AB-04）
    # ------------------------------------------------------------------

    def preview(self, project_id: str, policy_doc: dict) -> dict:
        """AutobuildPreview；`policy_doc` 已校验过。在线程里调。"""
        from ...kernel.changesets import store as changeset_store

        ws = _ws()
        running = store.active(project_id)
        if running is not None:
            raise ApiError("autobuild_running", "这个项目有一次自主构建还没结束", 409, {"run_id": running["run_id"]})
        # 不要求先确认蓝图（F8）
        units, rows = plan.build(ws, project_id, policy_doc)
        if not units:
            raise ApiError("invalid_argument", "范围里没有要做的步骤", 400, {"fields": {"scope": "empty"}})
        warnings = []
        pending = changeset_store.current(ws.project_root(project_id))
        if pending is not None and pending.get("state") == "conflict":
            warnings.append("有一轮的修改和你的修改冲突，先在待处理里选好保留哪边")
        unanswered = _blueprint_unanswered(ws, project_id)
        if unanswered:
            # 契约 BP-03：蓝图没答的问题留在待处理，运行不代答（实测：开始前没提，跑完还在待处理里）
            warnings.append(f"建模蓝图里有 {unanswered} 个问题没回答，运行不会替你答，会留在待处理里")
        now = time.time()
        for token, item in list(self._previews.items()):
            if item["expires"] < now:
                self._previews.pop(token, None)
        token = f"pv-{secrets.token_urlsafe(12)}"
        self._previews[token] = {"project_id": project_id, "policy": policy_doc, "expires": now + PREVIEW_TTL_S}
        slots = limits.max_runs()
        busy = len(store.all_runs(store.RUNNING))
        queued = busy >= slots
        live = _manager().live_status(project_id)
        from . import policy as policy_module

        return {
            "preview_token": token, "expires_at": views.iso(now + PREVIEW_TTL_S),
            "slices": rows,
            "below_threshold_text": policy_module.BELOW_TEXT[policy_doc["advanced"]["below_threshold"]],
            "warnings": warnings, "queued": queued,
            "queue_position": (len(store.all_runs(("queued",))) + 1) if queued else None,
            # 别的对话正占着助手：在等你回答（waiting）还是在干活；开始时带 take_over 就先接过来
            "busy": {"conversation_id": live["conversation_id"], "waiting": live["waiting"]}
            if live and live["busy"] else None,
        }

    async def start(self, project_id: str, token: str, *, actor: str, owner: str, take_over: bool = False) -> dict:
        """AB-04：开一条「自主构建 · 运行 #n」对话，记下运行，轮到时开始。同一项目的两次开始串行（审查 H）。"""
        async with self._project_lock(project_id):
            return await self._start(project_id, token, actor=actor, owner=owner, take_over=take_over)

    async def _start(self, project_id: str, token: str, *, actor: str, owner: str, take_over: bool = False) -> dict:
        from ...governance import quota
        from ...governance.http import reject_new_session
        from ...kernel.changesets import store as changeset_store
        from .. import conversation_store, mainline, project_events

        item = self._previews.get(token or "")
        if item is None or item["project_id"] != project_id or item["expires"] < time.time():
            raise ApiError("preview_expired", "开始前预览已过期，请重新预览", 410)
        running = store.active(project_id)
        if running is not None:
            raise ApiError("autobuild_running", "这个项目有一次自主构建还没结束", 409, {"run_id": running["run_id"]})
        reject_new_session(project_id)
        manager = _manager()
        live = manager.live_status(project_id)
        handover = {"exec_id": None, "questions": []}
        if live and live["busy"] and take_over:
            handover = await manager.take_over(project_id, TAKE_OVER_REASON)
            live = manager.live_status(project_id)
        if live and live["busy"]:
            message = "助手在等你回答一个问题，回答或打断后再开始" if live["waiting"] else "助手正在另一条对话里干活，等它结束或先打断"
            raise ApiError("session_busy", message, 409,
                           {"running_conversation_id": live["conversation_id"], "waiting": live["waiting"]})
        ws = _ws()
        root = ws.project_root(project_id)
        # 上一轮遗留的修改先合入成版本（F3）；只有合入冲突要你先处理
        from ...kernel.changesets import service as changesets

        await asyncio.to_thread(changesets.settle, ws, project_id, actor=owner, turn_over=True)
        pending = changeset_store.current(root)
        if pending is not None and pending.get("state") in ("open", "pending_confirm", "conflict"):
            raise ApiError("changeset_in_progress", "有一轮的修改和你的修改冲突，先在待处理里选好保留哪边", 409,
                           {"changeset_id": pending["id"], "conversation_id": pending.get("conversation_id")})
        await asyncio.to_thread(quota.check_before_turn, project_id, owner, "tokens")
        self._previews.pop(token, None)
        policy_doc = item["policy"]
        redo = await asyncio.to_thread(self._unfinished_step, project_id, handover.get("exec_id"))
        units, rows = await asyncio.to_thread(plan.build, ws, project_id, policy_doc, redo)
        if not units:
            raise ApiError("invalid_argument", "范围里没有要做的步骤", 400, {"fields": {"scope": "empty"}})
        run_no = store.next_run_no(project_id)
        run_id = f"run-{run_no}"
        conversation = conversation_store.create(project_id, kind="autobuild", run_id=run_id,
                                                 title=f"自主构建 · 运行 #{run_no}")
        state = await asyncio.to_thread(mainline.state, root)
        now = time.time()
        record = {
            "project_id": project_id, "run_id": run_id, "run_no": run_no, "state": "queued",
            "conversation_id": conversation["id"], "owner": owner, "actor": actor,
            "created_at": now, "started_at": None, "ended_at": None, "policy": policy_doc,
            "units": units, store.UNITS_KEY: {}, "cursor": {}, "flags": {}, "budget": budget.empty(), "ledger": [],
            "suspensions": [], "notes": [], "cards": [], "recovered": 0, "resume_pending": False,
            "base_commit": state.get("head"), "base_version_label": state.get("version_label"),
            "failure": None, "report": None, "pending_rejudges": [],
        }
        await asyncio.to_thread(store.save, record)
        self._post_card(record, views.start_card(record))
        await self._hand_over(record, handover)
        project_events.conversation_changed(project_id, conversation["id"])
        _publish(project_id, "autobuild.run.updated", views.summary(record))
        await self.admit()
        return run_view(store.get(project_id, run_id) or record)

    # ------------------------------------------------------------------
    # 接手别的对话里没做完的（开始时）
    # ------------------------------------------------------------------

    async def _hand_over(self, run: dict, handover: dict) -> None:
        """运行建好之后：接管时撤回的问题按策略定下来记进台账（或挂起），别的对话里没做完的执行交给运行收尾。

        开始失败（合入冲突、配额）走不到这里：问题已撤回，执行留着中断，照常能续跑。"""
        project_id, run_id = run["project_id"], run["run_id"]
        pairs = handover.get("questions") or []
        if pairs:
            try:
                _answers, entries, suspensions = await asyncio.to_thread(
                    decide.record, project_id, run_id, [question for question, _ in pairs],
                    [raw for _, raw in pairs], handover.get("exec_id"),
                    unit_ref=self._handover_unit(run, handover.get("exec_id")), handed_over=True)
                await self.on_decisions(project_id, run_id, entries, suspensions)
            except Exception:  # noqa: BLE001 - 记台账失败不挡开始；问题已撤回，运行里助手会再问
                _log.exception("接管时撤回的问题没记进台账（%s %s）", project_id, run_id)
        try:
            closed = await asyncio.to_thread(self._close_handed_over, run, handover.get("exec_id"))
        except Exception:  # noqa: BLE001 - 收尾失败时续跑项留着，和修复前一样
            _log.exception("别的对话里没做完的执行没收尾（%s %s）", project_id, run_id)
            return
        by_conversation: dict[str, list[str]] = {}
        for record in closed:
            labels = by_conversation.setdefault(record.get("conversation_id") or "", [])
            label = record.get("command_label") or "这一步"
            if label not in labels:
                labels.append(label)
        for conversation_id, labels in by_conversation.items():
            if not conversation_id:
                continue
            steps = "、".join(f"「{label}」" for label in labels)
            text = f"{steps}交给自主构建 · 运行 #{run['run_no']}，这里不用再继续"
            try:
                _manager().append_record(project_id, conversation_id, {"type": "notice", "payload": {
                    "kind": "info", "text": text, "version_label": None, "changeset_id": None, "align": "center"}})
            except Exception:  # noqa: BLE001 - 对话删了：不留这一行
                _log.warning("对话 %s 里没留下交接说明（%s）", conversation_id, project_id, exc_info=True)

    @staticmethod
    def _unfinished_step(project_id: str, exec_id: str | None) -> dict:
        """被接管打断、停在干活中途的那一步，运行要接着做完：{节点: {step, phase, command}}。

        合入时条目写了一半也按「条目在」算这一步做完（实测：「写入对象」中途接管，对象一步记成已确认，运行跳过对象）。
        停在「等你确认」「等你回复」的不算：活已经干完，只差回答，回答交给台账。"""
        from ..session_host import exec_records

        record = exec_records.get(project_id, exec_id) if exec_id else None
        if record is None or record.get("state") != "interrupted":
            return {}
        flow = plan.flow(_ws().project_root(project_id))
        if _stopped_for_reply(record, flow):
            return {}
        phase = (record.get("phase") or {}).get("label") or ""
        node_id = exec_records.node_of(record, flow) or ""
        if not node_id or node_id.startswith("command."):
            return {}
        return {node_id: {"step": node_id, "phase": phase, "command": record.get("command")}}

    @staticmethod
    def _handover_unit(run: dict, exec_id: str | None) -> dict:
        """接管时撤回的问题记在哪个单元：被打断的那一步的节点，运行里没有它时记在第一个单元。"""
        from ..session_host import exec_records

        units = run.get("units") or []
        record = exec_records.get(run["project_id"], exec_id) if exec_id else None
        wanted = exec_records.node_of(record, plan.flow(_ws().project_root(run["project_id"]))) if record else None
        unit = next((item for item in units if item["id"] == wanted), units[0] if units else None)
        return {"id": unit["id"], "name": unit["name"]} if unit else {"id": None, "name": ""}

    def _close_handed_over(self, run: dict, taken_exec: str | None) -> list[dict]:
        """别的对话里没做完的执行交给运行收尾，返回收尾了的执行记录。

        收尾 = 记为放弃、清掉命令锁，不再列「没做完」、建议条不再推「继续」：
        - 运行计划里有这一步：运行接着做，交给它；
        - 不在计划里：停在「等你确认」「等你回复」（活干完了，只差回答）、这一步也已经做完的收尾；停在干活中途的留着续跑
          ——条目写了一半也按「条目在」算做完，收掉就没人知道它没做完。"""
        from ..session_host import exec_records, turns

        project_id = run["project_id"]
        root = _ws().project_root(project_id)
        candidates = [record for record in exec_records.query(project_id, ["running", "waiting", "interrupted"])
                      if not record.get("run_id") and record.get("conversation_id") != run["conversation_id"]]
        if not candidates:
            return []
        flow = plan.flow(root)
        node_states: dict | None = None
        closed = []
        for record in candidates:
            if not self._in_run(run, record, flow):
                if not _stopped_for_reply(record, flow):
                    continue
                if node_states is None:
                    node_states = plan.node_states(root, flow)
                if node_states.get(exec_records.node_of(record, flow)) != "confirmed":
                    continue
            self._close_exec(project_id, record["id"])
            updated = exec_records.update(project_id, record["id"],
                                          handed_over={"run_id": run["run_id"], "run_no": run["run_no"],
                                                       "taken_over": record["id"] == taken_exec})
            if updated is not None:
                turns.publish_exec(project_id, updated, root)
                closed.append(updated)
        return closed

    @staticmethod
    def _in_run(run: dict, record: dict, flow: dict) -> bool:
        """这次执行的那一步在运行计划里：按它的节点对（老记录的建模按步骤认成提取对象、提取操作）。"""
        from ..session_host import exec_records

        return exec_records.node_of(record, flow) in {unit["id"] for unit in run.get("units") or []}

    # ------------------------------------------------------------------
    # 名额与驱动
    # ------------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._admit_lock is None or getattr(self._admit_lock, "_onto_loop", None) is not loop:
            self._admit_lock = asyncio.Lock()
            self._admit_lock._onto_loop = loop
        return self._admit_lock

    def _project_lock(self, project_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        found = self._project_locks.get(project_id)
        if found is None or found[0] is not loop:
            found = self._project_locks[project_id] = (loop, asyncio.Lock())
        return found[1]

    async def admit(self) -> None:
        """有名额就按先来后到开始排队的运行（同时运行数上限 SY-17）；维护期间不开新的。"""
        from ...governance import ops_state

        if ops_state.rejects_new_sessions():
            return
        async with self._lock():
            slots = limits.max_runs()
            busy = len(store.all_runs(store.RUNNING))
            queued = sorted(store.all_runs(("queued",)), key=lambda run: float(run.get("created_at") or 0))
            for run in queued:
                if busy >= slots:
                    break
                task = self._tasks.get(run["project_id"])
                if task is not None and not task.done() and task is not asyncio.current_task():
                    continue  # 这个项目的上一次驱动还没退出（自己正在收尾时不算）
                busy += 1
                self._spawn(run["project_id"], run["run_id"])

    def _spawn(self, project_id: str, run_id: str) -> None:
        def mark(run: dict) -> None:
            run["state"] = run.pop("resume_state", None) or "running"
            run["started_at"] = run.get("started_at") or time.time()
            budget.start_clock(run)

        store.update(project_id, run_id, mark)
        task = asyncio.create_task(self._drive(project_id, run_id))
        self._tasks[project_id] = task

    def running(self, project_id: str) -> bool:
        task = self._tasks.get(project_id)
        return task is not None and not task.done()

    async def _drive(self, project_id: str, run_id: str) -> None:
        from ...governance import context

        context.set_label("project", project_id)
        manager = _manager()
        try:
            manager.set_run(project_id, run_id)
            run = store.get(project_id, run_id)
            self._updated(run)
            if run is not None and run.get("finishing"):
                # 收尾到一半停了机（审查 M）：接着收尾，不再做别的
                await self._finish(project_id, run_id, run["finishing"].get("state") or "completed")
                return
            while True:
                run = store.get(project_id, run_id)
                if run is None or run["state"] not in store.RUNNING:
                    return
                unit = self._next_unit(run)
                fresh = unit is not None and store.unit_info(run, unit["id"]).get("state") in (None, "queued")
                ending = self._ending(run)
                if unit is None or (fresh and ending):
                    # 做完了；或者已经在安全点（这个单元还没开始）又要停
                    await self._finish(project_id, run_id, ending or "completed")
                    return
                if fresh and run.get("flags", {}).get("pause"):
                    await self._pause(project_id, run_id)
                    return
                outcome = await self._run_unit(project_id, run_id, unit)
                if outcome == "quota" and self._ending(store.get(project_id, run_id) or run):
                    # 叫停或转为陪伴之后撞上配额：这个单元做不完，按叫停的方式结束（已有的修改照样合入）
                    await self._finish(project_id, run_id, self._ending(store.get(project_id, run_id) or run))
                    return
                if outcome in ("paused", "quota"):
                    await self._pause(project_id, run_id, reason="quota" if outcome == "quota" else None)
                    return
                if outcome == "failed":
                    await self._finish(project_id, run_id, "failed")
                    return
                nxt = await self._safe_point(project_id, run_id, unit)
                if nxt == "paused":
                    await self._pause(project_id, run_id)
                    return
                if nxt != "continue":
                    await self._finish(project_id, run_id, nxt)
                    return
        except Exception:  # noqa: BLE001 - 停机的 CancelledError 不在其中：运行记录停在检查点，启动恢复接着跑
            _log.exception("自主构建运行出错（%s %s）", project_id, run_id)
            try:
                await self._finish(project_id, run_id, "failed", reason="internal")
            except Exception:  # noqa: BLE001
                _log.exception("收尾失败（%s %s）", project_id, run_id)
        finally:
            if self._tasks.get(project_id) is asyncio.current_task():
                self._tasks.pop(project_id, None)

    @staticmethod
    def _next_unit(run: dict) -> dict | None:
        for unit in run.get("units") or []:
            if store.unit_info(run, unit["id"]).get("state") not in ("done", "partial", "skipped"):
                return unit
        return None

    @staticmethod
    def _ending(run: dict) -> str | None:
        """要在安全点结束的原因：只有用户叫停、转为陪伴；用量不判停（F6）。"""
        flags = run.get("flags") or {}
        if flags.get("stop"):
            return "stopped"
        if flags.get("convert"):
            return "converted"
        return None

    # ------------------------------------------------------------------
    # 一个单元
    # ------------------------------------------------------------------

    async def _run_unit(self, project_id: str, run_id: str, unit: dict) -> str:
        from ...kernel.changesets import service as changesets

        run = store.get(project_id, run_id)
        fresh = store.unit_info(run, unit["id"]).get("state") in (None, "queued")
        if fresh and await asyncio.to_thread(_done_by_upstream, project_id, unit):
            return self._already_done(project_id, run_id, unit)
        if fresh:
            from ..flow import state as node_state

            # 运行前的确认记录（按节点）：撤回时放回去（原来过期的仍是过期，审查 K）
            before = await asyncio.to_thread(node_state.records_of, _ws().project_root(project_id), plan.nodes_of(unit))

            def begin(record: dict) -> None:
                info = store.unit_entry(record, unit["id"])
                info.update(state="running", started_at=time.time(), steps={step["id"]: "queued" for step in unit["steps"]},
                            decisions=info.get("decisions") or [], suspensions=info.get("suspensions") or [],
                            timeline=info.get("timeline") or [], fix_rounds=0, produced={}, checks={},
                            state_before=before)
                record["cursor"] = {"unit": unit["id"], "step": None, "exec_id": None, "continues": 0}

            run = store.update(project_id, run_id, begin)
            await self._new_context(run, unit)
        else:
            merged = self._merged_already(project_id, run, unit)
            if merged is not None:
                # 合入刚做完就停了机（审查 C）：这个单元已经在主线上，接回它的合入提交，不再另开变更集
                return self._unit_done(project_id, run_id, unit, record=merged,
                                       merged={"commit": (merged.get("merged") or {}).get("commit"),
                                               "version_label": (merged.get("merged") or {}).get("version_label")})
        self._unit_card(run, unit)
        ws = _ws()
        try:
            slot = await asyncio.to_thread(
                changesets.open_changeset, ws, project_id, source="autobuild", build_mode="L4", run_id=run_id,
                slice_id=unit["id"], purpose=f"自主构建「{unit['name']}」", conversation_id=run["conversation_id"])
        except Exception:  # noqa: BLE001
            _log.exception("先占变更集失败（%s %s）", project_id, unit["id"])
            self._fail(project_id, run_id, "changeset")
            return "failed"
        if slot.get("source") != "autobuild" or slot.get("run_id") != run_id:
            self._fail(project_id, run_id, "changeset_in_progress")
            return "failed"
        store.update(project_id, run_id,
                     lambda record: store.unit_entry(record, unit["id"]).update(changeset_id=slot["id"]))
        for step in unit["steps"]:
            run = store.get(project_id, run_id)
            if (store.unit_info(run, unit["id"]).get("steps") or {}).get(step["id"]) in ("done", "partial", "skipped"):
                continue
            try:
                outcome = await self._run_step(project_id, run_id, unit, step)
            except _Stop as stop:
                return self._stopped(project_id, run_id, stop)
            if outcome == "failed":
                return "failed"
            run = store.get(project_id, run_id)
            last = step is unit["steps"][-1]
            if not last and (run.get("flags") or {}).get("pause"):
                return "paused"
        try:
            return await self._merge(project_id, run_id, unit)
        except _Stop as stop:
            return self._stopped(project_id, run_id, stop)

    def _stopped(self, project_id: str, run_id: str, stop: _Stop) -> str:
        """发不出去的一步：会话出错记为失败原因；配额用完不算失败，运行暂停。"""
        if stop.outcome != "quota":
            self._fail(project_id, run_id, stop.reason or stop.outcome)
        return stop.outcome

    @staticmethod
    def _merged_already(project_id: str, run: dict, unit: dict) -> dict | None:
        from ...kernel.changesets import store as changeset_store

        changeset_id = store.unit_info(run, unit["id"]).get("changeset_id")
        if not changeset_id:
            return None
        record = changeset_store.get(_ws().project_root(project_id), changeset_id)
        return record if record and record.get("state") == "merged" and record.get("run_id") == run["run_id"] else None

    async def _new_context(self, run: dict, unit: dict) -> None:
        """每个单元新开上下文（方案 B12 第 1 步）：关掉模型上下文、历史里记分界（Jiuwen 回放从这里起）。"""
        from ..agent_sessions import CONTEXT_RESET

        manager = _manager()
        try:
            await manager.close(run["project_id"], run["conversation_id"])
        except Exception:  # noqa: BLE001 - 对话已删除时下面照样失败，交给发送报错
            _log.warning("新开上下文失败（%s）", run["project_id"], exc_info=True)
        manager.set_run(run["project_id"], run["run_id"])
        manager.append_record(run["project_id"], run["conversation_id"],
                              {"type": CONTEXT_RESET, "run_id": run["run_id"], "unit": unit["id"]})

    def _write_hint(self, run: dict, unit: dict) -> dict:
        """发一步之前写注入；没送出的用户补充一起带上，记为已读。"""
        notes = [item["text"] for item in run.get("notes") or [] if not item.get("read")]
        carried = []
        for ref in unit.get("carried") or []:
            found = store.find_suspension(run["project_id"], ref["id"])
            if found is not None and found[1].get("state") == "deferred":
                carried.append(found[1])
        hint.write(run["project_id"], run["conversation_id"],
                   hint.compose(run, unit, notes=notes, carried=carried))
        if notes:
            def mark(record: dict) -> None:
                for item in record.get("notes") or []:
                    item["read"] = True

            run = store.update(run["project_id"], run["run_id"], mark) or run
        return run

    async def _send(self, project_id: str, run_id: str, unit: dict, text: str, *, resume_exec: str | None = None) -> str | None:
        """以项目所有者的身份发一步（不进对话流：进展在单元的进展块里）；返回这一步的执行记录编号。"""
        from ...governance.quota import QuotaExceeded
        from ..agent_sessions import SessionError

        run = store.get(project_id, run_id)
        run = await asyncio.to_thread(self._write_hint, run, unit)
        manager = _manager()
        meta = {"display_text": text, "origin": None, "hidden": True, "autobuild_run": run_id}
        if resume_exec:
            meta["resume_exec"] = resume_exec
        try:
            session = await manager.send(project_id, _ws().project_root(project_id), text, run["conversation_id"],
                                         username=run.get("owner") or "", meta=meta)
        except QuotaExceeded as exc:
            raise _Stop("quota") from exc
        except SessionError as exc:
            raise _Stop("failed", f"session:{exc.status}") from exc
        return session.exec_id

    async def _wait_turn(self, project_id: str, run_id: str, conversation_id: str) -> str:
        """按条件等这一轮结束：idle、error、gone（会话被回收或换了对话）。等的过程中隔一会儿记一次用量。"""
        manager = _manager()
        while True:
            event = manager.signal(project_id)
            event.clear()
            session = manager.get(project_id)
            if session is None or session.conversation_id != conversation_id or session.state == "closed":
                return "gone"
            starting = session.sending or session.is_connecting() or manager.queue_position(project_id) is not None
            if session.state == "error" and not starting:
                # 刚发出的续跑还在重连：会话仍停在上一轮的 error，不能再记一次出错（V 阶段真实模型）
                return "error"
            busy = (session.sending or session.finishing or session.pending_prompts or session.is_connecting()
                    or manager.queue_position(project_id) is not None or session.state in ("running", "queued")
                    or session.turns_in_flight > 0 or session.steers or session.chaining)
            if not busy:
                return "idle"
            try:
                await asyncio.wait_for(event.wait(), WAIT_TICK_S)
            except asyncio.TimeoutError:
                await self._account(project_id, run_id)

    async def _run_step(self, project_id: str, run_id: str, unit: dict, step: dict) -> str:
        """跑一步：发命令，等结果，没做完就「继续」（限次数），出错按执行记录续跑。返回 done、partial、failed。"""
        from ..session_host import exec_records, turns

        run = store.get(project_id, run_id)
        cursor = run.get("cursor") or {}
        same = cursor.get("unit") == unit["id"] and cursor.get("step") == step["id"]
        exec_id = cursor.get("exec_id") if same else None
        if same and exec_id is None:
            # 发出去之后、记下执行编号之前停了机：按执行记录找回这一步
            exec_id = self._find_exec(project_id, run, unit, step)
        continues = int(cursor.get("continues") or 0) if same else 0
        started = time.time()

        def mark_running(record: dict) -> None:
            info = store.unit_entry(record, unit["id"])
            info["steps"][step["id"]] = "running"
            info["current_step"] = step["id"]
            if not any(item.get("type") == "step" and item.get("id") == step["id"] for item in info["timeline"]):
                info["timeline"].append({"type": "step", "id": step["id"], "text": step["action"], "meta": "",
                                         "state": "running", "started_at": started, "duration_ms": None})
            record["cursor"] = {"unit": unit["id"], "step": step["id"], "exec_id": exec_id, "continues": continues}

        run = store.update(project_id, run_id, mark_running)
        self._unit_card(run, unit)
        self._updated(run)
        suspended_before = len(store.unit_info(run, unit["id"]).get("suspensions") or [])
        if exec_id is None:
            command = " ".join([step["command"], *[f"{key}={value}" for key, value in step["args"].items()]])
            exec_id = await self._send(project_id, run_id, unit, command)
        else:
            record = exec_records.get(project_id, exec_id) or {}
            if record.get("state") in ("done", "abandoned"):
                return self._step_done(project_id, run_id, unit, step, "done")
            text = turns.resume_text(record) if record.get("state") == "interrupted" else "继续"
            exec_id = await self._send(project_id, run_id, unit, text,
                                       resume_exec=exec_id if record.get("state") == "interrupted" else None) or exec_id
        store.update(project_id, run_id, lambda record: record["cursor"].update(exec_id=exec_id))
        errors = 0
        while True:
            outcome = await self._wait_turn(project_id, run_id, run["conversation_id"])
            if store.get(project_id, run_id) is None:
                return "failed"
            await self._account(project_id, run_id)
            record = exec_records.get(project_id, exec_id) if exec_id else None
            if record is None or record.get("state") in ("done", "abandoned"):
                return self._step_done(project_id, run_id, unit, step, "done")
            if outcome in ("error", "gone") or record.get("state") == "interrupted":
                errors += 1
                if errors > ERROR_LIMIT:
                    self._fail(project_id, run_id, "engine")
                    return "failed"
            run = store.get(project_id, run_id)
            suspended = len(store.unit_info(run, unit["id"]).get("suspensions") or []) > suspended_before
            if suspended or continues >= CONTINUE_LIMIT:
                # 挂起了这一步的问题、或说了几次「继续」还没做完：这一步先到这里，接着做下一步
                await asyncio.to_thread(self._close_exec, project_id, exec_id)
                return self._step_done(project_id, run_id, unit, step, "partial")
            continues += 1
            store.update(project_id, run_id, lambda record: record["cursor"].update(continues=continues))
            if record.get("state") == "interrupted":
                exec_id = await self._send(project_id, run_id, unit, turns.resume_text(record), resume_exec=exec_id) or exec_id
            else:
                exec_id = await self._send(project_id, run_id, unit, "继续") or exec_id

    @staticmethod
    def _find_exec(project_id: str, run: dict, unit: dict, step: dict) -> str | None:
        from ..session_host import exec_records

        started = float(store.unit_info(run, unit["id"]).get("started_at") or 0)
        for record in exec_records.query(project_id, conversation_id=run["conversation_id"]):
            if _ran_step(record, run, step, started):
                return record["id"]
        return None

    def _step_done(self, project_id: str, run_id: str, unit: dict, step: dict, state: str) -> str:
        now = time.time()

        def mark(record: dict) -> None:
            info = store.unit_entry(record, unit["id"])
            info["steps"][step["id"]] = state
            info["current_step"] = None
            for item in info["timeline"]:
                if item.get("type") == "step" and item.get("id") == step["id"]:
                    item["state"] = "done"
                    item["duration_ms"] = int((now - float(item.get("started_at") or now)) * 1000)
                    item["meta"] = f"{max(1, round(item['duration_ms'] / 60000))} 分"
            record["cursor"] = {"unit": unit["id"], "step": None, "exec_id": None, "continues": 0}

        run = store.update(project_id, run_id, mark)
        self._unit_card(run, unit)
        self._updated(run)
        return state

    @staticmethod
    def _close_exec(project_id: str, exec_id: str | None) -> None:
        """没做完就往下走的一步：执行记为放弃、副本里的命令锁清掉（下一步不会被它挡住）。"""
        from ...kernel.changesets import service as changesets
        from ...kernel.changesets import worktree
        from ..session_host import exec_records

        if not exec_id:
            return
        record = exec_records.get(project_id, exec_id)
        if record is None or record.get("state") in ("done", "abandoned"):
            return
        exec_records.abandon(project_id, exec_id)
        session = _manager().get(project_id)
        if session is not None and session.exec_id == exec_id:
            session.exec_id = None
        copy = changesets.existing_copy(_ws().project_root(project_id))
        if copy is not None and record.get("command_name"):
            worktree.clear_run_state(copy, [record["command_name"]])

    # ------------------------------------------------------------------
    # 合入与安全点
    # ------------------------------------------------------------------

    async def _merge(self, project_id: str, run_id: str, unit: dict) -> str:
        """自动合入这个单元；门禁不过让助手修（最多策略里的轮数）。返回 merged、failed。"""
        from ...kernel.changesets import service as changesets
        from ...services.workspace.errors import WorkspaceError
        from ..session_host import exec_records

        ws = _ws()
        run = store.get(project_id, run_id)
        changeset_id = store.unit_info(run, unit["id"]).get("changeset_id")
        owner = run.get("owner") or run.get("actor") or ""
        records = exec_records.query(project_id, conversation_id=run["conversation_id"])
        last_exec = records[0] if records else None
        # 这个单元最后的样子再看一次（带检查）：一轮收尾时会话宿主已经看过，补上之后在副本里的改动
        await asyncio.to_thread(changesets.observe, ws, project_id, running=False,
                                conversation_id=run["conversation_id"], exec_record=last_exec, checks=True)
        current = changesets.current(ws, project_id)
        if current is None or current["id"] != changeset_id or not current.get("tree"):
            # 没改出东西：先占的变更集关掉，版本不变
            if current is not None and current["id"] == changeset_id:
                await asyncio.to_thread(changesets.discard, ws, project_id, changeset_id, actor=owner)
            return self._unit_done(project_id, run_id, unit, record=None)
        limit = int(((run.get("policy") or {}).get("advanced") or {}).get("max_fix_rounds") or 0)
        # 合入前检查有新错误、校验这一步还有会挡发布的问题（ADR-0103）：让助手修，最多策略里的轮数；
        # 还有就照样合入，只提示（F3），会挡发布的记进待处理清单
        while True:
            checks = (changesets.current(ws, project_id) or {}).get("checks") or {}
            findings = [item for item in checks.get("findings") or [] if item.get("level") == "error"]
            publish = checks.get("publish") or []
            rounds = int(store.unit_info(store.get(project_id, run_id), unit["id"]).get("fix_rounds") or 0)
            if not (findings or publish) or rounds >= limit:
                break
            store.update(project_id, run_id,
                         lambda record: store.unit_entry(record, unit["id"]).update(fix_rounds=rounds + 1))
            await self._fix(project_id, run_id, unit, findings, publish)
            await asyncio.to_thread(changesets.observe, ws, project_id, running=False,
                                    conversation_id=run["conversation_id"], checks=True)
        while True:
            try:
                result = await asyncio.to_thread(
                    changesets.merge, ws, project_id, changeset_id, actor=owner, source="autobuild", needs_review=True,
                    message=f"autobuild: 自主构建运行 #{run['run_no']}「{unit['name']}」（{changeset_id}）")
                break
            except WorkspaceError as exc:
                if exc.code == "delete_forbidden":
                    return await self._give_up(project_id, run_id, unit, changeset_id, owner,
                                               title=f"「{unit['name']}」要删除已确认的内容", flag="delete_forbidden",
                                               reason="、".join(workspace_labels.path_label(rel) or "其他文件"
                                                               for rel in (exc.details or {}).get("paths") or []))
                if exc.code == "changeset_closed":
                    return self._unit_done(project_id, run_id, unit, record=None)
                self._fail(project_id, run_id, exc.code or "merge")
                return "failed"
        if changesets.readiness_step(result["record"]):
            await asyncio.to_thread(_record_publish_leftover, project_id, owner)
        return self._unit_done(project_id, run_id, unit, record=result["record"], merged=result)

    async def _fix(self, project_id: str, run_id: str, unit: dict, findings: list[dict],
                   publish: list[dict] | None = None) -> None:
        from ..publish import readiness

        parts = []
        if findings:
            lines = [f"- {item.get('message') or item.get('code')}（{item.get('path') or ''}）" for item in findings[:10]]
            parts.append(f"合入前的检查发现 {len(findings)} 个新问题。只改「{unit['name']}」这一步写的内容，把它们修好：\n"
                         + "\n".join(lines))
        if publish:
            parts.append(readiness.fix_prompt(publish))
        text = "\n\n".join(parts)
        run = store.get(project_id, run_id)
        await self._send(project_id, run_id, unit, text)
        await self._wait_turn(project_id, run_id, run["conversation_id"])
        await self._account(project_id, run_id)

    async def _give_up(self, project_id: str, run_id: str, unit: dict, changeset_id: str, owner: str, *,
                       title: str, flag: str, reason: str) -> str:
        """这个单元合入不了（要删除已确认的内容）：它的修改撤回，挂起一个问题（进待处理），接着做下一个。"""
        from ...kernel.changesets import service as changesets

        await asyncio.to_thread(changesets.discard, _ws(), project_id, changeset_id, actor=owner)
        handled = "这一步没生效，接着做下一步"

        def change(record: dict) -> None:
            item = {"id": f"sp-{uuid.uuid4().hex[:8]}", "run_id": run_id, "at": time.time(),
                    "slice": {"id": unit["id"], "name": unit["name"]}, "title": title, "reason": reason,
                    "handled_as": handled, "kind": "question", "options": [], "allow_custom": True, "actions": [],
                    "state": "open", "step": None, "exec_id": None, flag: True}
            record.setdefault("suspensions", []).append(item)
            info = store.unit_entry(record, unit["id"])
            info.setdefault("suspensions", []).append(item["id"])
            info.setdefault("timeline", []).append({"type": "suspension", "id": item["id"]})
            info.update({"state": "skipped", flag: True, "ended_at": time.time()})

        run = store.update(project_id, run_id, change)
        self._unit_card(run, unit)
        self._updated(run)
        self._suspended(run, 1)
        return "merged"

    def _unit_done(self, project_id: str, run_id: str, unit: dict, *, record: dict | None,
                   merged: dict | None = None) -> str:
        from ...kernel.changesets import service as changesets
        from .. import mainline

        produced = {"objects": 0, "operations": 0, "rules": 0}
        if record is not None:
            model = changesets._model(_ws().project_root(project_id))
            for status, rel in record.get("changes") or []:
                key = model.entry_key(rel) if status == "A" else None
                kind = views.KINDS.get((key or "").partition(":")[0])
                if kind:
                    produced[kind] += 1
        state = mainline.state(_ws().project_root(project_id))
        checks = (record or {}).get("checks") or {}

        def mark(run: dict) -> None:
            info = store.unit_entry(run, unit["id"])
            partial = any(value == "partial" for value in (info.get("steps") or {}).values())
            info.update(state="partial" if partial else "done", ended_at=time.time(), produced=produced,
                        commit=(merged or {}).get("commit"),
                        version_label=(merged or {}).get("version_label") or state.get("version_label"),
                        checks={"errors": int(checks.get("errors") or 0), "warnings": int(checks.get("warnings") or 0)})

        run = store.update(project_id, run_id, mark)
        self._consume_carried(project_id, run_id, unit)
        self._unit_card(run, unit)
        self._updated(run)
        return "merged"

    def _already_done(self, project_id: str, run_id: str, unit: dict) -> str:
        """可选节点走到时上游已经顺带做完（`_done_by_upstream`）：不开上下文、不开变更集，记为做完接着往下。"""
        def mark(run: dict) -> None:
            now = time.time()
            store.unit_entry(run, unit["id"]).update(
                state="done", already_done=True, started_at=now, ended_at=now,
                produced={"objects": 0, "operations": 0, "rules": 0})

        run = store.update(project_id, run_id, mark)
        self._unit_card(run, unit)
        self._updated(run)
        return "merged"

    @staticmethod
    def _consume_carried(project_id: str, run_id: str, unit: dict) -> None:
        """之前留到这一次的挂起问题：这个单元做完（带着回答补做过）就算回答过，待处理里不再列（审查 I）。"""
        for ref in unit.get("carried") or []:
            def change(record: dict, ref=ref) -> None:
                for item in record.get("suspensions") or []:
                    if item.get("id") == ref["id"] and item.get("state") == "deferred":
                        item.update(state="answered", consumed_by=run_id)

            store.update(project_id, ref["run_id"], change)

    async def _safe_point(self, project_id: str, run_id: str, unit: dict) -> str:
        """一个单元合入之后：EP-15；暂停、停在安全点、转为陪伴、在安全点暂停（维护）在这里生效。"""
        await self._account(project_id, run_id)
        run = store.get(project_id, run_id)
        flags = run.get("flags") or {}
        ending = self._ending(run)
        nxt = ending or ("paused" if flags.get("pause") or flags.get("safe_pause") else "continue")
        info = store.unit_info(run, unit["id"])
        _publish(project_id, "autobuild.safe_point", {
            "run_id": run_id, "slice_id": unit["id"], "slice_name": unit["name"],
            "version_label": info.get("version_label"),
            "next": nxt})
        return nxt

    async def _account(self, project_id: str, run_id: str) -> None:
        """记用量（只记不拦，F6）。"""
        run = store.get(project_id, run_id)
        if run is None:
            return
        events = await asyncio.to_thread(_manager().get_history, project_id, run["conversation_id"])

        def change(record: dict) -> None:
            budget.add_usage(project_id, record, list(events))
            record["budget"]["tick_at"] = time.time()

        run = store.update(project_id, run_id, change)
        self._updated(run)

    def _fail(self, project_id: str, run_id: str, reason: str) -> None:
        store.update(project_id, run_id, lambda record: record.update(failure={"reason": reason, "at": time.time()}))

    # ------------------------------------------------------------------
    # 停下：暂停、结束
    # ------------------------------------------------------------------

    async def _pause(self, project_id: str, run_id: str, *, reason: str | None = None) -> None:
        """暂停在当前位置；reason=quota 是配额用完（补足后续跑），其余是用户或维护要求的暂停。"""
        def change(record: dict) -> None:
            record["state"] = "paused"
            record["pause_reason"] = reason
            record.setdefault("flags", {}).pop("pause", None)
            record["flags"].pop("safe_pause", None)
            budget.stop_clock(record)

        run = store.update(project_id, run_id, change)
        _manager().set_run(project_id, None)
        hint.clear(project_id)
        self._updated(run)
        self._post_controls(run)
        self._refresh(project_id)
        if reason == "quota":
            self._notify(run, "autobuild_stopped")  # 人不在页面上也要知道运行停了、等着补配额
        await self.admit()

    async def _finish(self, project_id: str, run_id: str, state: str, *, reason: str | None = None) -> None:
        """运行结束。先落「收尾中」标记（state、原因），收尾的每一步各自兜住出错，最后一定落到结束态（推断：收尾出错
        卡在「在跑」）；停机后重启按标记接着收尾，已经做过的步骤不重做（审查 M）。"""
        run = store.get(project_id, run_id)
        if run is None or run.get("state") in store.ENDED:
            return
        marker = dict(run.get("finishing") or {})
        if marker:
            state, reason = marker.get("state") or state, marker.get("reason") or reason
        else:
            marker = {"state": state, "reason": reason, "at": time.time()}
            run = store.update(project_id, run_id, lambda record: record.update(finishing=marker)) or run
        ended_at = float(marker.get("at") or time.time())

        async def step(label: str, func, *args):
            try:
                return await asyncio.to_thread(func, *args)
            except Exception:  # noqa: BLE001 - 收尾的一步出错不挡落到结束态
                _log.exception("自主构建收尾「%s」出错（%s %s）", label, project_id, run_id)
                return None

        await step("执行记录", self._close_exec, project_id, (run.get("cursor") or {}).get("exec_id"))
        await step("变更集", self._settle_changeset, run)
        final_checks = await step("最终检查", self._final_checks, project_id, run)

        def change(record: dict) -> None:
            record["state"] = state
            if final_checks:
                record["final_checks"] = final_checks
            record["ended_at"] = ended_at
            budget.stop_clock(record, ended_at)
            if reason and not record.get("failure"):
                record["failure"] = {"reason": reason, "at": ended_at}
            for info in store.unit_infos(record).values():
                if info.get("state") == "running":
                    info["state"] = "partial" if info.get("commit") else "queued"
            record["report"] = views.report(record, ended_at)

        # 报告先进 git、卡片先进对话，最后才记结束：看到运行结束时，报告已经在版本里、对话里了
        final = json.loads(json.dumps(run))
        change(final)
        if not marker.get("report"):
            await step("报告", self._write_report, final)
            store.update(project_id, run_id, lambda record: record.setdefault("finishing", {}).update(report=True))
        if not marker.get("posted"):
            try:
                for unit in final.get("units") or []:
                    if unit["id"] in store.unit_infos(final):
                        self._unit_card(final, unit)
                self._post_card(final, views.report_card(final))
                self._post(final, {"type": "notice", "payload": {
                    "kind": "info", "text": f"运行 #{final['run_no']} {END_NOTICE.get(state, '已结束')}",
                    "version_label": None, "changeset_id": None, "align": "center"}})
                suggestions = await step("建议条", self._end_suggestions, final)
                if suggestions:
                    self._post(final, {"type": "suggestions", "payload": suggestions})
            except Exception:  # noqa: BLE001
                _log.exception("自主构建收尾时对话更新出错（%s %s）", project_id, run_id)
            store.update(project_id, run_id, lambda record: record.setdefault("finishing", {}).update(posted=True))

        def close(record: dict) -> None:
            change(record)
            record.pop("finishing", None)

        run = store.update(project_id, run_id, close) or run
        try:
            _manager().set_run(project_id, None)
            hint.clear(project_id)
            self._updated(run)
            _publish(project_id, "autobuild.finished", {"run_id": run_id, "run_no": run["run_no"], "state": state,
                                                        "report_available": True})
            self._notify(run, NOTIFY_KIND.get(state, "autobuild_stopped"))
            self._telemetry(run)
            self._refresh(project_id)
            await self._dispatch_rejudges(project_id, run_id)
        except Exception:  # noqa: BLE001
            _log.exception("自主构建结束后的通知出错（%s %s）", project_id, run_id)
        await self.admit()

    @staticmethod
    def _settle_changeset(run: dict) -> None:
        """停在一个单元中间（出错、停下）：已有的改动照样合入成一个版本、标需复核（F3：不再留给用户确认）；
        没改动就关掉；要删除已确认的内容时撤回；合入冲突进待处理。都不挡之后的对话。"""
        from ...kernel.changesets import service as changesets
        from ...services.workspace.errors import WorkspaceError

        ws = _ws()
        record = changesets.current(ws, run["project_id"])
        if record is None or record.get("run_id") != run["run_id"] or record.get("state") not in ("open", "pending_confirm"):
            return
        owner = run.get("owner") or run.get("actor") or "system"
        try:
            observed = changesets.observe(ws, run["project_id"], running=False, conversation_id=run["conversation_id"],
                                          checks=False)
            if observed is None:
                if (changesets.current(ws, run["project_id"]) or {}).get("id") == record["id"]:
                    changesets.discard(ws, run["project_id"], record["id"], actor=owner)
                return
            try:
                changesets.merge(ws, run["project_id"], record["id"], actor=owner, source="autobuild", needs_review=True,
                                 auto=True, message=f"autobuild: 自主构建运行 #{run.get('run_no')} 停下时的这一步（{record['id']}）")
            except WorkspaceError as exc:
                if exc.code == "delete_forbidden":
                    changesets.discard(ws, run["project_id"], record["id"], actor=owner)
                elif exc.code != "merge_conflict":
                    raise exc
        except Exception:  # noqa: BLE001 - 收不了尾时变更集留着，下一轮开始前、重启时再合入
            _log.warning("自主构建结束时整理变更集失败（%s）", run["project_id"], exc_info=True)

    def _final_checks(self, project_id: str, run: dict) -> dict | None:
        from ...kernel.changesets import gates, worktree
        from .. import mainline

        root = _ws().project_root(project_id)
        head = mainline.state(root).get("head")
        if not head or not run.get("base_commit") or head == run["base_commit"]:
            return None
        result = gates.compare(root, worktree.tree_of(root, run["base_commit"]), worktree.tree_of(root, head))
        return {"errors": result["errors"], "warnings": result["warnings"]}

    def _write_report(self, run: dict) -> None:
        """台账与运行报告进 git（`reports/autobuild/<运行号>/`，一次提交，来源 autobuild）。"""
        from ...kernel.writepath import ChangeTx, Origin

        from ...kernel.changesets import worktree

        ws = _ws()
        folder = f"{REPORT_DIR}/{run['run_no']}"
        root = ws.project_root(run["project_id"])
        if worktree.read_blob(root, "HEAD", f"{folder}/report.json") is not None:
            return  # 这次运行的报告已经提交过（收尾时停了机，审查 M）
        ledger = [views.ledger_entry(entry) for entry in run.get("ledger") or []]
        suspended = [views.suspension(item) for item in run.get("suspensions") or []]
        try:
            with ChangeTx(ws, run["project_id"], Origin("autobuild", run.get("owner") or run.get("actor") or ""),
                          summary=f"自主构建运行 #{run['run_no']} 报告") as tx:
                tx.write(f"{folder}/ledger.json", json.dumps({"ledger": ledger, "suspensions": suspended},
                                                             ensure_ascii=False, indent=2) + "\n")
                tx.write(f"{folder}/report.json", json.dumps(run.get("report") or views.report(run),
                                                             ensure_ascii=False, indent=2) + "\n")
                tx.commit(f"autobuild: 自主构建运行 #{run['run_no']} 报告")
        except Exception:  # noqa: BLE001 - 报告写不进 git 不影响运行结果，数据目录里有
            _log.exception("自主构建报告写入失败（%s %s）", run["project_id"], run["run_id"])

    def _end_suggestions(self, run: dict) -> dict:
        """运行结束后的建议条（详设 6.10）：主推「去复核」，其余按流程的下一步。"""
        from ..flow import view

        base = view.build(_ws().project_root(run["project_id"]),
                          context={"autobuild": False, "autobuild_paused": False})["suggestions"]
        review = views.report(run)["metrics"]["decisions"]["total"] or views.summary(run)["suspended"]
        if not review:
            return base
        primary = {"id": "autobuild_review", "label": "去复核", "icon": "inbox", "primary": True, "disabled": False,
                   "reason": "", "priority": 1,
                   "action": {"type": "navigate", "page": "inbox", "params": {"filter": "autobuild_review"}}}
        others = [item for item in [base.get("primary"), *(base.get("secondary") or [])] if item][:2]
        return {**base, "mode": "normal", "primary": primary,
                "secondary": [{**item, "primary": False} for item in others]}

    # ------------------------------------------------------------------
    # 控制（AB-06～AB-09）
    # ------------------------------------------------------------------

    async def control(self, project_id: str, run_id: str, op: str) -> dict:
        """AB-06～AB-09。同一项目的控制串行，持锁重读状态（推断：停止收尾时来了续跑）。"""
        async with self._project_lock(project_id):
            return await self._control(project_id, run_id, op)

    async def _control(self, project_id: str, run_id: str, op: str) -> dict:
        run = store.get(project_id, run_id)
        if run is None:
            raise ApiError("run_not_found", "运行不存在", 404)
        state = run["state"]
        if run.get("finishing"):
            raise ApiError("autobuild_not_running", "运行正在收尾", 409, {"state": state})
        orphan = state in store.RUNNING and not self.running(project_id)  # 在跑却没有驱动任务
        if op == "resume":
            if state not in ("paused", "interrupted"):
                raise ApiError("autobuild_not_running", "运行不在可以续跑的状态", 409, {"state": state})
            run = store.update(project_id, run_id,
                               lambda record: record.update(state="queued", resume_pending=False, pause_reason=None))
            self._post_controls(run)
            await self.admit()
            return run_view(store.get(project_id, run_id) or run)
        allowed = {"pause": ("running", "queued"), "stop": ("running", "queued", "pausing", "paused", "interrupted"),
                   "convert": ("running", "queued", "pausing", "paused", "interrupted")}[op]
        if state not in allowed and not (orphan and op != "pause"):
            raise ApiError("autobuild_not_running", "运行不在可以控制的状态", 409, {"state": state})
        idle = state in ("paused", "interrupted") or orphan
        not_started = state == "queued" and op != "pause"
        if not_started or (idle and not self._in_unit(run)):
            # 还没开始、或已经停在安全点：直接结束
            await self._finish(project_id, run_id, "stopped" if op == "stop" else "converted")
            return run_view(store.get(project_id, run_id))
        if state == "queued":
            run = store.update(project_id, run_id, lambda record: record.update(state="paused"))
            self._updated(run)
            self._post_controls(run)
            return run_view(run)

        def change(record: dict) -> None:
            flags = record.setdefault("flags", {})
            if op == "pause":
                flags["pause"] = True
                record["state"] = "pausing"
            else:
                flags.pop("pause", None)
                flags["stop" if op == "stop" else "convert"] = True
                record["state"] = "stopping" if op == "stop" else "converting"

        was_idle = idle
        run = store.update(project_id, run_id, change)
        self._updated(run)
        if was_idle:
            # 停在一个单元中间：接着把它做完，到安全点再停
            def queue(record: dict) -> None:
                record["resume_state"] = record["state"]
                record["state"] = "queued"

            store.update(project_id, run_id, queue)
            await self.admit()
        return run_view(store.get(project_id, run_id) or run)

    async def acknowledge(self, project_id: str, run_id: str) -> dict:
        """AB-19：看过结束的运行后确认——运行条收起；项目没有别的运行在进行时，构建方式回到陪伴建模（L2）。"""
        from .. import project_events

        async with self._project_lock(project_id):
            run = store.get(project_id, run_id)
            if run is None:
                raise ApiError("run_not_found", "运行不存在", 404)
            if run["state"] not in store.ENDED or run.get("finishing"):
                raise ApiError("autobuild_not_running", "运行还没结束", 409, {"state": run["state"]})
            if not run.get("acknowledged_at"):
                run = store.update(project_id, run_id, lambda record: record.update(acknowledged_at=time.time())) or run
            if store.active(project_id) is None:
                await asyncio.to_thread(_ws()._update_meta, project_id, build_mode="L2")
            # 不论改没改都发：各个标签页以服务端的构建方式为准
            project_events.project_changed(project_id, ["build_mode"])
            self._updated(run)
            return run_view(run)

    @staticmethod
    def _in_unit(run: dict) -> bool:
        return any(info.get("state") == "running" for info in store.unit_infos(run).values())

    def pause_all_at_safe_point(self) -> int:
        """进入维护模式（O-21 `autobuild=pause_at_safe_point`）：在跑的运行做完当前单元就暂停。"""
        runs = store.all_runs(store.RUNNING)
        for run in runs:
            store.update(run["project_id"], run["run_id"],
                         lambda record: record.setdefault("flags", {}).update(safe_pause=True))
        return len(runs)

    # ------------------------------------------------------------------
    # 运行中的补充说明、自动决定
    # ------------------------------------------------------------------

    def note(self, project_id: str, run: dict, text: str, meta: dict) -> dict:
        """运行对话里用户发的消息（契约 3.22「补充说明不打断运行」）：记下来，下一步开始前带给助手。"""
        record = {
            "type": "user_message", "text": meta.get("display_text") or text, "message_id": f"m-{uuid.uuid4().hex[:8]}",
            "attachments": meta.get("attachments") or [], "references": meta.get("references") or [],
            "origin": None, "delivery": "deferred", "prompt": text,
        }
        posted = _manager().append_record(project_id, run["conversation_id"], record)
        store.update(project_id, run["run_id"], lambda item: item.setdefault("notes", []).append(
            {"text": text, "at": time.time(), "read": False}))
        return posted

    async def on_decisions(self, project_id: str, run_id: str, entries: list, suspensions: list) -> None:
        """策略刚答了一组问题：单元的进展块更新，挂起的进待处理。"""
        from ...governance import telemetry

        run = store.get(project_id, run_id)
        if run is None:
            return
        unit = next((item for item in run.get("units") or [] if item["id"] == (run.get("cursor") or {}).get("unit")), None)
        if unit is not None:
            self._unit_card(run, unit)
        self._updated(run)
        if entries:
            telemetry.count("onto_autobuild_decisions_total", len(entries), project=project_id)
        if suspensions:
            self._suspended(run, len(suspensions))

    def _suspended(self, run: dict, count: int) -> None:
        from .. import project_events

        project_events.schedule_flow_refresh(run["project_id"])
        self._notify(run, "autobuild_suspension", count=count)

    # ------------------------------------------------------------------
    # 对话、项目流、用户流
    # ------------------------------------------------------------------

    def _post(self, run: dict, record: dict) -> None:
        try:
            _manager().append_record(run["project_id"], run["conversation_id"], record)
        except Exception:  # noqa: BLE001 - 对话删了：卡片不更新
            _log.warning("运行对话追加失败（%s）", run["project_id"], exc_info=True)

    def _post_controls(self, run: dict) -> None:
        """暂停、续跑之后运行对话的建议条（EC-53）跟着换：暂停时只有「运行详情」，在跑时是三个控制（与 FL-02 同一份）。"""
        from ..flow import view

        context = {"autobuild_paused": True} if run["state"] in ("paused", "interrupted") else {"autobuild": True}
        try:
            suggestions = view.build(_ws().project_root(run["project_id"]), context=context)["suggestions"]
        except Exception:  # noqa: BLE001 - 建议条算不出不影响运行
            _log.warning("运行对话的建议条没算出来（%s）", run["project_id"], exc_info=True)
            return
        self._post(run, {"type": "suggestions", "payload": suggestions})

    def _post_card(self, run: dict, card: dict) -> None:
        posted = card["card_id"] in (run.get("cards") or [])
        if not posted:
            store.update(run["project_id"], run["run_id"], lambda record: record.setdefault("cards", []).append(card["card_id"]))
            run.setdefault("cards", []).append(card["card_id"])
        self._post(run, {"type": "card_updated" if posted else "card", "payload": card})

    def _unit_card(self, run: dict | None, unit: dict) -> None:
        if run is not None:
            self._post_card(run, views.unit_card(run, unit))

    def _updated(self, run: dict | None) -> None:
        if run is not None:
            _publish(run["project_id"], "autobuild.run.updated", views.summary(run))

    def _refresh(self, project_id: str) -> None:
        from .. import project_events

        project_events.schedule_flow_refresh(project_id)
        project_events.card_changed(project_id)

    def _notify(self, run: dict, kind: str, *, count: int = 0) -> None:
        """进通知中心（A-13 落盘）并推用户流 EU-01。新增挂起按这次运行成组：没读的旧一条被替换，正文是还没回答的总数。"""
        from .. import notifications

        owner = run.get("owner") or run.get("actor") or ""
        summary = views.summary(run)
        title, body = {
            "autobuild_finished": ("自主构建完成", f"需复核 {summary['needs_review']} 项"),
            "autobuild_suspension": ("新增挂起问题", f"{summary.get('suspended') or count} 个"),
        }.get(kind, ("自主构建已暂停", "配额用完，补足后点续跑") if summary.get("pause_reason") == "quota"
              else ("自主构建已停下", END_NOTICE.get(run["state"], "")))
        action = ({"label": "去复核", "page": "inbox", "params": {"filter": "autobuild_review"}}
                  if kind != "autobuild_stopped" else
                  {"label": "查看", "page": "l4-run", "params": {"c": run["conversation_id"]}})
        group = f"{run['project_id']}:{run['run_id']}" if kind == "autobuild_suspension" else None
        notifications.notify(owner, kind, title, body, project_id=run["project_id"], action=action, group=group)

    def _telemetry(self, run: dict) -> None:
        """可观测（详设 8.5）：运行次数、时长、费用、自动决定数；复核被改掉的比例在复核时记。"""
        from ...governance import telemetry

        labels = {"project": run["project_id"]}
        telemetry.count("onto_autobuild_runs_total", state=run["state"], **labels)
        telemetry.observe("onto_autobuild_run_seconds", budget.elapsed_ms(run) / 1000, **labels)
        telemetry.count("onto_autobuild_cost_total", float((run.get("budget") or {}).get("cost") or 0),
                        currency=views.currency_of(run), **labels)

    # ------------------------------------------------------------------
    # 改判排队（AB-12 在运行中）
    # ------------------------------------------------------------------

    async def _dispatch_rejudges(self, project_id: str, run_id: str) -> None:
        from ..session_host import command_tasks
        from . import review

        run = store.get(project_id, run_id)
        queued = list((run or {}).get("pending_rejudges") or [])
        if not queued:
            return
        store.update(project_id, run_id, lambda record: record.update(pending_rejudges=[]))
        for item in queued:
            try:
                if item.get("suspension_id"):
                    await review.send_answer(project_id, item.get("run_id") or run_id, item["suspension_id"],
                                             task_id=item.get("task_id"))
                else:
                    await review.send_rejudge(project_id, item.get("run_id") or run_id, item["entry_id"],
                                              task_id=item.get("task_id"))
            except ApiError as exc:
                if exc.code != "slice_reverted":
                    _log.exception("改判发不出去（%s %s）", project_id, item.get("entry_id"))
                # 排队期间这一步撤回了：不再发，任务记为取消（审查 G）
                command_tasks.update(project_id, item.get("task_id") or "", state="cancelled",
                                     result={"summary": "这一步已经撤回", "refs": []})
            except Exception:  # noqa: BLE001 - 一条改判发不出去不挡别的
                _log.exception("改判发不出去（%s %s）", project_id, item.get("entry_id"))

    # ------------------------------------------------------------------
    # 重启、停机、删除项目
    # ------------------------------------------------------------------

    async def resume_after_restart(self) -> None:
        """启动后：启动恢复改判中断、标了自动续跑的运行接着跑（AB-05 recovered_from_checkpoint 加一）。"""
        for run in store.all_runs(("interrupted",)):
            if not run.get("resume_pending"):
                continue

            def change(record: dict) -> None:
                record["resume_pending"] = False
                record["recovered"] = int(record.get("recovered") or 0) + 1
                record["state"] = "queued"

            store.update(run["project_id"], run["run_id"], change)
        await self.admit()

    async def shutdown(self) -> None:
        """停机：运行任务取消，运行记录停在检查点（不改状态，启动恢复接着跑）。"""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def forget(self, project_id: str) -> None:
        """删除项目：取消运行任务、清掉运行记录。"""
        task = self._tasks.pop(project_id, None)
        if task is not None and not task.done():
            task.cancel()
        store.purge(project_id)


runner = AutobuildRunner()


def _restore_state(record: dict) -> str:
    """暂停中（pausing）重启：直接算暂停；停在安全点、转为陪伴的接着把当前单元做完。"""
    return {"pausing": "paused"}.get(record["state"], record["state"])


def startup_sweep(manager) -> dict:
    """启动恢复（order 105，在执行记录改判之后）：停机时还在跑的运行改判中断，启动后自动续跑。"""
    changed = []
    for run in store.all_runs(store.RUNNING):
        def change(record: dict) -> None:
            tick = (record.get("budget") or {}).get("tick_at")
            budget.stop_clock(record, tick if tick else None)
            previous = _restore_state(record)
            if previous == "paused":
                record["state"] = "paused"
                record.setdefault("flags", {}).pop("pause", None)  # 和 _pause 一样（审查 E）
                record["flags"].pop("safe_pause", None)
                return
            if previous in ("stopping", "converting"):
                record["resume_state"] = previous
            record["state"] = "interrupted"
            record["resume_pending"] = True

        updated = store.update(run["project_id"], run["run_id"], change)
        if updated is not None:
            changed.append(updated)
    reverted = _repair_reverts()
    if not changed and not reverted:
        return None  # 没有停机时在跑的运行：不进报告（同 agent 提交钩子、助手副本）
    per_project: dict[str, int] = dict(reverted)
    for run in changed:
        per_project[run["project_id"]] = per_project.get(run["project_id"], 0) + 1
    resumed = sum(1 for run in changed if run.get("resume_pending"))
    detail = f"{len(changed)} 次运行停在检查点，{resumed} 次启动后自动续跑" if changed else ""
    if reverted:
        detail = "，".join(filter(None, [detail, f"补完 {sum(reverted.values())} 次撤回"]))
    return {"id": "autobuild_runs", "title": "自主构建", "result": "cleared", "tone": "fix", "detail": detail,
            "projects": [{"project_id": pid, "count": count, "paths": []} for pid, count in sorted(per_project.items())]}


def _repair_reverts() -> dict[str, int]:
    """撤回到一半停了机的单元（审查推断）：{项目: 补完的单元数}；一个项目出错不挡别的。"""
    from . import review

    out = {}
    for project_id in sorted({run["project_id"] for run in store.all_runs(store.ENDED)}):
        try:
            fixed = review.repair_reverts(project_id)
        except Exception:  # noqa: BLE001
            _log.exception("补完撤回失败（%s）", project_id)
            continue
        if fixed:
            out[project_id] = fixed
    return out


def register_steps(register) -> None:
    register("autobuild_runs", "自主构建", startup_sweep, order=105)
