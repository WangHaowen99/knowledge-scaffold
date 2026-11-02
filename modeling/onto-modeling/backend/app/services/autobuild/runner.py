"""自主构建运行器（方案 B12、B13；详设 8.5、8.4；契约 AB-03～AB-09、7.15、EP-14～EP-17）。

按蓝图逐片执行，每片：

1. 新开上下文（关掉这条对话的模型上下文、历史里记一个分界），长上下文自动压缩不会丢掉台账；
2. 先占一个变更集（`source=autobuild`、`build_mode=L4`），注入片说明与台账摘要，以项目所有者的身份发这一片的
   每一步命令（`/ontology-extract-object slice=<片>` 等），按条件等这一轮出 result；
3. 查执行记录：做完了就下一步；没做完也没挂起就说「继续」，最多 `CONTINUE_LIMIT` 次；出错按执行记录续跑；
4. 自动合入（`changesets.merge(source="autobuild", needs_review=True)`）；门禁不过让助手修，最多策略里的轮数，
   还不过就挂起这一片、撤回这一片的修改；合入冲突停下（冲突进待处理）；
5. 合入之后是安全点：停在安全点、转为陪伴、预算用完在这里结束；暂停在一步做完之后生效。

切片之后主线上的几步（校验、走查、全局一致性）各算一个单元，同样一步一个变更集、自动合入。运行状态与台账在
数据目录（`store`），结束时台账与运行报告写进工作区 `reports/autobuild/<运行号>/`（进 git）。运行在自己的
对话里（「自主构建 · 运行 #n」），进展、自动决定、挂起都显示成切片进展块。

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
from . import budget, hint, limits, plan, store, views

_log = logging.getLogger(__name__)

PREVIEW_TTL_S = 600
CONTINUE_LIMIT = 3
ERROR_LIMIT = 2
WAIT_TICK_S = 5.0
REPORT_DIR = "reports/autobuild"
END_NOTICE = {"completed": "已完成", "stopped": "已在安全点停下", "converted": "已转为陪伴",
              "budget_exhausted": "预算用完，已在安全点停下", "failed": "出错停下"}
NOTIFY_KIND = {"completed": "autobuild_finished", "converted": "autobuild_finished"}


class _Stop(Exception):
    """一步发不出去：预算（配额）用完或会话出错。"""

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


def _stage_ids(project_id: str) -> list[str]:
    root = _ws().project_root(project_id)
    node = plan.per_slice_node(plan.flow(root)) or {}
    from ..flow import slices

    return [stage["id"] for stage in slices.stage_defs(node)] if node else []


def run_view(run: dict) -> dict:
    return views.run_view(run, _stage_ids(run["project_id"]))


class AutobuildRunner:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._previews: dict[str, dict] = {}
        self._admit_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # 开始前预览（AB-03）与开始（AB-04）
    # ------------------------------------------------------------------

    def preview(self, project_id: str, policy_doc: dict) -> dict:
        """AutobuildPreview；`policy_doc` 已校验过。在线程里调。"""
        from ...kernel.changesets import store as changeset_store
        from ..flow import blueprint

        ws = _ws()
        running = store.active(project_id)
        if running is not None:
            raise ApiError("autobuild_running", "这个项目有一次自主构建还没结束", 409, {"run_id": running["run_id"]})
        doc = blueprint.effective_doc(ws, project_id)
        if doc is None or doc.get("state") != "confirmed":
            raise ApiError("blueprint_not_confirmed", "建模蓝图还没确认", 409)
        units, rows = plan.build(ws, project_id, policy_doc)
        if not units:
            raise ApiError("invalid_argument", "范围里没有要做的切片", 400, {"fields": {"scope": "empty"}})
        warnings = []
        pending = changeset_store.current(ws.project_root(project_id))
        if pending is not None and pending.get("state") in ("open", "pending_confirm", "conflict"):
            warnings.append("这一轮的修改还没确认生效或撤回")
        now = time.time()
        for token, item in list(self._previews.items()):
            if item["expires"] < now:
                self._previews.pop(token, None)
        token = f"pv-{secrets.token_urlsafe(12)}"
        self._previews[token] = {"project_id": project_id, "policy": policy_doc, "expires": now + PREVIEW_TTL_S}
        slots = limits.max_runs()
        busy = len(store.all_runs(store.RUNNING))
        queued = not slots or busy >= slots
        from . import policy as policy_module

        return {
            "preview_token": token, "expires_at": views.iso(now + PREVIEW_TTL_S),
            "slices": rows, "budget": policy_doc["budget"],
            "below_threshold_text": policy_module.BELOW_TEXT[policy_doc["advanced"]["below_threshold"]],
            "warnings": warnings, "queued": queued,
            "queue_position": (len(store.all_runs(("queued",))) + 1) if queued else None,
        }

    async def start(self, project_id: str, token: str, *, actor: str, owner: str) -> dict:
        """AB-04：开一条「自主构建 · 运行 #n」对话，记下运行，轮到时开始。"""
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
        live = _manager().live_status(project_id)
        if live and live["busy"]:
            raise ApiError("session_busy", "有对话正在运行，请等它结束或先打断", 409,
                           {"running_conversation_id": live["conversation_id"]})
        ws = _ws()
        root = ws.project_root(project_id)
        pending = changeset_store.current(root)
        if pending is not None and pending.get("state") in ("open", "pending_confirm", "conflict"):
            raise ApiError("changeset_in_progress", "这一轮的修改还没确认生效或撤回", 409,
                           {"changeset_id": pending["id"], "conversation_id": pending.get("conversation_id")})
        await asyncio.to_thread(quota.check_before_turn, project_id, owner, "tokens")
        self._previews.pop(token, None)
        policy_doc = item["policy"]
        units, rows = await asyncio.to_thread(plan.build, ws, project_id, policy_doc)
        if not units:
            raise ApiError("invalid_argument", "范围里没有要做的切片", 400, {"fields": {"scope": "empty"}})
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
            "units": units, "before_rows": [{"id": row["id"], "name": row["name"], "group": row.get("group"),
                                             "stages": row.get("stages") or {}}
                                            for row in await asyncio.to_thread(plan.slice_rows, ws, project_id)],
            "slices": {}, "cursor": {}, "flags": {}, "budget": budget.empty(), "ledger": [], "suspensions": [],
            "notes": [], "cards": [], "recovered": 0, "resume_pending": False,
            "base_commit": state.get("head"), "base_version_label": state.get("version_label"),
            "failure": None, "report": None, "pending_rejudges": [],
        }
        await asyncio.to_thread(store.save, record)
        self._post_card(record, views.start_card(record))
        project_events.conversation_changed(project_id, conversation["id"])
        _publish(project_id, "autobuild.run.updated", views.summary(record))
        await self.admit()
        return run_view(store.get(project_id, run_id) or record)

    # ------------------------------------------------------------------
    # 名额与驱动
    # ------------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        if self._admit_lock is None:
            self._admit_lock = asyncio.Lock()
        return self._admit_lock

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
                if not slots or busy >= slots:
                    break
                if run["project_id"] in self._tasks:
                    continue
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
            self._updated(store.get(project_id, run_id))
            while True:
                run = store.get(project_id, run_id)
                if run is None or run["state"] not in store.RUNNING:
                    return
                unit = self._next_unit(run)
                fresh = unit is not None and (run.get("slices") or {}).get(unit["id"], {}).get("state") in (None, "queued")
                ending = self._ending(run)
                if unit is None or (fresh and ending):
                    # 做完了；或者已经在安全点（这一片还没开始）又要停
                    await self._finish(project_id, run_id, ending or "completed")
                    return
                if fresh and run.get("flags", {}).get("pause"):
                    await self._pause(project_id, run_id)
                    return
                outcome = await self._run_unit(project_id, run_id, unit)
                if outcome == "paused":
                    await self._pause(project_id, run_id)
                    return
                if outcome in ("failed", "budget"):
                    await self._finish(project_id, run_id, "budget_exhausted" if outcome == "budget" else "failed")
                    return
                nxt = await self._safe_point(project_id, run_id, unit)
                if nxt == "paused":
                    await self._pause(project_id, run_id)
                    return
                if nxt != "continue":
                    await self._finish(project_id, run_id, nxt)
                    return
        except asyncio.CancelledError:
            raise  # 停机：运行记录停在检查点，启动恢复接着跑
        except Exception:  # noqa: BLE001
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
            if (run.get("slices") or {}).get(unit["id"], {}).get("state") not in ("done", "partial", "skipped"):
                return unit
        return None

    @staticmethod
    def _ending(run: dict) -> str | None:
        flags = run.get("flags") or {}
        if flags.get("budget"):
            return "budget_exhausted"
        if flags.get("stop"):
            return "stopped"
        if flags.get("convert"):
            return "converted"
        return None

    # ------------------------------------------------------------------
    # 一片
    # ------------------------------------------------------------------

    async def _run_unit(self, project_id: str, run_id: str, unit: dict) -> str:
        from ...kernel.changesets import service as changesets

        run = store.get(project_id, run_id)
        fresh = (run.get("slices") or {}).get(unit["id"], {}).get("state") in (None, "queued")
        if fresh:
            def begin(record: dict) -> None:
                info = record.setdefault("slices", {}).setdefault(unit["id"], {})
                info.update(state="running", started_at=time.time(), steps={step["id"]: "queued" for step in unit["steps"]},
                            decisions=info.get("decisions") or [], suspensions=info.get("suspensions") or [],
                            timeline=info.get("timeline") or [], fix_rounds=0, produced={}, checks={})
                record["cursor"] = {"unit": unit["id"], "step": None, "exec_id": None, "continues": 0}

            run = store.update(project_id, run_id, begin)
            await self._new_context(run, unit)
        self._slice_card(run, unit)
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
        store.update(project_id, run_id, lambda record: record["slices"][unit["id"]].update(changeset_id=slot["id"]))
        for step in unit["steps"]:
            run = store.get(project_id, run_id)
            if run["slices"][unit["id"]]["steps"].get(step["id"]) in ("done", "partial", "skipped"):
                continue
            try:
                outcome = await self._run_step(project_id, run_id, unit, step)
            except _Stop as stop:
                self._fail(project_id, run_id, stop.reason or stop.outcome)
                return stop.outcome
            if outcome == "failed":
                return "failed"
            run = store.get(project_id, run_id)
            last = step is unit["steps"][-1]
            if not last and (run.get("flags") or {}).get("pause"):
                return "paused"
        try:
            return await self._merge(project_id, run_id, unit)
        except _Stop as stop:
            self._fail(project_id, run_id, stop.reason or stop.outcome)
            return stop.outcome

    async def _new_context(self, run: dict, unit: dict) -> None:
        """每片新开上下文（方案 B12 第 1 步）：关掉模型上下文、历史里记分界（Jiuwen 回放从这里起）。"""
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
        from ..flow import blueprint

        brief = ""
        if unit["kind"] == "slice":
            try:
                doc = blueprint.effective_doc(_ws(), run["project_id"])
                module = blueprint.script(_ws().project_root(run["project_id"]))
                brief = module.brief(doc, unit["id"]) if doc is not None and unit["id"] != "all" else ""
            except Exception:  # noqa: BLE001 - 片说明是增强信息
                brief = ""
        notes = [item["text"] for item in run.get("notes") or [] if not item.get("read")]
        hint.write(run["project_id"], run["conversation_id"], hint.compose(run, unit, brief=brief, notes=notes))
        if notes:
            def mark(record: dict) -> None:
                for item in record.get("notes") or []:
                    item["read"] = True

            run = store.update(run["project_id"], run["run_id"], mark) or run
        return run

    async def _send(self, project_id: str, run_id: str, unit: dict, text: str, *, resume_exec: str | None = None) -> str | None:
        """以项目所有者的身份发一步（不进对话流：进展在切片进展块里）；返回这一步的执行记录编号。"""
        from ...governance.quota import QuotaExceeded
        from ..agent_sessions import SessionError

        run = store.get(project_id, run_id)
        run = await asyncio.to_thread(self._write_hint, run, unit)
        manager = _manager()
        meta = {"display_text": text, "origin": None, "hidden": True}
        if resume_exec:
            meta["resume_exec"] = resume_exec
        try:
            session = await manager.send(project_id, _ws().project_root(project_id), text, run["conversation_id"],
                                         username=run.get("owner") or "", meta=meta)
        except QuotaExceeded as exc:
            raise _Stop("budget", "quota") from exc
        except SessionError as exc:
            raise _Stop("failed", f"session:{exc.status}") from exc
        return session.exec_id

    async def _wait_turn(self, project_id: str, run_id: str, conversation_id: str) -> str:
        """按条件等这一轮结束：idle、error、gone（会话被回收或换了对话）。等的过程中按墙钟看预算。"""
        manager = _manager()
        while True:
            event = manager.signal(project_id)
            event.clear()
            session = manager.get(project_id)
            if session is None or session.conversation_id != conversation_id or session.state == "closed":
                return "gone"
            if session.state == "error":
                return "error"
            busy = (session.sending or session.finishing or session.pending_prompts or session.is_connecting()
                    or manager.queue_position(project_id) is not None or session.state in ("running", "queued")
                    or session.turns_in_flight > 0 or session.deferred)
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
            info = record["slices"][unit["id"]]
            info["steps"][step["id"]] = "running"
            info["current_step"] = step["id"]
            if not any(item.get("type") == "step" and item.get("id") == step["id"] for item in info["timeline"]):
                info["timeline"].append({"type": "step", "id": step["id"], "text": step["action"], "meta": "",
                                         "state": "running", "started_at": started, "duration_ms": None})
            record["cursor"] = {"unit": unit["id"], "step": step["id"], "exec_id": exec_id, "continues": continues}

        run = store.update(project_id, run_id, mark_running)
        self._slice_card(run, unit)
        self._updated(run)
        suspended_before = len(run["slices"][unit["id"]].get("suspensions") or [])
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
            suspended = len(run["slices"][unit["id"]].get("suspensions") or []) > suspended_before
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

        started = float(((run.get("slices") or {}).get(unit["id"]) or {}).get("started_at") or 0)
        for record in exec_records.query(project_id, conversation_id=run["conversation_id"]):
            if (record.get("run_id") == run["run_id"] and record.get("command") == step["command"]
                    and float(record.get("started_at") or 0) >= started
                    and ((record.get("slice") or {}).get("id") in (unit["id"], None) or unit["kind"] == "node")):
                return record["id"]
        return None

    def _step_done(self, project_id: str, run_id: str, unit: dict, step: dict, state: str) -> str:
        now = time.time()

        def mark(record: dict) -> None:
            info = record["slices"][unit["id"]]
            info["steps"][step["id"]] = state
            info["current_step"] = None
            for item in info["timeline"]:
                if item.get("type") == "step" and item.get("id") == step["id"]:
                    item["state"] = "done"
                    item["duration_ms"] = int((now - float(item.get("started_at") or now)) * 1000)
                    item["meta"] = f"{max(1, round(item['duration_ms'] / 60000))} 分"
            record["cursor"] = {"unit": unit["id"], "step": None, "exec_id": None, "continues": 0}

        run = store.update(project_id, run_id, mark)
        self._slice_card(run, unit)
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
        """自动合入这一片；门禁不过让助手修（最多策略里的轮数）。返回 merged、failed。"""
        from ...kernel.changesets import service as changesets
        from ...services.workspace.errors import WorkspaceError
        from ..session_host import exec_records

        ws = _ws()
        run = store.get(project_id, run_id)
        info = run["slices"][unit["id"]]
        changeset_id = info.get("changeset_id")
        owner = run.get("owner") or run.get("actor") or ""
        records = exec_records.query(project_id, conversation_id=run["conversation_id"])
        last_exec = records[0] if records else None
        # 这一片最后的样子再看一次（带检查）：一轮收尾时会话宿主已经看过，补上之后在副本里的改动
        await asyncio.to_thread(changesets.observe, ws, project_id, running=False,
                                conversation_id=run["conversation_id"], exec_record=last_exec, checks=True)
        current = changesets.current(ws, project_id)
        if current is None or current["id"] != changeset_id or not current.get("tree"):
            # 这一片没改出东西：先占的变更集关掉，版本不变
            if current is not None and current["id"] == changeset_id:
                await asyncio.to_thread(changesets.discard, ws, project_id, changeset_id, actor=owner)
            return self._unit_done(project_id, run_id, unit, record=None)
        limit = int(((run.get("policy") or {}).get("advanced") or {}).get("max_fix_rounds") or 0)
        while True:
            try:
                result = await asyncio.to_thread(
                    changesets.merge, ws, project_id, changeset_id, actor=owner, source="autobuild", needs_review=True,
                    message=f"autobuild: 自主构建运行 #{run['run_no']}「{unit['name']}」（{changeset_id}）")
                break
            except WorkspaceError as exc:
                if exc.code == "gate_failed":
                    rounds = int(store.get(project_id, run_id)["slices"][unit["id"]].get("fix_rounds") or 0)
                    if rounds < limit:
                        store.update(project_id, run_id,
                                     lambda record: record["slices"][unit["id"]].update(fix_rounds=rounds + 1))
                        await self._fix(project_id, run_id, unit, (exc.details or {}).get("findings") or [])
                        await asyncio.to_thread(changesets.observe, ws, project_id, running=False,
                                                conversation_id=run["conversation_id"], checks=True)
                        continue
                    return await self._give_up(project_id, run_id, unit, changeset_id, owner,
                                               (exc.details or {}).get("findings") or [])
                if exc.code == "changeset_closed":
                    return self._unit_done(project_id, run_id, unit, record=None)
                self._fail(project_id, run_id, exc.code or "merge")
                return "failed"
        return self._unit_done(project_id, run_id, unit, record=result["record"], merged=result)

    async def _fix(self, project_id: str, run_id: str, unit: dict, findings: list[dict]) -> None:
        lines = [f"- {item.get('message') or item.get('code')}（{item.get('path') or ''}）" for item in findings[:10]]
        text = (f"确认生效前的检查发现 {len(findings)} 个新错误。只改「{unit['name']}」这一片的内容，把它们修好：\n"
                + "\n".join(lines))
        run = store.get(project_id, run_id)
        await self._send(project_id, run_id, unit, text)
        await self._wait_turn(project_id, run_id, run["conversation_id"])
        await self._account(project_id, run_id)

    async def _give_up(self, project_id: str, run_id: str, unit: dict, changeset_id: str, owner: str,
                       findings: list[dict]) -> str:
        """修了几轮门禁还不过：这一片的修改撤回，挂起一个问题（进待处理），接着做下一片。"""
        from ...kernel.changesets import service as changesets

        await asyncio.to_thread(changesets.discard, _ws(), project_id, changeset_id, actor=owner)
        reason = "；".join(str(item.get("message") or item.get("code") or "") for item in findings[:3])

        def change(record: dict) -> None:
            item = {"id": f"sp-{uuid.uuid4().hex[:8]}", "run_id": run_id, "at": time.time(),
                    "slice": {"id": unit["id"], "name": unit["name"]}, "title": f"「{unit['name']}」检查没通过",
                    "reason": reason, "handled_as": "这一片没生效，先做下一片", "kind": "question", "options": [],
                    "allow_custom": True, "actions": [], "state": "open", "step": None, "exec_id": None,
                    "gate": True}
            record.setdefault("suspensions", []).append(item)
            info = record["slices"][unit["id"]]
            info.setdefault("suspensions", []).append(item["id"])
            info.setdefault("timeline", []).append({"type": "suspension", "id": item["id"]})
            info.update(state="skipped", gate_failed=True, ended_at=time.time())

        run = store.update(project_id, run_id, change)
        self._slice_card(run, unit)
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
            info = run["slices"][unit["id"]]
            partial = any(value == "partial" for value in (info.get("steps") or {}).values())
            info.update(state="partial" if partial else "done", ended_at=time.time(), produced=produced,
                        commit=(merged or {}).get("commit"),
                        version_label=(merged or {}).get("version_label") or state.get("version_label"),
                        checks={"errors": int(checks.get("errors") or 0), "warnings": int(checks.get("warnings") or 0)})

        run = store.update(project_id, run_id, mark)
        self._slice_card(run, unit)
        self._updated(run)
        return "merged"

    async def _safe_point(self, project_id: str, run_id: str, unit: dict) -> str:
        """一片合入之后：EP-15；停在安全点、转为陪伴、预算用完、在安全点暂停（维护）在这里生效。"""
        await self._account(project_id, run_id)
        run = store.get(project_id, run_id)
        flags = run.get("flags") or {}
        ending = self._ending(run)
        nxt = ending or ("paused" if flags.get("pause") or flags.get("safe_pause") else "continue")
        info = run["slices"].get(unit["id"]) or {}
        _publish(project_id, "autobuild.safe_point", {
            "run_id": run_id, "slice_id": unit["id"], "slice_name": unit["name"],
            "version_label": info.get("version_label"),
            "next": {"budget_exhausted": "stopped"}.get(nxt, nxt)})
        return nxt

    async def _account(self, project_id: str, run_id: str) -> None:
        """记用量、看预算：80% 提醒一次（EP-16），100% 改为停在安全点。"""
        run = store.get(project_id, run_id)
        if run is None:
            return
        events = await asyncio.to_thread(_manager().get_history, project_id, run["conversation_id"])
        result = {}

        def change(record: dict) -> None:
            budget.add_usage(project_id, record, list(events))
            record["budget"]["tick_at"] = time.time()
            fresh, exhausted = budget.check(record)
            result.update(fresh=fresh, exhausted=exhausted)
            if exhausted and record.get("state") in store.RUNNING:
                record.setdefault("flags", {})["budget"] = exhausted
                if record["state"] == "running":
                    record["state"] = "stopping"

        run = store.update(project_id, run_id, change)
        for dimension, pct in result.get("fresh") or []:
            _publish(project_id, "autobuild.budget_warning", {"run_id": run_id, "dimension": dimension, "used_pct": pct})
        self._updated(run)

    def _fail(self, project_id: str, run_id: str, reason: str) -> None:
        store.update(project_id, run_id, lambda record: record.update(failure={"reason": reason, "at": time.time()}))

    # ------------------------------------------------------------------
    # 停下：暂停、结束
    # ------------------------------------------------------------------

    async def _pause(self, project_id: str, run_id: str) -> None:
        def change(record: dict) -> None:
            record["state"] = "paused"
            record.setdefault("flags", {}).pop("pause", None)
            record["flags"].pop("safe_pause", None)
            budget.stop_clock(record)

        run = store.update(project_id, run_id, change)
        _manager().set_run(project_id, None)
        hint.clear(project_id)
        self._updated(run)
        self._refresh(project_id)
        await self.admit()

    async def _finish(self, project_id: str, run_id: str, state: str, *, reason: str | None = None) -> None:
        run = store.get(project_id, run_id)
        if run is None:
            return
        cursor = run.get("cursor") or {}
        await asyncio.to_thread(self._close_exec, project_id, cursor.get("exec_id"))

        ended_at = time.time()

        def change(record: dict) -> None:
            record["state"] = state
            record["ended_at"] = ended_at
            budget.stop_clock(record, ended_at)
            if reason and not record.get("failure"):
                record["failure"] = {"reason": reason, "at": ended_at}
            for info in (record.get("slices") or {}).values():
                if info.get("state") == "running":
                    info["state"] = "partial" if info.get("commit") else "queued"
            record["report"] = views.report(record, ended_at)

        # 报告先进 git、卡片先进对话，最后才记结束：看到运行结束时，报告已经在版本里、对话里了
        final = json.loads(json.dumps(run))
        change(final)
        await asyncio.to_thread(self._write_report, final)
        for unit in final.get("units") or []:
            if unit["id"] in (final.get("slices") or {}):
                self._slice_card(final, unit)
        self._post_card(final, views.report_card(final))
        self._post(final, {"type": "notice", "payload": {
            "kind": "info", "text": f"运行 #{final['run_no']} {END_NOTICE.get(state, '已结束')}",
            "version_label": None, "changeset_id": None, "align": "center"}})
        self._post(final, {"type": "suggestions", "payload": await asyncio.to_thread(self._end_suggestions, final)})
        run = store.update(project_id, run_id, change)
        _manager().set_run(project_id, None)
        hint.clear(project_id)
        self._updated(run)
        _publish(project_id, "autobuild.finished", {"run_id": run_id, "run_no": run["run_no"], "state": state,
                                                    "report_available": True})
        self._notify(run, NOTIFY_KIND.get(state, "autobuild_stopped"))
        self._telemetry(run)
        self._refresh(project_id)
        await self._dispatch_rejudges(project_id, run_id)
        await self.admit()

    def _write_report(self, run: dict) -> None:
        """台账与运行报告进 git（`reports/autobuild/<运行号>/`，一次提交，来源 autobuild）。"""
        from ...kernel.writepath import ChangeTx, Origin

        ws = _ws()
        folder = f"{REPORT_DIR}/{run['run_no']}"
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
        run = store.get(project_id, run_id)
        if run is None:
            raise ApiError("run_not_found", "运行不存在", 404)
        state = run["state"]
        if op == "resume":
            if state not in ("paused", "interrupted"):
                raise ApiError("autobuild_not_running", "运行不在可以续跑的状态", 409, {"state": state})
            run = store.update(project_id, run_id, lambda record: record.update(state="queued", resume_pending=False))
            await self.admit()
            return run_view(store.get(project_id, run_id) or run)
        allowed = {"pause": ("running", "queued"), "stop": ("running", "queued", "pausing", "paused", "interrupted"),
                   "convert": ("running", "queued", "pausing", "paused", "interrupted")}[op]
        if state not in allowed:
            raise ApiError("autobuild_not_running", "运行不在可以控制的状态", 409, {"state": state})
        if state == "queued" and op != "pause" or (state in ("paused", "interrupted") and not self._in_slice(run)):
            # 还没开始、或已经停在安全点：直接结束
            await self._finish(project_id, run_id, "stopped" if op == "stop" else "converted")
            return run_view(store.get(project_id, run_id))
        if state == "queued":
            run = store.update(project_id, run_id, lambda record: record.update(state="paused"))
            self._updated(run)
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

        was_idle = state in ("paused", "interrupted")
        run = store.update(project_id, run_id, change)
        self._updated(run)
        if was_idle:
            # 停在一片中间：接着把这一片做完，到安全点再停
            def queue(record: dict) -> None:
                record["resume_state"] = record["state"]
                record["state"] = "queued"

            store.update(project_id, run_id, queue)
            await self.admit()
        return run_view(store.get(project_id, run_id) or run)

    @staticmethod
    def _in_slice(run: dict) -> bool:
        return any(info.get("state") == "running" for info in (run.get("slices") or {}).values())

    def pause_all_at_safe_point(self) -> int:
        """进入维护模式（O-21 `autobuild=pause_at_safe_point`）：在跑的运行做完这一片就暂停。"""
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
        """策略刚答了一组问题：切片进展块更新，挂起的进待处理。"""
        from ...governance import telemetry

        run = store.get(project_id, run_id)
        if run is None:
            return
        unit = next((item for item in run.get("units") or [] if item["id"] == (run.get("cursor") or {}).get("unit")), None)
        if unit is not None:
            self._slice_card(run, unit)
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

    def _post_card(self, run: dict, card: dict) -> None:
        posted = card["card_id"] in (run.get("cards") or [])
        if not posted:
            store.update(run["project_id"], run["run_id"], lambda record: record.setdefault("cards", []).append(card["card_id"]))
            run.setdefault("cards", []).append(card["card_id"])
        self._post(run, {"type": "card_updated" if posted else "card", "payload": card})

    def _slice_card(self, run: dict | None, unit: dict) -> None:
        if run is not None:
            self._post_card(run, views.slice_card(run, unit))

    def _updated(self, run: dict | None) -> None:
        if run is not None:
            _publish(run["project_id"], "autobuild.run.updated", views.summary(run))

    def _refresh(self, project_id: str) -> None:
        from .. import project_events

        project_events.schedule_flow_refresh(project_id)
        project_events.card_changed(project_id)

    def _notify(self, run: dict, kind: str, *, count: int = 0) -> None:
        """用户流 EU-01（通知中心 A-13 还没有，只推实时通知）。"""
        from .. import streams

        owner = run.get("owner") or run.get("actor") or ""
        summary = views.summary(run)
        title, body = {
            "autobuild_finished": ("自主构建完成", f"需复核 {summary['needs_review']} 项"),
            "autobuild_suspension": ("新增挂起问题", f"{count} 个"),
        }.get(kind, ("自主构建已停下", END_NOTICE.get(run["state"], "")))
        try:
            meta = _ws().get_project(run["project_id"]) or {}
            streams.publish_user(owner, "notification", {
                "id": f"nt-{uuid.uuid4().hex[:10]}", "kind": kind, "title": title, "body": body,
                "project_id": run["project_id"], "project_name": meta.get("name"),
                "action": {"label": "去复核", "page": "inbox", "params": {"filter": "autobuild_review"}}
                if kind != "autobuild_stopped" else {"label": "查看", "page": "l4-run",
                                                     "params": {"c": run["conversation_id"]}},
                "created_at": views.iso(time.time()), "read": False}, project_id=run["project_id"])
        except Exception:  # noqa: BLE001 - 通知是增强信息
            _log.warning("自主构建通知发不出去", exc_info=True)

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
        from . import review

        run = store.get(project_id, run_id)
        queued = list((run or {}).get("pending_rejudges") or [])
        if not queued:
            return
        store.update(project_id, run_id, lambda record: record.update(pending_rejudges=[]))
        for item in queued:
            try:
                await review.send_rejudge(project_id, run_id, item["entry_id"], task_id=item.get("task_id"))
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
    """暂停中（pausing）重启：直接算暂停；停在安全点、转为陪伴的接着把这一片做完。"""
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
                return
            if previous in ("stopping", "converting"):
                record["resume_state"] = previous
            record["state"] = "interrupted"
            record["resume_pending"] = True

        updated = store.update(run["project_id"], run["run_id"], change)
        if updated is not None:
            changed.append(updated)
    if not changed:
        return None  # 没有停机时在跑的运行：不进报告（同 agent 提交钩子、助手副本）
    per_project: dict[str, int] = {}
    for run in changed:
        per_project[run["project_id"]] = per_project.get(run["project_id"], 0) + 1
    resumed = sum(1 for run in changed if run.get("resume_pending"))
    return {"id": "autobuild_runs", "title": "自主构建", "result": "cleared", "tone": "fix",
            "detail": f"{len(changed)} 次运行停在检查点，{resumed} 次启动后自动续跑",
            "projects": [{"project_id": pid, "count": count, "paths": []} for pid, count in sorted(per_project.items())]}


def register_steps(register) -> None:
    register("autobuild_runs", "自主构建", startup_sweep, order=105)
