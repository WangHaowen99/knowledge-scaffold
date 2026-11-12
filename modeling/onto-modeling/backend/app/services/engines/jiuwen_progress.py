"""同步 task_tool 的子代理过程观测：只上报有界步骤流，不搬运子代理正文。

子代理的 chunk 全部留在 task_tool 内部（见 openjiuwen task_tool 的
``_run_subagent_with_observable_stream``），主会话收不到。这里用 Rail 回调把
"子代理执行了哪一步"桥回主事件队列：父 ``task_tool`` 的 before/after 负责
开卡与收卡，子代理的工具/模型回调产出步骤事件。子代理一次模型调用超过 15 秒还没
出结果（只在思考、不调工具）时，每 15 秒报一次「思考中」，界面据此显示已想了多久。
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# 单个子任务的步骤事件上限：事件会逐条进 JSONL 历史并参与回放，长子任务
# 不能把历史刷爆（CLAUDE.md §6 结果体积封顶）。最坏一个子任务 200 步 × 约 500 字节 ≈ 100 KB
_MAX_STEPS = 200
# 工具参数里只保留卡片摘要需要的键：子代理的 write_file 参数可能携带整段文件内容
_SUMMARY_KEYS = ("file_path", "path", "pattern", "command", "description", "url", "query")
_SUMMARY_LIMIT = 200
_TEXT_LIMIT = 300
# 派给子代理的任务说明：面板上能展开看，只留前 2000 字
_BRIEF_LIMIT = 2000
# 子代理一次模型调用期间「思考中」的上报间隔（秒）；不到一个间隔就出结果的调用不报
THINKING_EVERY = 15.0

_CURRENT_TRACKER: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "onto_jiuwen_progress_tracker", default=None
)
_CURRENT_TASK: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "onto_jiuwen_progress_task", default=None
)


def in_subagent() -> bool:
    """当前回调是不是子代理里的：派发子任务时 task_tool 把它设上，子代理的回调都在它里面跑。"""
    return _CURRENT_TASK.get() is not None


@contextmanager
def progress_scope(tracker: JiuwenProgressTracker):
    token = _CURRENT_TRACKER.set(tracker)
    try:
        yield
    finally:
        _CURRENT_TRACKER.reset(token)


@dataclass
class _TaskProgress:
    call_id: str
    description: str
    agent_type: str
    started: float
    emitted: int = 0  # 已发出的步骤事件数（含省略提示），用于封顶
    open_steps: dict[str, str] = field(default_factory=dict)  # step_id → 工具名
    finished: bool = False
    brief: str = ""
    thinking: asyncio.Task | None = None  # 这次模型调用的「思考中」上报


class JiuwenProgressTracker:
    """每个派发任务一条有界步骤流；模型调用久了报「思考中」，耗时在终态上报一次。"""

    def __init__(
        self,
        emit: Callable[[dict], Any],
        clock: Callable[[], float] = time.monotonic,
        thinking_every: float = THINKING_EVERY,
    ):
        self._emit = emit
        self._clock = clock
        self._thinking_every = thinking_every
        self._tasks: dict[str, _TaskProgress] = {}

    def _event(self, task: _TaskProgress, event_type: str, **extra: Any) -> dict:
        return {
            "type": event_type,
            "task_id": task.call_id,
            "tool_use_id": task.call_id,
            "description": task.description,
            "agent_type": task.agent_type,
            **extra,
        }

    async def _emit_event(self, event: dict) -> None:
        result = self._emit(event)
        if inspect.isawaitable(result):
            await result

    async def _reserve(self, task: _TaskProgress) -> bool:
        """占用一个步骤额度；额度耗尽时只补一条省略提示。"""
        if task.emitted < _MAX_STEPS:
            task.emitted += 1
            return True
        if task.emitted == _MAX_STEPS:
            task.emitted += 1
            await self._emit_event(
                self._event(
                    task,
                    "subagent_activity",
                    step_id=f"{task.call_id}-truncated",
                    kind="text",
                    text="步骤较多，后续过程已省略",
                )
            )
        return False

    async def start(self, call_id: str, args: dict) -> _TaskProgress:
        existing = self._tasks.get(call_id)
        if existing is not None:
            return existing
        # task_description 常常是一整段提示词资产：只有简短单行的才直接当名称，
        # 其余整段作任务说明，名称由会话宿主按「说明第一行 → 子代理类型」回退（timeline.subagent_title）
        label = str(args.get("description") or "").strip()
        brief = str(args.get("task_description") or "").strip()
        short_brief = len(brief) <= 120 and "\n" not in brief
        if not label and brief and short_brief:
            label = brief
        if len(label) > 120 or "\n" in label:
            label = ""
        task = _TaskProgress(
            call_id=call_id,
            description=label,
            agent_type=str(args.get("subagent_type") or "general-purpose")[:80],
            started=self._clock(),
            brief=brief[:_BRIEF_LIMIT],
        )
        self._tasks[call_id] = task
        await self._emit_event(self._event(task, "subagent_started", brief=task.brief))
        return task

    def thinking_start(self, task: _TaskProgress) -> None:
        """子代理开始一次模型调用：超过一个间隔还没结果就开始报「思考中」。"""
        self.thinking_stop(task)
        if task.finished:
            return
        task.thinking = asyncio.create_task(self._think(task, time.time()))

    def thinking_stop(self, task: _TaskProgress) -> None:
        if task.thinking is not None:
            task.thinking.cancel()
            task.thinking = None

    async def _think(self, task: _TaskProgress, since: float) -> None:
        # 不占步骤额度：「思考中」是瞬时事件，不进历史（timeline.ingest），只刷新界面
        while not task.finished:
            await asyncio.sleep(self._thinking_every)
            if task.finished:
                return
            await self._emit_event(self._event(
                task, "subagent_activity", step_id=f"{task.call_id}-thinking", kind="thinking",
                since=since, elapsed_ms=int((time.time() - since) * 1000),
            ))

    async def step(
        self, task: _TaskProgress, step_id: str, tool_name: str, args: Any
    ) -> None:
        if task.finished or step_id in task.open_steps:
            return
        if not await self._reserve(task):
            return
        task.open_steps[step_id] = tool_name
        await self._emit_event(
            self._event(
                task,
                "subagent_activity",
                step_id=step_id,
                kind="tool",
                tool=tool_name,
                input=_summary_input(args),
            )
        )

    async def step_done(self, task: _TaskProgress, step_id: str, failed: bool) -> None:
        tool_name = task.open_steps.pop(step_id, None)
        if task.finished or tool_name is None:
            return
        await self._emit_event(
            self._event(
                task,
                "subagent_activity",
                step_id=step_id,
                kind="tool",
                tool=tool_name,
                status="error" if failed else "ok",
            )
        )

    async def say(self, task: _TaskProgress, text: str) -> None:
        body = str(text or "").strip()
        if task.finished or not body:
            return
        if not await self._reserve(task):
            return
        await self._emit_event(
            self._event(
                task,
                "subagent_activity",
                step_id=f"{task.call_id}-say-{task.emitted}",
                kind="text",
                text=body[:_TEXT_LIMIT] + ("…" if len(body) > _TEXT_LIMIT else ""),
            )
        )

    async def finish(self, task: _TaskProgress, status: str, summary: str = "") -> None:
        if task.finished:
            return
        task.finished = True
        self.thinking_stop(task)
        self._tasks.pop(task.call_id, None)
        extra = {"summary": summary} if summary else {}
        await self._emit_event(
            self._event(
                task,
                "subagent_finished",
                status=status,
                usage={"duration_ms": max(0, int((self._clock() - task.started) * 1000))},
                **extra,
            )
        )

    async def finish_all(self, status: str) -> None:
        for task in list(self._tasks.values()):
            await self.finish(task, status)


def _summary_input(args: Any) -> dict:
    """工具参数 → 卡片摘要参数子集（前端 presentTool 只读这几个键）。"""
    if not isinstance(args, dict):
        return {}
    summary = {}
    for key in _SUMMARY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            summary[key] = value[:_SUMMARY_LIMIT]
    return summary


def _args(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _call_id(ctx: Any) -> str:
    existing = getattr(ctx, "_onto_progress_call_id", None)
    if existing:
        return existing
    call = getattr(ctx.inputs, "tool_call", None)
    native_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
    call_id = str(native_id or uuid.uuid4().hex[:12])
    ctx._onto_progress_call_id = call_id
    return call_id


def _failed(ctx: Any) -> bool:
    result = getattr(ctx.inputs, "tool_result", None)
    success = result.get("success") if isinstance(result, dict) else getattr(result, "success", None)
    return getattr(ctx, "exception", None) is not None or success is False


def _result_summary(ctx: Any, failed: bool) -> str:
    """子任务结束时卡片和面板上的一句话：做完了取它交回的结果开头；失败只给平台自己写的原因，
    原始异常文本不进界面。"""
    if failed:
        from .jiuwen import TRUNCATION_FAILURE

        raw = f"{getattr(ctx, 'exception', '') or ''} {getattr(ctx.inputs, 'tool_result', '') or ''}"
        return TRUNCATION_FAILURE if TRUNCATION_FAILURE[:12] in raw else "子任务出错，没有交回结果"
    result = getattr(ctx.inputs, "tool_result", None)
    data = result.get("data") if isinstance(result, dict) else getattr(result, "data", None)
    output = data.get("output") if isinstance(data, dict) else None
    text = " ".join(str(output or "").split())
    return text[:_TEXT_LIMIT] + ("…" if len(text) > _TEXT_LIMIT else "")


def _response_text(response: Any) -> str:
    """模型响应正文；带 tool_calls 的轮次由步骤行呈现，不再重复成文字。"""
    if response is None or getattr(response, "tool_calls", None):
        return ""
    content = getattr(response, "content", None)
    return content if isinstance(content, str) else ""


def build_progress_rail(tool_names: dict[str, str]):
    """延迟加载 Jiuwen 并把本观测器带进 general-purpose 子代理。

    ToolTrackingRail 本身不带调用 id，这个子类保留它的结果格式化并补上原生 id，
    让生命周期事件与工具行能可靠对上卡片。状态全部挂在调用上下文里，所以
    ``fork_for_agent`` 出来的子代理实例与共享 Rail 对象的旧版本都能工作。
    """
    from openjiuwen.core.session.stream.base import OutputSchema
    from openjiuwen.harness.cli.rails.tool_tracker import ToolTrackingRail

    class SubagentProgressRail(ToolTrackingRail):
        def fork_for_agent(self):
            return SubagentProgressRail()

        async def before_tool_call(self, ctx: Any) -> None:
            name = str(getattr(ctx.inputs, "tool_name", "") or "")
            args = _args(getattr(ctx.inputs, "tool_args", {}))
            call_id = _call_id(ctx)
            tracker = _CURRENT_TRACKER.get()
            current = _CURRENT_TASK.get()
            if tracker is not None and name == "task_tool" and current is None:
                task = await tracker.start(call_id, args if isinstance(args, dict) else {})
                ctx._onto_progress_owner = task
                ctx._onto_progress_token = _CURRENT_TASK.set(task)
            elif tracker is not None and current is not None:
                await tracker.step(
                    current, call_id, tool_names.get(name, name)[:80], args
                )
            if ctx.session is not None:
                await ctx.session.write_stream(OutputSchema(
                    type="tool_call", index=0,
                    payload={"tool_name": name, "tool_args": args, "tool_use_id": call_id},
                ))

        async def after_tool_call(self, ctx: Any) -> None:
            name = str(getattr(ctx.inputs, "tool_name", "") or "")
            result = getattr(ctx.inputs, "tool_result", None)
            failed = _failed(ctx)
            if ctx.session is not None:
                await ctx.session.write_stream(OutputSchema(
                    type="tool_result", index=0,
                    payload={
                        "tool_name": name,
                        "tool_args": _args(getattr(ctx.inputs, "tool_args", {})),
                        "tool_use_id": _call_id(ctx),
                        "is_error": failed,
                        **self._build_tool_result_payload(name, result),
                    },
                ))
            tracker = _CURRENT_TRACKER.get()
            owner = getattr(ctx, "_onto_progress_owner", None)
            if owner is not None:
                try:
                    if tracker is not None:
                        await tracker.finish(owner, "failed" if failed else "completed",
                                             _result_summary(ctx, failed))
                finally:
                    token = getattr(ctx, "_onto_progress_token", None)
                    if token is not None:
                        _CURRENT_TASK.reset(token)
                        ctx._onto_progress_token = None
                    ctx._onto_progress_owner = None
                return
            current = _CURRENT_TASK.get()
            if tracker is not None and current is not None:
                await tracker.step_done(current, _call_id(ctx), failed)

        async def on_tool_exception(self, ctx: Any) -> None:
            # Jiuwen 可能重试：这里只把该步骤标成失败，任务终态由 after_tool_call
            # 或流终止决定；原始异常文本不进界面
            tracker, task = _CURRENT_TRACKER.get(), _CURRENT_TASK.get()
            if tracker is None or task is None:
                return
            if getattr(ctx, "_onto_progress_owner", None) is None:
                await tracker.step_done(task, _call_id(ctx), True)

        async def before_model_call(self, ctx: Any) -> None:
            tracker, task = _CURRENT_TRACKER.get(), _CURRENT_TASK.get()
            if tracker is not None and task is not None:
                tracker.thinking_start(task)

        async def on_model_exception(self, ctx: Any) -> None:
            tracker, task = _CURRENT_TRACKER.get(), _CURRENT_TASK.get()
            if tracker is not None and task is not None:
                tracker.thinking_stop(task)

        async def after_model_call(self, ctx: Any) -> None:
            tracker, task = _CURRENT_TRACKER.get(), _CURRENT_TASK.get()
            if tracker is None or task is None:
                return
            tracker.thinking_stop(task)
            if task.finished or getattr(ctx, "exception", None) is not None:
                return
            await tracker.say(task, _response_text(getattr(ctx.inputs, "response", None)))

    return SubagentProgressRail()
