"""openJiuwen DeepAgent 引擎：进程内实例，无子进程（方案 Phase 2）。

与 Claude 引擎的形态差异（详见 docs/openjiuwen-engine-migration.md §3.2 与 §9 PoC 结论）：
- 进程内对象：空闲会话 ≈3MB / 活跃轮次 ≈12MB（Claude CLI 子进程 ~400MB）
- 逐轮驱动：send() 启动一个 turn 任务消费 Runner.run_agent_streaming 的 chunk 流，
  桥接为平台事件推入内部队列；events() 从队列产出
- 确认卡：ask_user / 审批 interaction 桥到 ctx.request_permission（与 Claude 引擎
  同一张前端卡），答案经 InteractiveInput 回填后续跑
- resume = 框架 checkpoint 续接：会话状态（含工具调用/结果、思考的完整上下文）
  落 DATA_DIR 下的 SQLite，回收、重启后凭续接 token 由 Runner 恢复；无可用存档时
  （首次切到本引擎、显式关闭过会话、上一轮停在确认卡上）退回历史回放——
  把事件历史里的问答文本注水 create_new_context_engine，历史里有压缩摘要时从摘要接续
- 上下文压缩：ContextProcessorRail 挂三个压缩器，窗口与触发比例按配置页 / .env
  推算（model_settings.jiuwen_effective_context）；上下文用量与压缩以 context_usage /
  context_compacted 事件呈现，与稳定引擎同一套前端展示
- 目录分层：DeepAgentConfig.cwd = 项目 git 工作区（文件/Shell 工具的根），
  workspace = DATA_DIR/jiuwen/<pid>（框架私有工件区），互不污染；子代理工件区
  再下沉到 workspace/sub_agents（必须与主 agent 分开，见 _general_purpose_spec）

同步 task_tool 的子代理执行步骤由 jiuwen_progress.py 通过 Rail 回调送回主事件队列；
成本无单价来源，只报 tokens：每次模型调用发 model_usage 逐次记账，result 只给单轮合计。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ... import config
from .base import FRIENDLY_ERROR, EngineContext, config_error_for, safe_json
from .jiuwen_progress import JiuwenProgressTracker, build_progress_rail, progress_scope

JIUWEN_AVAILABLE = importlib.util.find_spec("openjiuwen") is not None

UNAVAILABLE_MESSAGE = "openjiuwen 未安装（需 Python 3.11+ 镜像），无法启动 Jiuwen 引擎会话"
UNCONFIGURED_MESSAGE = (
    "Jiuwen 引擎未配置模型。请管理员在「配置中心 → 模型接入 → OpenAI 兼容（Jiuwen 引擎）」"
    "填写网关地址、API Key 与模型名。"
)

# jiuwen 工具名 → 前端已知的工具语义名（TOOL_VERBS 映射所依赖）；
# 参数键（file_path/content/command/description）两边一致，无需转换
_TOOL_NAME_MAP = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "bash": "Bash",
    "powershell": "Bash",
    "list_files": "Glob",
    "glob": "Glob",
    "grep": "Grep",
    "task_tool": "Task",
    "todo": "TodoWrite",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
}

# 历史回放的上下文预算（字符）：从最近往前取，避免超长历史撑爆上下文。
# 连接时按模型窗口的四分之一折算（中文约一字一 token，取保守口径）；
# 超过压缩触发点的部分在首次请求前会被压缩器压掉，预算只防极端长的历史
_REPLAY_CHAR_BUDGET = 24_000
_REPLAY_WINDOW_SHARE = 0.25

# 摘要覆盖此前全部对话的压缩器：续接时用最近一次的摘要代替压缩前的历史。
# CurrentRoundCompressor 只压当轮的工具往返，摘要不覆盖之前的对话，不用于续接
_PREFIX_COMPRESSORS = frozenset({"DialogueCompressor", "RoundLevelCompressor"})
_COMPRESSORS = _PREFIX_COMPRESSORS | {"CurrentRoundCompressor"}
_RUNNER_STARTED = False
_RUNNER_LOCK: asyncio.Lock | None = None


def shadow_dir(project_id: str) -> Path:
    """框架私有工件区（见模块文档「目录分层」）；删除项目时随之清除。"""
    return Path(config.DATA_DIR) / "jiuwen" / project_id


def model_config(owner: str = "") -> dict:
    """Jiuwen 引擎的 OpenAI 兼容模型配置：UI 配置优先，env 兜底。

    owner 非空时 Key 换成该用户的个人 Key，网关地址 / 模型个人未填则沿用上述结果；
    个人配置读取失败直接抛错，不回落到管理员 Key。

    context 为生效的上下文窗口、最大输出与压缩触发比例，一并计入配置指纹：
    改了上下文参数，空闲会话同样按新配置重建。个人配置不含上下文参数，
    与稳定引擎一样沿用管理员配置。
    """
    from .. import model_settings

    cfg = {
        "base_url": config.JIUWEN_BASE_URL,
        "api_key": config.JIUWEN_API_KEY,
        "model": config.JIUWEN_MODEL,
    }
    try:
        ui = model_settings.jiuwen_config()
        for key in cfg:
            if ui.get(key):
                cfg[key] = ui[key]
    except Exception:
        pass
    if owner:
        from .. import personal_model_settings

        personal = personal_model_settings.resolve(owner, "jiuwen")
        cfg = {
            "base_url": personal["base_url"] or cfg["base_url"],
            "api_key": personal["api_key"],
            "model": personal["model"] or cfg["model"],
        }
    cfg["context"] = model_settings.jiuwen_effective_context()
    return cfg


def _fingerprint(cfg: dict) -> str:
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _checkpoint_db_path() -> Path:
    return Path(config.DATA_DIR) / "jiuwen" / "checkpoints.db"


async def _ensure_runner_started(log_error: Callable[[str, str], None]) -> None:
    global _RUNNER_STARTED, _RUNNER_LOCK
    if _RUNNER_LOCK is None:
        _RUNNER_LOCK = asyncio.Lock()
    async with _RUNNER_LOCK:
        if _RUNNER_STARTED:
            return
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.runner.runner_config import (
            get_runner_config,
            set_runner_config,
        )
        from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig

        # 默认的内存 checkpointer 随进程消失；改为持久化，Runner.start 据此装配
        checkpointer = CheckpointerConfig(
            type="persistence",
            conf={"db_type": "sqlite", "db_path": str(_checkpoint_db_path())},
        )
        set_runner_config(
            get_runner_config().model_copy(update={"checkpointer_config": checkpointer})
        )
        try:
            await Runner.start()
        except Exception as exc:
            # 持久化存档装配失败（如缺 aiosqlite）：退回内存版，续接降级为历史回放，不阻断会话
            log_error("checkpoint", f"持久化存档不可用，已退回内存存档：{exc}")
            set_runner_config(
                get_runner_config().model_copy(update={"checkpointer_config": None})
            )
            await Runner.start()
        _RUNNER_STARTED = True


# 系统提示词与命令/守卫由资产装载器提供（jiuwen_assets.py，Phase 3：
# workspace-rules + 命令索引 + 运行时适配注记；单一资产事实源）


class _ChunkBridge:
    """openjiuwen chunk 流 → 平台事件（契约见 engines/base.py 模块文档）。

    优先使用进度 Rail 携带的原生调用 id；旧 chunk 无 id 时保留 FIFO 配对
    （主引擎以 parallel_tool_calls=False 运行）。

    子代理的 chunk 留在 task_tool 内部不进主流，这里的上下文用量与压缩都属于主 agent。
    """

    def __init__(self):
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._open_calls: deque[str] = deque()
        # 最近一次模型调用占用的上下文，随下一条 assistant_message 带出
        self._context_tokens: int | None = None

    @staticmethod
    def _payload_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            return str(payload.get("content") or payload.get("output") or "")
        return str(getattr(payload, "content", "") or "")

    def _flush_blocks(self, extra: list[dict] | None = None) -> list[dict]:
        blocks: list[dict] = []
        thinking = "".join(self._thinking).strip()
        if thinking:
            blocks.append({"kind": "thinking", "text": thinking})
        text = "".join(self._text).strip()
        if text:
            blocks.append({"kind": "text", "text": text})
        self._text.clear()
        self._thinking.clear()
        blocks.extend(extra or [])
        if not blocks:
            return []
        event = {
            "type": "assistant_message",
            "blocks": blocks,
            "model": None,
            "subagent": False,
        }
        if self._context_tokens:
            event["context_tokens"] = self._context_tokens
            self._context_tokens = None
        return [event]

    def _note_context_usage(self, ctype: str, payload: Any) -> None:
        """context.usage 给出发给模型的上下文（服务端未报时为本地估算）；
        llm_usage 随后给出服务端实报的输入与输出，口径与稳定引擎一致（输入 + 输出）。"""
        if not isinstance(payload, dict):
            return
        if ctype == "context.usage":
            window = payload.get("context_window") or {}
            tokens = window.get("input_tokens") or window.get("local_estimated_input_tokens")
        else:
            usage = payload.get("usage_metadata") or {}
            tokens = usage.get("input_tokens") or 0
            if tokens:
                tokens += usage.get("output_tokens") or 0
        if isinstance(tokens, int) and tokens > 0:
            self._context_tokens = tokens

    def _compaction_events(self, payload: Any) -> list[dict]:
        """压缩器完成一次压缩 → context_compacted；前缀压缩器的摘要随事件落盘，供续接回放。"""
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            return []
        processor = payload.get("processor") or ""
        if processor not in _COMPRESSORS:
            return []
        before = payload.get("before") or {}
        after = payload.get("after") or {}
        event = {
            "type": "context_compacted",
            "trigger": "auto",
            "pre_tokens": before.get("tokens") or None,
            "post_tokens": after.get("tokens") or None,
        }
        if processor in _PREFIX_COMPRESSORS and payload.get("compact_summary"):
            event["summary"] = payload["compact_summary"]
        events = self._flush_blocks()
        self._context_tokens = None  # 压缩前的用量已过时，以 post_tokens 为准
        return events + [event]

    def feed(self, chunk: Any) -> tuple[list[dict], tuple[str, Any] | None]:
        """返回 (平台事件列表, 待回填 interaction 或 None)。"""
        ctype = getattr(chunk, "type", "")
        payload = getattr(chunk, "payload", None)

        if ctype == "llm_output":
            text = self._payload_text(payload)
            if not text:
                return [], None
            self._text.append(text)
            return [{"type": "stream_delta", "kind": "text", "text": text}], None

        if ctype == "llm_reasoning":
            text = self._payload_text(payload)
            if not text:
                return [], None
            self._thinking.append(text)
            return [{"type": "stream_delta", "kind": "thinking", "text": text}], None

        if ctype == "tool_call":
            raw_name = ""
            args: Any = {}
            if isinstance(payload, dict):
                raw_name = str(payload.get("tool_name") or "")
                args = payload.get("tool_args")
            native_id = payload.get("tool_use_id") if isinstance(payload, dict) else None
            call_id = str(native_id or uuid.uuid4().hex[:12])
            self._open_calls.append(call_id)
            tool_block = {
                "kind": "tool_use",
                "tool": _TOOL_NAME_MAP.get(raw_name, raw_name or "tool"),
                "input": safe_json(args if isinstance(args, dict) else {"input": args}),
                "tool_use_id": call_id,
            }
            return self._flush_blocks([tool_block]), None

        if ctype == "tool_result":
            native_id = payload.get("tool_use_id") if isinstance(payload, dict) else None
            if native_id:
                call_id = str(native_id)
                if call_id in self._open_calls:
                    self._open_calls.remove(call_id)
            else:
                call_id = self._open_calls.popleft() if self._open_calls else ""
            excerpt = ""
            is_error = False
            if isinstance(payload, dict):
                excerpt = str(payload.get("tool_result") or "")
                is_error = bool(payload.get("is_error"))
            excerpt = excerpt.strip()
            if len(excerpt) > 400:
                excerpt = excerpt[:400] + "…"
            return [
                {
                    "type": "tool_results",
                    "blocks": [
                        {
                            "kind": "tool_result",
                            "tool_use_id": call_id,
                            "is_error": is_error,
                            "excerpt": excerpt,
                        }
                    ],
                }
            ], None

        if ctype == "__interaction__":
            if isinstance(payload, dict):
                iid = payload.get("interaction_id", "unknown")
                request = payload
            else:
                iid = getattr(payload, "id", "unknown")
                request = getattr(payload, "value", payload)
            return self._flush_blocks(), (str(iid), request)

        if ctype in ("context.usage", "llm_usage"):
            self._note_context_usage(ctype, payload)
            return [], None

        if ctype == "context.compression_state":
            return self._compaction_events(payload), None

        # answer 是 llm_output 的汇总（避免重复）；其余运行时噪音一律忽略
        return [], None

    def finish(self) -> list[dict]:
        return self._flush_blocks()


def _general_purpose_spec(
    model: Any, system_prompt: str, rails: list, workspace_root: Path, sys_operation: Any
):
    """自带的 general-purpose 子代理 spec（必须与主 agent 的 workspace 根分开）。

    openjiuwen 的 ``add_general_purpose_agent=True`` 会注入一个把**父 agent 的
    workspace 原样带上**的子代理 spec，父子共用同一个 workspace 根时子代理会在
    首个模型调用之前永久挂住（父 agent 停在 task_tool 的子流迭代上，子代理一个
    Rail 回调都发不出）。这里显式提供同名 spec 抢在注入之前，并给子代理独立的
    工件目录；spec 的 sys_operation 只在 workspace 同时给出时才被采纳，必须一起
    传，否则子代理会自建 LOCAL sysop、丢掉沙箱三根。

    这里给的只是初值，真正生效的是每次派发时 _bind_subagent_workspace_rotation
    换上的按派遣命名的目录。
    """
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.prompts.tools.task_tool import GENERAL_PURPOSE_AGENT_DESC
    from openjiuwen.harness.schema.config import SubAgentConfig
    from openjiuwen.harness.workspace.workspace import Workspace

    return SubAgentConfig(
        agent_card=AgentCard(
            name="general-purpose",
            description=GENERAL_PURPOSE_AGENT_DESC["cn"],
        ),
        system_prompt=system_prompt,
        model=model,
        rails=list(rails),
        workspace=Workspace(root_path=str(workspace_root), language="cn"),
        sys_operation=sys_operation,
        restrict_to_work_dir=False,
    )


def _bind_subagent_workspace_rotation(agent: Any, spec: Any, root: Path) -> None:
    """每次派发都给子代理一个独立工件目录（不能复用同一个根）。

    openjiuwen 的文件锁按"目标路径哈希"建 SQLite 锁库，而租约释放后进程内的
    锁并不会立刻放掉。两次派发若共用同一个 workspace 根，第二个子代理写同名
    脚手架文件（sub_agents/memory/.workspace 等）会卡满 ``write_file`` 的 300 秒
    lock_timeout 再失败——界面上就是子任务卡一直停在"已启动，等待子代理反馈"。

    ``create_subagent`` 是所有派发路径的唯一汇聚点（SDK 自己的 observability
    也挂在这里），派发前把 spec 的 workspace 换成按 subsession 命名的目录即可。
    赋值与调用之间没有 await，并发派发也不会互相串台；spec.workspace 必须保持
    非空，否则 SDK 不采纳我们的 sys_operation（沙箱三根会丢）。
    """
    from openjiuwen.harness.workspace.workspace import Workspace

    original = agent.create_subagent

    def create_subagent(subagent_type: str, subsession_id: str, *args: Any, **kwargs: Any):
        spec.workspace = Workspace(
            root_path=str(root / str(subsession_id)), language="cn"
        )
        return original(subagent_type, subsession_id, *args, **kwargs)

    agent.create_subagent = create_subagent


def _context_processor_rail(trigger_ratio: float):
    """上下文压缩 Rail：只挂三个压缩器，上下文达到「窗口 × trigger_ratio」时压缩。

    不用 SDK 预设链：预设里的 MessageSummaryOffloader 会把大的工具结果换成摘要（模型要原文
    只能重跑工具），SessionMemoryCompressor 默认关闭。Rail 在 init 时绑定所属 agent，
    主 agent 与子代理各用一个实例，不能共用。
    """
    from openjiuwen.core.context_engine.processor import forked
    from openjiuwen.harness.rails.context_engineer import ContextProcessorRail

    # 用 forked 版压缩器顶替同名的旧实现；preset=False 时 Rail 不会替我们注册
    forked.activate()
    return ContextProcessorRail(
        preset=False,
        processors=[
            (
                "DialogueCompressor",
                forked.DialogueCompressorConfig(trigger_context_ratio=trigger_ratio),
            ),
            (
                "CurrentRoundCompressor",
                forked.CurrentRoundCompressorConfig(trigger_context_ratio=trigger_ratio),
            ),
            (
                "RoundLevelCompressor",
                forked.RoundLevelCompressorConfig(trigger_context_ratio=trigger_ratio),
            ),
        ],
    )


def _call_usage_rail(on_call_usage: Callable[[int, int], None]):
    """在 TokenTrackingRail 的累计之外，每次模型调用返回后把这次的（输入, 输出）交给回调。

    项目用量按这些逐次增量记账：长命令跑的过程中数字就会增长，打断超时、会话回收、进程重启
    也只丢在飞的那一次调用。子代理 spec 沿用同一个实例（没有 fork_for_agent），一并计入。
    """
    from openjiuwen.harness.cli.rails import TokenTrackingRail

    class CallUsageRail(TokenTrackingRail):
        async def after_model_call(self, ctx: Any) -> None:
            input_before, output_before = self.total_input_tokens, self.total_output_tokens
            await super().after_model_call(ctx)
            input_tokens = self.total_input_tokens - input_before
            output_tokens = self.total_output_tokens - output_before
            if input_tokens or output_tokens:
                on_call_usage(input_tokens, output_tokens)

    return CallUsageRail()


def _req_get(request: Any, key: str, default: Any = None) -> Any:
    """interaction 请求兼容取值：runtime 可能给对象也可能给 dict。"""
    if isinstance(request, dict):
        return request.get(key, default)
    return getattr(request, key, default)


def _interaction_to_permission(request: Any) -> tuple[str, dict]:
    """interaction 请求 → 确认卡的 (tool_name, tool_input)。

    ask_user 的 questions 结构（question/header/options[].label/multiSelect）
    与前端 AskUserQuestion 卡的契约同形，直接透传。
    """
    tool_name = str(_req_get(request, "tool_name", "") or "")
    if tool_name == "ask_user":
        questions = _req_get(request, "questions")
        if not questions:
            # rail 只认规范的 questions 列表；模型传参不规范时（字符串化 JSON、
            # 单数 question、纯 query 文本）从 tool_args 里尽量恢复出卡片
            args = _req_get(request, "tool_args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    args = {}
            args = args if isinstance(args, dict) else {}
            raw_questions = args.get("questions")
            if isinstance(raw_questions, str):
                try:
                    raw_questions = json.loads(raw_questions)
                except (ValueError, TypeError):
                    raw_questions = None
            if isinstance(raw_questions, list) and raw_questions:
                questions = raw_questions
            elif args.get("question"):
                questions = [{
                    "question": str(args["question"]),
                    "options": args.get("options") or [],
                }]
            else:
                text = args.get("query") or args.get("prompt") or args.get("message") or ""
                questions = [{"question": str(text or "请确认"), "options": []}]
        return "AskUserQuestion", {"questions": safe_json(list(questions))}
    args = _req_get(request, "tool_args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = {"input": args}
    return tool_name or "tool", safe_json(args if isinstance(args, dict) else {})


def _fill_interactive_input(ii: Any, iid: str, request: Any, decision: dict) -> None:
    """中立决策 dict → InteractiveInput 回填。"""
    tool_name = str(_req_get(request, "tool_name", "") or "")
    if tool_name == "ask_user":
        answers = (decision.get("updated_input") or {}).get("answers") or {}
        if decision.get("behavior") != "allow":
            # 拒绝提问 = 明确告知不再追问
            answers = {"__declined__": decision.get("message") or "用户取消了该提问"}
        questions = _req_get(request, "questions")
        if questions:
            filled = {}
            for q in questions:
                qtext = str(q.get("question", ""))
                filled[qtext] = str(
                    answers.get(qtext) or next(iter(answers.values()), "") or "确认"
                )
            ii.update(iid, {"answers": filled})
        else:
            ii.update(iid, {"answer": str(next(iter(answers.values()), "") or "确认")})
        return
    approved = decision.get("behavior") == "allow"
    ii.update(
        iid,
        {
            "approved": approved,
            "feedback": "" if approved else (decision.get("message") or "用户拒绝了该操作"),
            "auto_confirm": False,
        },
    )


def _history_to_messages(events: list[dict], char_budget: int = _REPLAY_CHAR_BUDGET):
    """平台事件历史 → 模型消息（历史回放续接的注水材料）。

    历史里有带摘要的压缩事件时从最近一次接续：摘要（压缩器生成的记忆块，原样作为用户消息）
    在前，其后是压缩所在那一轮起的对话；更早的对话已在摘要里，不再回放。
    """
    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        UserMessage,
    )

    summary = ""
    start = 0
    for index, event in enumerate(events):
        if event.get("type") == "context_compacted" and event.get("summary"):
            summary, start = event["summary"], index
    # 压缩发生在该轮请求模型之前，该轮的用户消息在压缩器保留的尾部，从它开始回放
    while start > 0 and events[start].get("type") != "user_message":
        start -= 1

    pairs: list[tuple[str, str]] = []
    for event in events[start:]:
        etype = event.get("type")
        if etype == "user_message":
            text = (event.get("text") or "").strip()
            if text:
                pairs.append(("user", text))
        elif etype == "assistant_message":
            texts = [
                b.get("text", "")
                for b in event.get("blocks", [])
                if b.get("kind") == "text"
            ]
            text = "\n".join(t for t in texts if t).strip()
            if text:
                # 相邻 assistant 段合并，保持 user/assistant 交替尽量干净
                if pairs and pairs[-1][0] == "assistant":
                    pairs[-1] = ("assistant", pairs[-1][1] + "\n" + text)
                else:
                    pairs.append(("assistant", text))
    # 尾部未获回复的用户消息不注水：它们即将作为 pending prompt 重发，
    # 注水会让模型看到两遍（也避免首轮被误标为"已续接"）
    while pairs and pairs[-1][0] == "user":
        pairs.pop()
    # 从最近往前收进预算；摘要总是保留，占用的预算先扣掉
    kept: list[tuple[str, str]] = []
    budget = char_budget - len(summary)
    for role, text in reversed(pairs):
        cost = len(text)
        if budget - cost < 0 and kept:
            break
        budget -= cost
        kept.append((role, text[:char_budget]))
    kept.reverse()
    if summary:
        kept.insert(0, ("user", summary))
    return [
        UserMessage(content=text) if role == "user" else AssistantMessage(content=text)
        for role, text in kept
    ]


class JiuwenEngine:
    """openjiuwen DeepAgent 引擎实现（AgentEngine 协议）。"""

    name = "jiuwen"
    UNAVAILABLE_MESSAGE = UNAVAILABLE_MESSAGE

    def __init__(self, ctx: EngineContext):
        self._ctx = ctx
        self.client: Any = None  # DeepAgent 实例（协议观察点：None = 未连接）
        self.resume_attempted = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._turn_task: asyncio.Task | None = None
        self._buffered: list[str] = []  # 轮内追加的消息，本轮结束后接续
        # 上下文引擎与流式运行都按这个 id 寻址：每条会话一个，default 沿用单会话时期的值
        self._sid = f"onto-{ctx.project_id}"
        if ctx.conversation_id != "default":
            self._sid += f"-{ctx.conversation_id}"
        # 续接 token：本引擎每轮正常收尾后写下，表示 checkpoint 可用于续接
        self._resume_token = f"jiuwen:{self._sid}"
        from .jiuwen_assets import JiuwenAssetLoader

        self._loader = JiuwenAssetLoader(Path(config.ASSETS_DIR))
        self._fingerprint = ""
        self._tracker: Any = None
        self._closed = False
        self._progress_tracker: JiuwenProgressTracker | None = None
        # 上下文用量：每轮结束前以 context_usage 下发（与稳定引擎同一事件）
        self._context_used: int | None = None
        self._context_window: int | None = None
        self._compact_at: int | None = None

    @classmethod
    def available(cls) -> bool:
        return JIUWEN_AVAILABLE

    # ---- 连接生命周期 ----

    async def connect(self) -> bool:
        if not JIUWEN_AVAILABLE:
            raise RuntimeError(UNAVAILABLE_MESSAGE)
        cfg = model_config(self._ctx.model_owner)
        if not (cfg.get("base_url") and cfg.get("api_key") and cfg.get("model")):
            raise RuntimeError(UNCONFIGURED_MESSAGE)
        # openjiuwen 首次 import 较重（秒级），放线程避免卡住事件循环
        agent, tracker = await asyncio.to_thread(self._build_agent, cfg)
        await _ensure_runner_started(self._ctx.log_error)
        context = cfg["context"]
        # 存档里是压缩后的上下文（压缩摘要也在其中），能续接就不再看事件历史
        resumed = await self._resume_from_checkpoint()
        if not resumed:
            history = list(self._ctx.load_history()) if self._ctx.load_history else []
            replayed = _history_to_messages(
                history, int(context["max_context_tokens"] * _REPLAY_WINDOW_SHARE)
            )
            if replayed:
                await agent.create_new_context_engine(
                    session_id=self._sid, messages=replayed
                )
            resumed = bool(replayed)
        self.client = agent
        self._tracker = tracker
        self._fingerprint = _fingerprint(cfg)
        self._context_window = context["max_context_tokens"]
        self._compact_at = int(context["max_context_tokens"] * context["trigger_ratio"])
        self.resume_attempted = resumed
        return resumed

    async def _resume_from_checkpoint(self) -> bool:
        """续接 token 有效且存档仍在：首轮由 Runner 从 checkpoint 恢复完整上下文。

        否则清掉残留存档，交给历史回放——显式关闭过会话（token 已清）、
        上一轮停在确认卡上（存档带挂起中断，普通消息会被当成卡片答复）都走这里。
        """
        from openjiuwen.core.session.checkpointer import CheckpointerFactory

        checkpointer = CheckpointerFactory.get_checkpointer()
        if self._ctx.load_resume_token() == self._resume_token:
            if await checkpointer.session_exists(self._sid):
                return True
        await checkpointer.release(self._sid)
        return False

    def _build_agent(self, cfg: dict):
        """测试替换点：单测注入假 agent，不触碰真实框架。"""
        from openjiuwen.core.foundation.llm import init_model
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.single_agent import AgentCard
        from openjiuwen.core.sys_operation import (
            LocalWorkConfig,
            OperationMode,
            SysOperationCard,
        )
        from openjiuwen.harness import create_deep_agent
        from openjiuwen.harness.rails import AskUserRail
        from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
        from openjiuwen.harness.workspace.workspace import Workspace

        from .jiuwen_assets import build_hooks_rail

        context = cfg["context"]
        model = init_model(
            provider="OpenAI",
            model_name=cfg["model"],
            api_key=cfg["api_key"],
            api_base=cfg["base_url"],
            max_tokens=context["max_output_tokens"],
            # 只作上下文引擎的窗口元数据（压缩阈值按它算），不随请求发给网关
            context_window=context["max_context_tokens"],
            timeout=300.0,
        )
        # 框架私有工件区与项目 git 工作区分离（官方 cwd/workspace 分层）
        shadow = shadow_dir(self._ctx.project_id)
        shadow.mkdir(parents=True, exist_ok=True)
        # 文件沙箱三根：项目区 + 私有工件区 + 资产目录（工作区 .claude/CLAUDE.md
        # 是指向资产的符号链接，沙箱按 resolve 后路径判定，不加资产根则读不穿；
        # 资产写保护由 toolset_write_guard 经 ClaudeHooksRail 承担）
        sysop_id = f"onto-jiuwen-{self._ctx.project_id}"
        if Runner.resource_mgr.get_sys_operation(sysop_id) is None:
            Runner.resource_mgr.add_sys_operation(
                SysOperationCard(
                    id=sysop_id,
                    mode=OperationMode.LOCAL,
                    work_config=LocalWorkConfig(
                        shell_allowlist=None,
                        restrict_to_sandbox=True,
                        sandbox_root=[
                            str(self._ctx.cwd),
                            str(shadow),
                            str(self._loader.assets_dir),
                        ],
                    ),
                )
            )
        sys_operation = Runner.resource_mgr.get_sys_operation(sysop_id)
        tracker = _call_usage_rail(self._record_call_usage)
        progress_rail = build_progress_rail(_TOOL_NAME_MAP)
        system_prompt = self._loader.system_prompt()
        subagent_root = shadow / "sub_agents"
        rails = [
            tracker,
            progress_rail,
            SysOperationRail(),
            AskUserRail(),
            build_hooks_rail(self._loader, self._ctx.cwd),
        ]
        gp_spec = _general_purpose_spec(
            model=model,
            system_prompt=system_prompt,
            rails=[*rails, _context_processor_rail(context["trigger_ratio"])],
            workspace_root=subagent_root,
            sys_operation=sys_operation,
        )
        agent = create_deep_agent(
            model=model,
            # checkpoint 按（会话 id, agent id）存取：agent id 默认每个实例随机，
            # 重建的实例读不到上一个实例的存档，故按项目固定
            card=AgentCard(
                id=f"onto-jiuwen-{self._ctx.project_id}",
                name="deep_agent",
                description="DeepAgent instance",
            ),
            system_prompt=system_prompt,
            rails=[*rails, _context_processor_rail(context["trigger_ratio"])],
            workspace=Workspace(root_path=str(shadow), language="cn"),
            sys_operation=sys_operation,
            enable_task_loop=True,
            # 命令资产的 Task 派遣模式：读 agents/*.md 内容作为子代理 prompt
            add_general_purpose_agent=True,
            # 子代理工件区与主 agent 分开，否则子代理会挂死（见 _general_purpose_spec）
            subagents=[gp_spec],
            max_iterations=config.JIUWEN_MAX_ITERATIONS,
            language="cn",
            # tool_call chunk 无调用 id，串行执行保证桥接层 FIFO 配对正确
            parallel_tool_calls=False,
            cwd=str(self._ctx.cwd),
            project_root=str(self._ctx.cwd),
        )
        # 每次派发再换一个独立子目录，避免第二个子任务撞上第一个留下的文件锁
        _bind_subagent_workspace_rotation(agent, gp_spec, subagent_root)
        return agent, tracker

    async def send(self, text: str) -> None:
        text = self._loader.expand_command(text)
        if self._turn_task and not self._turn_task.done():
            self._buffered.append(text)
            return
        self._turn_task = asyncio.create_task(self._run_turn(text))

    async def interrupt(self) -> None:
        agent = self.client
        if agent is not None:
            try:
                result = agent.abort()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        task = self._turn_task
        if task and not task.done():
            # abort 会取消轮任务，轮任务以 CancelledError 收尾；asyncio.wait 只等结束、不抛出任务的异常，
            # 否则 CancelledError 冒到接口层，打断请求返回 500
            await asyncio.wait({task}, timeout=5)
            if not task.done():
                task.cancel()

    async def disconnect(self) -> None:
        if self._progress_tracker is not None:
            await self._progress_tracker.finish_all("stopped")
        self._closed = True
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        agent = self.client
        self.client = None
        if agent is not None:
            try:
                result = agent.cleanup_task_resources()
                if asyncio.iscoroutine(result):  # openjiuwen 0.1.19 起是协程
                    await result
            except Exception:
                pass

    async def kill(self) -> None:
        """硬终止：取消轮任务并释放实例（阻塞在 interaction 等待中的轮也能停）。"""
        await self.disconnect()

    def config_stale(self) -> bool:
        return bool(self._fingerprint) and self._fingerprint != _fingerprint(
            model_config(self._ctx.model_owner)
        )

    def context_limits(self) -> tuple[int | None, int | None]:
        return self._context_window, self._compact_at

    # ---- 事件流 ----

    async def events(self):
        while True:
            event = await self._queue.get()
            yield event
            if event.get("type") == "_engine_end":
                return

    def _usage_snapshot(self) -> dict:
        tracker = self._tracker
        return {
            "input_tokens": int(getattr(tracker, "total_input_tokens", 0) or 0),
            "output_tokens": int(getattr(tracker, "total_output_tokens", 0) or 0),
        }

    def _record_call_usage(self, input_tokens: int, output_tokens: int) -> None:
        """一次模型调用（含子代理）的用量即时下发，会话层落盘后就计入项目用量。"""
        self._queue.put_nowait(
            {
                "type": "model_usage",
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            }
        )

    def _turn_result(
        self, subtype: str, started: float, before: dict, is_error: bool = False
    ) -> dict:
        """一轮终态。usage 是本轮开始以来的增量（打断、出错也照实给），供单轮展示；
        项目用量已由逐次的 model_usage 计入，usage_per_call 标明统计时不再累加。"""
        after = self._usage_snapshot()
        return {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "duration_ms": int((time.time() - started) * 1000),
            "total_cost_usd": None,  # 无单价来源，成本以 tokens 呈现
            "num_turns": 1,
            "usage": {key: after[key] - before[key] for key in after},
            "usage_per_call": True,
        }

    def _track_context(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "assistant_message" and event.get("context_tokens"):
            self._context_used = event["context_tokens"]
        elif kind == "context_compacted" and event.get("post_tokens"):
            self._context_used = event["post_tokens"]

    def _context_usage_event(self) -> dict | None:
        """本轮结束（result 之前）的上下文用量事件，格式与稳定引擎一致；还没有用量时不发。"""
        if not self._context_used:
            return None
        return {
            "type": "context_usage",
            "used": self._context_used,
            "window": self._context_window,
            "compact_at": self._compact_at,
        }

    async def _put_turn_events(self, events: list[dict]) -> None:
        for event in events:
            self._track_context(event)
            await self._queue.put(event)

    async def _run_turn(self, text: str) -> None:
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.session import InteractiveInput

        started = time.time()
        before = self._usage_snapshot()
        if self._progress_tracker is None:
            self._progress_tracker = JiuwenProgressTracker(self._queue.put)
        try:
            queries: list[Any] = [text]
            while queries:
                query = queries.pop(0)
                round_query: Any = query
                while True:
                    bridge = _ChunkBridge()
                    pending: list[tuple[str, Any]] = []
                    with progress_scope(self._progress_tracker):
                        stream = Runner.run_agent_streaming(
                            self.client, {"query": round_query}, session=self._sid
                        )
                        async for chunk in stream:
                            events, interaction = bridge.feed(chunk)
                            await self._put_turn_events(events)
                            if interaction:
                                pending.append(interaction)
                    await self._put_turn_events(bridge.finish())
                    if not pending:
                        # 一轮真正走完：仍挂着的子任务卡收成"已停止"。
                        # 有待确认 interaction 时流只是被挂起，下一轮会接着跑，
                        # 这里不能把在飞的子任务判成停止
                        await self._progress_tracker.finish_all("stopped")
                        break
                    # 此刻存档带着挂起中断：等确认期间若进程重启，下次连接不能用它续接
                    self._ctx.clear_resume_token()
                    ii = InteractiveInput()
                    for iid, request in pending:
                        tool_name, tool_input = _interaction_to_permission(request)
                        decision = await self._ctx.request_permission(
                            tool_name, tool_input, None, None
                        )
                        _fill_interactive_input(ii, iid, request, decision)
                    round_query = ii
                # 本轮正常收尾，Runner 已把会话状态落盘
                self._ctx.save_resume_token(self._resume_token)
                context_usage = self._context_usage_event()
                if context_usage:
                    await self._queue.put(context_usage)
                await self._queue.put(self._turn_result("success", started, before))
                started = time.time()
                before = self._usage_snapshot()
                if self._buffered:
                    queries.append(self._buffered.pop(0))
        except asyncio.CancelledError:
            await self._progress_tracker.finish_all("stopped")
            context_usage = self._context_usage_event()
            if context_usage:
                self._queue.put_nowait(context_usage)
            # 打断：以 interrupted 终态收轮，会话保持可用
            self._queue.put_nowait(self._turn_result("interrupted", started, before))
            raise
        except Exception as exc:
            await self._progress_tracker.finish_all("failed")
            if self._closed:
                return
            self._ctx.log_error("session", str(exc))
            friendly = (
                config_error_for(self._ctx.model_owner)
                if any(
                    marker in str(exc).lower()
                    for marker in ("401", "unauthorized", "api key", "authentication")
                )
                else FRIENDLY_ERROR
            )
            # 先以出错终态收轮，带上出错前已用的 token，再结束引擎
            await self._queue.put(
                self._turn_result("error_during_execution", started, before, is_error=True)
            )
            await self._queue.put(
                {"type": "_engine_end", "reason": "error", "error": friendly}
            )
        finally:
            await self._progress_tracker.finish_all("stopped")
