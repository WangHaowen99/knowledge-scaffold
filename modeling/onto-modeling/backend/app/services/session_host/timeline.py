"""对话时间线（契约 1.11、第 4.2 节）：引擎事件 → 宿主记录（JSONL 历史的一行）→ 对话流事件。

- 历史一行一条宿主记录，行号就是记录的 `seq`；对外的事件 id 是 `seq + 1`（从 1 开始，`after=0` 表示
  从头回放，`after=n` 补发 id 大于 n 的事件）。
- 一条记录至多投影成一条对话流事件，所以引擎的一条消息在宿主这里先拆开：助手正文、每个工具调用、
  「工作过程」组各写一条；有的记录只给内部用（逐次用量、子代理的内部消息），投影为空，id 就空着。
- 内部步骤（工具调用原文）只进 `tool.activity`（internal）；用户看到的进展由宿主按执行记录的阶段与
  子代理事件归并成业务话的 `progress`（契约第 9 章建议 4）。
- 旧版历史里的记录照样能投影，缺的字段给默认值。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from .views import STATE, iso

# 瞬时事件：不带 id、不回放（契约 1.11）
TRANSIENT = frozenset({"message.delta", "context.usage", "ping"})
# 宿主自己产出、记录里直接存契约载荷（payload）的类型
PASSTHROUGH = {
    "notice": "notice",
    "day_marker": "day.marker",
    "card": "card",
    "card_updated": "card.updated",
    "suggestions": "suggestions.updated",
    "question_asked": "question.asked",
    "question_answered": "question.answered",
    "question_withdrawn": "question.withdrawn",
    # 插话的去向、代存的下一条命令（契约 EC-10 补充）
    "message_delivery": "message.delivery",
    "next_updated": "next.updated",
}
STEPS_MAX = 30
ASK_TOOL = "AskUserQuestion"
CUSTOM_OPTION_LABEL = "其他，我来说"

# 旧版中断原因 → 契约 EC-05 的 by
_INTERRUPT_BY = {
    "user": "user", "ops": "ops", "ops_disconnect": "ops", "restart": "restart", "stale": "restart",
    "engine_exit": "engine_exit", "shutdown": "shutdown", "recycled": "restart",
}
_SUBAGENT_STATUS = {"completed": "done", "failed": "failed", "stopped": "failed", "killed": "failed"}
# 稳定引擎子代理的步骤上限（内测引擎在 jiuwen_progress 封顶，同一个数）；任务说明只留前 2000 字
SUBAGENT_STEPS_MAX = 200
SUBAGENT_BRIEF_MAX = 2000
_SUBAGENT_TEXT_MAX = 300
_SUBAGENT_INPUT_KEYS = ("file_path", "path", "pattern", "command", "description", "url", "query")
_GENERIC_AGENT_TYPES = frozenset({"", "general-purpose"})


# 平台派给子代理的说明里写着读哪个子代理文档（.claude/agents/<名>.md）：按它给中文名
_AGENT_LABELS = {
    "research-agent": "资料分析", "composition-analysis-agent": "规则组合分析",
    "question-extraction-agent": "业务问题梳理", "extract-objects-agent": "对象提取",
    "extract-operation-agent": "操作提取", "rule-metric-extraction-agent": "规则与指标提取",
    "coverage-check-agent": "覆盖检查", "rule-check-agent": "规则检查", "rule-fix-agent": "规则修复",
    "semantic-check-agent": "语义检查", "semantic-fix-agent": "语义修复", "update-impact-agent": "影响分析",
    "update-apply-agent": "按清单修改", "codegen-review-agent": "脚本复核", "script-candidate-agent": "脚本候选扫描",
    "knowledge-conflict-agent": "知识冲突检查", "mock-data-agent": "生成测试数据", "trace-agent": "场景推演",
}
_AGENT_DOC = re.compile(r"\.claude/agents/([A-Za-z0-9_-]+)\.md")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def has_cjk(text) -> bool:
    return bool(_CJK.search(str(text or "")))


def subagent_title(description, brief=None, agent_type=None) -> str:
    """子任务的名称：中文的简短描述 → 说明里点名的子代理文档的中文名 → 中文的任务说明第一行 → 子代理类型（中文、
    不是通用类型时）；都没有时叫「子任务」。模型写的英文描述不直接给用户看（端到端验证）。"""
    text = " ".join(str(description or "").split())
    if text and has_cjk(text):
        return text[:60]
    for name in _AGENT_DOC.findall(str(brief or "")) or [str(agent_type or "").strip()]:
        if name in _AGENT_LABELS:
            return _AGENT_LABELS[name]
    for line in str(brief or "").splitlines():
        line = line.strip().lstrip("#>*-·•").strip()
        if line:
            if has_cjk(line) and not _AGENT_DOC.search(line):
                return line[:40] + ("…" if len(line) > 40 else "")
            break
    kind = str(agent_type or "").strip()
    return kind[:60] if kind not in _GENERIC_AGENT_TYPES and has_cjk(kind) else "子任务"


def external_id(record: dict) -> int | None:
    seq = record.get("seq")
    return int(seq) + 1 if isinstance(seq, int) else None


def message_id(record: dict) -> str:
    """用户消息的 ID：宿主写入时就定下；旧记录按对外 id 补（与 S-07 返回的一致）。"""
    return record.get("message_id") or f"m-{external_id(record) or 0}"


# ---------------------------------------------------------------------------
# 工具调用 → 业务话
# ---------------------------------------------------------------------------


def _target_label(path) -> str:
    """工具调用里的路径（绝对或相对）→ 业务名；认不出、在隐藏目录（助手的说明、运行目录）里的给空串。"""
    from .. import workspace_labels

    parts = [part for part in str(path or "").replace("\\", "/").split("/") if part]
    if any(part.startswith(".") for part in parts[:-1]):
        return ""
    for index in range(len(parts)):
        label = workspace_labels.path_label("/".join(parts[index:]))
        if label:
            return label
    return ""


# 平台内部的状态维护（命令锁、进度、阶段）和助手读自己的说明、平台运行目录：不进给用户看的工作过程（端到端验证）
_INTERNAL_SCRIPTS = ("state_helper.py",)
_INTERNAL_DIRS = (".ontology-state", ".onto", ".claude")
# 工作区脚本 → 步骤说法（模型给 Bash 写的描述常是英文、说内部机制，按脚本说）
_SCRIPT_LABELS = {
    "knowledge_helper": "加载业务知识", "source_extract": "读取资料", "research_checker": "核验资料分析报告",
    "research_review": "复核资料分析", "ontology_checker": "检查知识包", "impact_analysis": "分析影响范围",
    "script_verify": "验证脚本", "script_descriptor": "生成脚本说明", "script_contract": "检查脚本契约",
    "script_argument_shape": "检查脚本入参", "candidate_revision": "整理修订", "report_reconcile": "核对检查报告",
    "evidence": "检索系统证据", "incremental_scope": "整理增量范围", "incremental_chain": "分析传播链",
    "incremental_check": "核对传播", "table_profile": "分析数据表", "excel_context": "读取表格",
    "blueprint": "整理建模蓝图", "verify_artifacts": "核验产物", "query": "查询知识包", "field_semantics": "核对字段口径",
    "operation_relations": "整理操作关系", "industry_rules": "检查行业规则", "check_diagrams": "检查流程图",
    "check_diagram_excel_alignment": "核对流程图与表格", "summarize_diagram": "读取流程图", "batch_summarize": "汇总资料",
    "trace_index": "整理场景", "trace_inputs": "准备推演输入", "seed_example": "准备示例", "onto_metamodel": "解析知识包",
    "solver_evaluator_contract": "检查求解契约", "check_eval_data": "检查评测数据",
    "check_eval_generalization": "检查评测泛化", "init_flowchart_resources": "准备流程图",
}
_SCRIPT_RE = re.compile(r"\.claude/scripts/([A-Za-z0-9_]+)\.py")


def _path_parts(value) -> list[str]:
    return [part for part in str(value or "").replace("\\", "/").split("/") if part]


def internal_step(tool: str, data) -> bool:
    """平台内部的一步：用户看不懂、也不用知道（命令锁、进度快照、读助手自己的命令说明）。"""
    data = data if isinstance(data, dict) else {}
    if tool == "Bash":
        command = str(data.get("command") or "")
        return any(name in command for name in _INTERNAL_SCRIPTS) or ".ontology-state" in command \
            or ".onto/run" in command
    if tool in ("Read", "Glob", "Grep", "LS"):
        target = data.get("file_path") or data.get("path") or data.get("pattern") or ""
        return any(part in _INTERNAL_DIRS for part in _path_parts(target))
    return False


def _bash_text(data: dict) -> str:
    """命令行的一步：认得出的工作区脚本按脚本说；模型写的中文描述照用；英文描述不给用户看。"""
    command = str(data.get("command") or "")
    scripts = _SCRIPT_RE.findall(command)
    label = next((_SCRIPT_LABELS[name] for name in scripts if name in _SCRIPT_LABELS), "")
    if label:
        return label
    description = str(data.get("description") or "").strip()
    if description and has_cjk(description):
        return description[:60]
    return "运行脚本"


def step_text(tool: str, data) -> str:
    """一次工具调用在「工作过程」里的说法：按业务名说（读取对象「门店」、读取资料分析报告），不出路径、文件名和命令原文。"""
    data = data if isinstance(data, dict) else {}
    path = data.get("file_path") or data.get("path") or data.get("notebook_path") or ""
    target = _target_label(path)
    if tool == "Read":
        return f"读取{target}" if target else "读取文件" if path else "读取资料"
    if tool == "Write":
        return f"写入{target}" if target else "写入文件"
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"修改{target}" if target else "修改文件"
    if tool == "Bash":
        return _bash_text(data)
    if tool in ("Glob", "Grep", "LS"):
        return "查找相关文件"
    if tool in ("Task", "Agent"):
        description = str(data.get("description") or "").strip()
        return f"派出子任务「{description[:40]}」" if description and has_cjk(description) else "派出子任务"
    if tool == "TodoWrite":
        return "整理待办"
    if tool == "WebSearch":
        return "联网搜索"
    if tool == "WebFetch":
        return "读取网页"
    if tool == ASK_TOOL:
        return "向你提问"
    if tool == "Skill":
        return "调用技能"
    return "执行操作"


def _duration_text(ms: int | None) -> str:
    """做完的一步的耗时。工具本身多在几十到几百毫秒（时间花在步骤之间的模型思考上），不满 1 秒不写，免得满屏「0 秒」。"""
    if ms is None:
        return "进行中"
    if ms < 1000:
        return ""
    seconds = int(round(ms / 1000))
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


# ---------------------------------------------------------------------------
# 一轮的宿主状态：消息 ID、工作过程组
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    turn_id: str
    title: str = "工作过程"
    started: float = field(default_factory=time.time)
    group_revision: int = 0
    steps: list = field(default_factory=list)
    hidden_steps: int = 0
    open_steps: dict = field(default_factory=dict)  # 工具调用 ID → (步骤 ID, 开始时间)
    tool_names: dict = field(default_factory=dict)
    stream_message_id: str | None = None
    had_text: bool = False
    state: str = "running"
    # 工作过程按时间分段：助手每说一段话，之前的步骤收成一段，之后的另起一段（组号带段号）
    segment: int = 0
    pending_subagents: dict = field(default_factory=dict)  # 派发调用还没到的子代理：工具调用 ID → 开始事件
    sub_steps: dict = field(default_factory=dict)  # (子任务 ID, 步骤 ID) → 步骤说法（收尾事件不带参数）
    sub_groups: dict = field(default_factory=dict)  # 子任务 ID → 它挂在哪一段（之后分段了也不挪）
    sub_titles: dict = field(default_factory=dict)  # 子任务 ID → 名称（开始时按任务说明定下，之后的事件沿用）
    sub_counts: dict = field(default_factory=dict)  # 子任务 ID → 已记的工具步骤数
    sub_briefs: dict = field(default_factory=dict)  # 派发调用 ID → 稳定引擎 Task 调用的 {description, brief}
    sub_tasks: dict = field(default_factory=dict)  # 派发调用 ID → 稳定引擎的子任务（子代理消息按派发调用归卡）
    sub_tool_steps: dict = field(default_factory=dict)  # 稳定引擎子代理的工具调用 ID → 派发调用 ID
    internal_tools: set = field(default_factory=set)  # 不进工作过程的平台内部步骤（工具调用 ID、子任务步骤 ID）

    def sub_group(self, event: dict, attach: bool = False) -> str:
        task_id = str(event.get("task_id") or "")
        if attach or task_id not in self.sub_groups:
            self.sub_groups[task_id] = self.group_id
        return self.sub_groups[task_id]

    @classmethod
    def new(cls, title: str = "") -> "Turn":
        return cls(turn_id=f"t-{uuid.uuid4().hex[:10]}", title=title or "工作过程")

    @property
    def group_id(self) -> str:
        return f"g-{self.turn_id}" if not self.segment else f"g-{self.turn_id}-{self.segment}"

    def seal(self) -> dict | None:
        """助手接着说话：前面做完的步骤收成一段（返回它的最后一版），后面的步骤另起一段、排在这段话之后。
        还有没做完的步骤（如子代理在跑）时不分段。"""
        if not self.steps or self.open_steps:
            return None
        self.state = "done"
        record = self.progress_record()
        self.segment += 1
        self.steps, self.hidden_steps, self.group_revision = [], 0, 0
        self.started, self.state = time.time(), "running"
        return record

    def add_step(self, key: str | None, text: str, state: str = "running") -> None:
        step_id = f"s{len(self.steps) + self.hidden_steps + 1}"
        self.steps.append({"id": step_id, "text": text, "meta": "进行中" if state == "running" else "",
                           "state": state, "duration_ms": None})
        if key:
            self.open_steps[key] = (step_id, time.monotonic())
        if len(self.steps) > STEPS_MAX:
            # 只留最近的；前面折掉的计数放进说明
            dropped = next((index for index, step in enumerate(self.steps) if step["state"] != "running"), 0)
            self.steps.pop(dropped)
            self.hidden_steps += 1

    def close_step(self, key: str, failed: bool = False) -> bool:
        found = self.open_steps.pop(key, None)
        if found is None:
            return False
        step_id, started = found
        for step in self.steps:
            if step["id"] == step_id:
                duration = int((time.monotonic() - started) * 1000)
                step.update(state="failed" if failed else "done", duration_ms=duration,
                            meta="出错" if failed else _duration_text(duration))
                return True
        return False

    def finish(self, state: str) -> None:
        self.state = state
        for key in list(self.open_steps):
            self.close_step(key, failed=state == "failed")

    def group(self) -> dict:
        done = sum(1 for step in self.steps if step["state"] == "done") + self.hidden_steps
        running = self.state == "running"
        desc = {
            "running": f"进行中 · 已完成 {done} 步",
            "done": f"已完成 {done} 步",
            "failed": "这一轮出错了",
            "interrupted": f"已打断 · 完成了 {done} 步",
            "waiting": f"等你回答 · 已完成 {done} 步",
        }.get(self.state, "")
        duration = None if running else int((time.time() - self.started) * 1000)
        return {
            "group_id": self.group_id,
            "title": self.title,
            "desc": desc,
            "state": self.state,
            "started_at": iso(self.started),
            "duration_ms": duration,
            "collapsed": not running,
            "steps": [dict(step) for step in self.steps],
        }

    def progress_record(self) -> dict:
        self.group_revision += 1
        return {"type": "progress", "revision": self.group_revision, "group": self.group()}


def ingest(turn: Turn, event: dict) -> list[tuple[dict, bool]]:
    """引擎的一条平台事件 → 宿主要写的记录（记录, 是否瞬时）。"""
    kind = event.get("type")
    if kind == "stream_delta":
        if turn.stream_message_id is None:
            turn.stream_message_id = f"a-{uuid.uuid4().hex[:10]}"
        return [({**event, "message_id": turn.stream_message_id}, True)]
    if kind == "assistant_message":
        return _assistant(turn, event)
    if kind == "tool_results":
        records: list[tuple[dict, bool]] = []
        changed = False
        for block in event.get("blocks") or []:
            tool_use_id = block.get("tool_use_id")
            parent = turn.sub_tool_steps.pop(str(tool_use_id or ""), None)
            if parent is not None:  # 稳定引擎子代理自己的一步做完了
                records += _claude_sub_step_done(turn, parent, str(tool_use_id), bool(block.get("is_error")))
                continue
            tool = turn.tool_names.get(tool_use_id, "")
            failed = bool(block.get("is_error"))
            records.append(({"type": "tool_activity", "tool_use_id": tool_use_id, "tool": tool,
                             "summary": step_text(tool, {}), "state": "failed" if failed else "done"}, False))
            changed = turn.close_step(f"tool:{tool_use_id}", failed) or changed
        if changed:
            records.append((turn.progress_record(), False))
        return records
    if kind == "subagent_started":
        event = _subagent_started(turn, event)
        tool_use_id = str(event.get("tool_use_id") or "")
        if not tool_use_id or tool_use_id in turn.tool_names:
            return [({**event, "group_id": turn.sub_group(event, attach=True)}, False)]
        # 内测引擎先报子代理开始、后报带派发调用的那条消息：等那条消息到了再发，
        # 子代理卡就排在这条消息里的话后面
        turn.pending_subagents[tool_use_id] = event
        return []
    if kind == "subagent_finished":
        records = _flush_subagent(turn, event.get("tool_use_id"))
        records.append(({**event, **_sub_fields(turn, event), "group_id": turn.sub_group(event)}, False))
        return records
    if kind == "subagent_activity":
        records = _flush_subagent(turn, event.get("tool_use_id"))
        return records + _subagent_activity(turn, event)
    return [(event, False)]


def _subagent_started(turn: Turn, event: dict) -> dict:
    """子任务开始：补上派发时的任务说明（稳定引擎从 Task 调用参数取），定下名称。"""
    dispatch = turn.sub_briefs.get(str(event.get("tool_use_id") or "")) or {}
    brief = str(event.get("brief") or dispatch.get("brief") or "")[:SUBAGENT_BRIEF_MAX]
    description = event.get("description") or dispatch.get("description") or ""
    task_id = str(event.get("task_id") or "")
    turn.sub_titles[task_id] = subagent_title(description, brief, event.get("agent_type"))
    turn.sub_counts.setdefault(task_id, 0)
    if event.get("tool_use_id"):
        turn.sub_tasks[str(event["tool_use_id"])] = {key: event.get(key) for key in ("task_id", "description",
                                                                                     "agent_type")}
    return {**event, "description": description, "brief": brief, **_sub_fields(turn, event)}


def _sub_fields(turn: Turn, event: dict) -> dict:
    """每条子任务记录都带上名称与已做的步骤数：投影是逐条做的，读不到开始那条里的任务说明。"""
    task_id = str(event.get("task_id") or "")
    fields = {"step_count": turn.sub_counts.get(task_id, 0)}
    if task_id in turn.sub_titles:
        fields["title"] = turn.sub_titles[task_id]
    return fields


def _subagent_activity(turn: Turn, event: dict) -> list[tuple[dict, bool]]:
    """子代理的一步。「思考中」是瞬时事件：只刷新界面上想了多久，不进历史。平台内部的步骤不记。"""
    group = turn.sub_group(event)
    if event.get("kind") == "thinking":
        return [({**event, **_sub_fields(turn, event), "group_id": group}, True)]
    if event.get("kind") == "tool":
        step_key = f"{event.get('task_id') or ''}:{event.get('step_id') or ''}"
        if not event.get("status") and internal_step(str(event.get("tool") or ""), event.get("input")):
            turn.internal_tools.add(step_key)
            return []
        if event.get("status") and step_key in turn.internal_tools:
            turn.internal_tools.discard(step_key)
            return []
    if event.get("kind") == "tool" and not event.get("status"):
        task_id = str(event.get("task_id") or "")
        turn.sub_counts[task_id] = turn.sub_counts.get(task_id, 0) + 1
    return [({**event, **_sub_fields(turn, event), "group_id": group, "step_text": _sub_step_text(turn, event)},
             False)]


def _claude_sub_steps(turn: Turn, event: dict) -> list[tuple[dict, bool]]:
    """稳定引擎子代理的消息（带派发调用 ID）→ 子任务的步骤：工具调用一步一条，说的话一条。"""
    parent = str(event.get("parent_tool_use_id") or "")
    task = turn.sub_tasks.get(parent)
    if task is None:
        return []  # 子任务开始还没报：只留档
    base = {"type": "subagent_activity", "tool_use_id": parent, **task}
    task_id = str(task.get("task_id") or "")
    records: list[tuple[dict, bool]] = []
    for block in event.get("blocks") or []:
        used = turn.sub_counts.get(task_id, 0)
        if used > SUBAGENT_STEPS_MAX:
            break
        if used == SUBAGENT_STEPS_MAX:
            turn.sub_counts[task_id] = used + 1
            records += _subagent_activity(turn, {**base, "step_id": f"{parent}-truncated", "kind": "text",
                                                 "text": "步骤较多，后续过程已省略"})
            break
        if block.get("kind") == "tool_use":
            step_id = str(block.get("tool_use_id") or "")
            turn.sub_tool_steps[step_id] = parent
            records += _subagent_activity(turn, {**base, "step_id": step_id, "kind": "tool",
                                                 "tool": str(block.get("tool") or ""),
                                                 "input": _sub_input(block.get("input"))})
        elif block.get("kind") == "text" and str(block.get("text") or "").strip():
            text = str(block["text"]).strip()
            records += _subagent_activity(turn, {
                **base, "step_id": f"{parent}-say-{uuid.uuid4().hex[:6]}", "kind": "text",
                "text": text[:_SUBAGENT_TEXT_MAX] + ("…" if len(text) > _SUBAGENT_TEXT_MAX else "")})
    return records


def _claude_sub_step_done(turn: Turn, parent: str, step_id: str, failed: bool) -> list[tuple[dict, bool]]:
    task = turn.sub_tasks.get(parent)
    if task is None:
        return []
    return _subagent_activity(turn, {"type": "subagent_activity", "tool_use_id": parent, **task,
                                     "step_id": step_id, "kind": "tool", "status": "error" if failed else "ok"})


def _sub_input(data) -> dict:
    """子代理工具参数只留说步骤用的几个键：写文件的参数可能带整段正文，不进历史。"""
    if not isinstance(data, dict):
        return {}
    return {key: data[key][:200] for key in _SUBAGENT_INPUT_KEYS if isinstance(data.get(key), str) and data[key].strip()}


def _flush_subagent(turn: Turn, tool_use_id) -> list[tuple[dict, bool]]:
    """子代理的过程先于派发调用到了（不该发生的次序）：先把它的开始补上。"""
    started = turn.pending_subagents.pop(str(tool_use_id or ""), None)
    return [({**started, "group_id": turn.sub_group(started, attach=True)}, False)] if started else []


def _sub_step_text(turn: Turn, event: dict) -> str:
    """子代理一步的说法：开始的事件带参数，按它说；收尾的事件不带，沿用开始时的说法。"""
    if event.get("kind") == "text":
        return str(event.get("text") or "").strip()
    key = (str(event.get("task_id") or ""), str(event.get("step_id") or ""))
    if event.get("status") and key in turn.sub_steps:
        return turn.sub_steps.pop(key)
    text = step_text(str(event.get("tool") or ""), event.get("input"))
    if not event.get("status"):
        turn.sub_steps[key] = text
    return text


_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})


def _assistant(turn: Turn, event: dict) -> list[tuple[dict, bool]]:
    """助手的话一律进正文，前面做完的步骤收成一段；派出的子任务在时间线上单独成卡，不进工作过程。"""
    if event.get("subagent") or event.get("parent_tool_use_id"):
        # 子代理的内部消息：原样只留档；其中的工具调用和说的话另记成子任务的步骤
        return [(event, False)] + _claude_sub_steps(turn, event)
    blocks = event.get("blocks") or []
    text_blocks = [block for block in blocks if block.get("kind") in ("text", "thinking")]
    tool_blocks = [block for block in blocks if block.get("kind") == "tool_use"]
    records: list[tuple[dict, bool]] = []
    has_text = any(block.get("kind") == "text" and str(block.get("text") or "").strip() for block in text_blocks)
    dispatching = any(str(block.get("tool") or "") in _SUBAGENT_TOOLS for block in tool_blocks)
    if has_text or dispatching:
        sealed = turn.seal()
        if sealed is not None:
            records.append((sealed, False))
    if text_blocks:
        record = {key: value for key, value in event.items() if key != "blocks"}
        record["blocks"] = text_blocks
        if has_text:
            record["message_id"] = turn.stream_message_id or f"a-{uuid.uuid4().hex[:10]}"
            record["continued"] = turn.had_text
            turn.stream_message_id = None
            turn.had_text = True
        records.append((record, False))
    stepped = False
    for block in tool_blocks:
        tool = str(block.get("tool") or "")
        tool_use_id = block.get("tool_use_id")
        turn.tool_names[tool_use_id] = tool
        if tool in _SUBAGENT_TOOLS and isinstance(block.get("input"), dict):
            data = block["input"]
            turn.sub_briefs[str(tool_use_id)] = {
                "description": str(data.get("description") or "").strip(),
                "brief": str(data.get("prompt") or data.get("task_description") or "").strip()[:SUBAGENT_BRIEF_MAX]}
        text = step_text(tool, block.get("input"))
        records.append(({"type": "tool_activity", "tool_use_id": tool_use_id, "tool": tool, "summary": text,
                         "state": "started"}, False))
        if internal_step(tool, block.get("input")):
            turn.internal_tools.add(str(tool_use_id or ""))
        elif tool not in _SUBAGENT_TOOLS:
            turn.add_step(f"tool:{tool_use_id}", text)
            stepped = True
    if stepped:
        records.append((turn.progress_record(), False))
    for block in tool_blocks:
        started = turn.pending_subagents.pop(str(block.get("tool_use_id") or ""), None)
        if started is not None:
            records.append(({**started, "group_id": turn.sub_group(started, attach=True)}, False))
    return records


# ---------------------------------------------------------------------------
# 决策卡（AskUserQuestion → Question，契约 4.6）
# ---------------------------------------------------------------------------


def question_ids(request_id: str, count: int) -> list[str]:
    """一个提问里只有一题时问题 ID 就是确认请求 ID（与 B2 一致）；多题时每题一个。"""
    return [request_id] if count <= 1 else [f"{request_id}-{index}" for index in range(count)]


def build_questions(request_id: str, tool_input: dict, *, conversation_id: str, exec_id: str | None = None,
                    step_label: str = "") -> list[dict]:
    raw = (tool_input or {}).get("questions") or []
    if not isinstance(raw, list) or not raw:
        raw = [{"question": "请确认", "options": []}]
    ids = question_ids(request_id, len(raw))
    questions = []
    for index, item in enumerate(raw):
        item = item if isinstance(item, dict) else {"question": str(item)}
        options = []
        for position, option in enumerate(item.get("options") or []):
            option = option if isinstance(option, dict) else {"label": str(option)}
            label = str(option.get("label") or "")
            options.append({
                "id": f"{index}.{position}", "key": chr(65 + position) if position < 26 else str(position + 1),
                "label": label, "consequence": option.get("description") or None,
                "recommended": "推荐" in label or "recommended" in label.lower(),
                "preview": None, "custom": False,
            })
        options.append({"id": f"{index}.custom", "key": chr(65 + len(options)) if len(options) < 26 else "*",
                        "label": CUSTOM_OPTION_LABEL, "consequence": None, "recommended": False, "preview": None,
                        "custom": True})
        header = str(item.get("header") or "").strip()
        questions.append({
            "question_id": ids[index],
            "kind": "decision",
            "conversation_id": conversation_id,
            "exec_id": exec_id,
            "step_label": step_label,
            "eyebrow": "需要你决定" + (f" · {step_label}" if step_label else ""),
            "title": str(item.get("question") or "请确认"),
            "background_md": header,
            "anchors": [],
            "options": options,
            "multi": bool(item.get("multiSelect")),
            "allow_custom": True,
            "custom_placeholder": "说说你的想法",
            "state": "open",
            "answer": None,
            "inbox_item_id": f"decision:{ids[index]}",
            "affected": [],
        })
    return questions


def answer_summary(question: dict, answer: dict) -> str:
    """「选了 A：…」一行。"""
    chosen = [answer["option_id"]] if answer.get("option_id") else list(answer.get("option_ids") or [])
    by_id = {option["id"]: option for option in question.get("options") or []}
    parts = [f"{by_id[option]['key']}：{by_id[option]['label']}" for option in chosen
             if option in by_id and not by_id[option].get("custom")]
    custom = str(answer.get("custom_text") or "").strip()
    if custom:
        parts.append(f"你说：{custom}")
    return "选了 " + "；".join(parts) if parts else "已回答"


# ---------------------------------------------------------------------------
# 确认卡（契约 EC-30）
# ---------------------------------------------------------------------------


def permission_description(tool: str, data) -> str:
    return {"Bash": "助手想运行一个脚本", "Write": "助手想写入一个文件", "Edit": "助手想修改一个文件",
            "MultiEdit": "助手想修改一个文件"}.get(tool) or f"助手想{step_text(tool, data)}"


def details_text(tool: str, data) -> str:
    try:
        body = json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        body = str(data)
    return f"{tool}\n{body}"[:2000]


# ---------------------------------------------------------------------------
# 这一轮改了什么（workspace_changes → round_changes 卡）
# ---------------------------------------------------------------------------

_CHANGE = {"added": "add", "modified": "mod", "deleted": "del"}


def round_changes_card(record: dict) -> dict:
    from ...kernel.changesets import service as changesets
    from .. import workspace_labels

    items = []
    for bucket in ("added", "modified", "deleted"):
        for path in record.get(bucket) or []:
            info = workspace_labels.describe(path) or {"group": "other", "target": {"type": "file", "key": path,
                                                                                      "title": "", "label": ""}}
            items.append({**info, "change": _CHANGE[bucket], "path": path})
    groups, total = changesets.round_rows(items)
    seq = external_id(record) or 0
    version_label = record.get("version_label")
    return {
        "card_id": record.get("card_id") or f"rc-{seq}",
        "card_type": "round_changes",
        "eyebrow": "这一轮改了什么",
        "title": f"改了 {total} 处",
        "subtitle": "",
        "status": {"key": "confirmed", "label": "已保存"},
        "plugin": None,
        "open": None,
        "changeset_id": record.get("changeset_id"),
        "change_list_id": record.get("change_list_id"),  # 契约 F3 补充：增量更新的清单
        "checks": {"errors": 0, "warnings": 0},
        "groups": [{"kind": kind, "label": changesets.GROUP_LABELS[kind], "items": groups[kind]}
                   for kind in changesets.GROUP_ORDER if kind in groups],
        "next_version_label": version_label,
        "state": "merged",
        "merged_version_label": version_label,
        "remaining_failures": 0,
        "from": record.get("from"),
        "to": record.get("to"),
    }


# ---------------------------------------------------------------------------
# 投影：宿主记录 → 对话流事件
# ---------------------------------------------------------------------------


def _usage(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"input_tokens": int(raw.get("input_tokens") or 0), "output_tokens": int(raw.get("output_tokens") or 0),
            "cache_read_tokens": int(raw.get("cache_read_input_tokens") or raw.get("cache_read_tokens") or 0)}


def context_usage(record: dict) -> dict:
    used = int(record.get("used") or 0)
    window = int(record.get("window") or 0)
    compact_at = int(record.get("compact_at") or 0)
    return {
        "used": used, "window": window, "pct": round(used * 100 / window, 1) if window else 0,
        "compact_at_pct": round(compact_at * 100 / window) if window and compact_at else 0,
        "breakdown": None, "last_call": None,
    }


def _error_id(record: dict) -> str:
    if record.get("error_id"):
        return record["error_id"]
    raw = f"{record.get('seq')}:{record.get('ts')}:{record.get('error')}"
    return "err-" + hashlib.sha256(raw.encode()).hexdigest()[:6]


def payload(record: dict) -> tuple[str | None, dict | None]:
    """记录 → (事件类型, 载荷)；只给内部用的记录返回 (None, None)。"""
    kind = str(record.get("type") or "")
    if kind in PASSTHROUGH:
        return PASSTHROUGH[kind], record.get("payload") or {}
    if kind == "progress":
        return ("progress" if int(record.get("revision") or 1) <= 1 else "progress.updated"), record.get("group") or {}
    if kind == "user_message":
        if record.get("hidden"):
            return None, None  # 自主构建运行器发的步骤（进展在分段进展块里）
        return "message.user", {
            "message_id": message_id(record), "text": record.get("text") or "",
            "attachments": record.get("attachments") or [], "references": record.get("references") or [],
            "origin": record.get("origin"), "delivery": record.get("delivery") or "immediate",
            "command": record.get("command") or _leading_command(record.get("prompt") or record.get("text")),
        }
    if kind == "stream_delta":
        return "message.delta", {"message_id": record.get("message_id") or "", "kind": record.get("kind") or "text",
                                 "text": record.get("text") or ""}
    if kind == "assistant_message":
        # narration：旧版把工具调用之间的短话收进了工作过程，那一步已经在组里
        if record.get("subagent") or record.get("parent_tool_use_id") or record.get("narration"):
            return None, None
        text = "\n\n".join(str(block.get("text") or "") for block in record.get("blocks") or []
                           if block.get("kind") == "text" and str(block.get("text") or "").strip())
        if not text:
            return None, None
        text, anchors = split_citations(text)
        return "message.assistant", {
            "message_id": record.get("message_id") or f"a-{external_id(record) or 0}", "text_md": text,
            "anchors": anchors, "entry_links": [], "continued": bool(record.get("continued")),
        }
    if kind == "tool_activity":
        return "tool.activity", {"tool_use_id": record.get("tool_use_id"), "visibility": "internal",
                                 "tool": record.get("tool") or "", "summary": record.get("summary") or "",
                                 "state": record.get("state") or "started"}
    if kind in ("subagent_started", "subagent_activity", "subagent_finished"):
        status = {"subagent_started": "started", "subagent_activity": "running"}.get(kind) or _SUBAGENT_STATUS.get(
            str(record.get("status") or ""), "failed")
        thinking = kind == "subagent_activity" and record.get("kind") == "thinking"
        step = _sub_step(record) if kind == "subagent_activity" and not thinking else None
        progress_text = (step["text"] or "").strip().split("\n", 1)[0] if step else ""
        return "subagent.updated", {
            "task_id": record.get("task_id") or "", "group_id": record.get("group_id"),
            "title": str(record.get("title") or subagent_title(record.get("description"), record.get("brief"),
                                                               record.get("agent_type"))),
            "status": status, "progress_text": progress_text[:120], "summary": record.get("summary") or None,
            "step": step, "step_count": record.get("step_count"),
            # 开始那条带派给它的任务说明（≤2000 字），其余为 null
            "brief": (record.get("brief") or None) if kind == "subagent_started" else None,
            # 子代理在想（瞬时事件，不回放）：从什么时候开始想的
            "thinking": {"since": iso(record.get("since")), "elapsed_ms": int(record.get("elapsed_ms") or 0)}
            if thinking else None,
        }
    if kind == "result":
        cost = record.get("total_cost_usd")
        return "turn.completed", {
            "turn_id": record.get("turn_id") or f"t-{external_id(record) or 0}",
            "duration_ms": int(record.get("duration_ms") or 0), "is_error": bool(record.get("is_error")),
            "usage": _usage(record.get("usage")),
            "cost": {"amount": float(cost) if isinstance(cost, (int, float)) else None, "currency": "USD"},
        }
    if kind == "context_usage":
        return "context.usage", context_usage(record)
    if kind == "context_compacted":
        pre, post = record.get("pre_tokens"), record.get("post_tokens")
        freed = pre - post if isinstance(pre, int) and isinstance(post, int) else None
        return "context.compacted", {
            "trigger": record.get("trigger") or "auto", "pre_tokens": pre, "post_tokens": post, "freed_tokens": freed,
            "kept": {"decisions": 0, "artifacts": True, "todos": False}, "at": iso(record.get("ts")),
        }
    if kind == "session_state":
        state = STATE.get(str(record.get("state") or "idle"), str(record.get("state") or "idle"))
        return "session.state", {"state": state, "since": iso(record.get("ts")),
                                 "turn_started_at": iso(record.get("turn_started_at")), "reason": record.get("reason")}
    if kind == "session_started":
        from ...api.views import engine_label

        resumed = bool(record.get("resumed"))
        return "session.started", {"engine_label": engine_label(record.get("engine") or "claude"), "resumed": resumed,
                                   "context_kept": bool(record.get("context_kept", resumed))}
    if kind == "session_error":
        from ..engines.base import FRIENDLY_ERROR

        return "session.error", {
            "error_id": _error_id(record), "message": record.get("error") or FRIENDLY_ERROR,
            "retries": int(record.get("retries") or 0), "retryable": bool(record.get("retryable", True)),
            "actions": list(record.get("actions") or ["retry"]),
            # stall_timeout：长时间没有引擎事件、判定执行超时（ADR-0097）；其余出错为 null
            "reason": record.get("reason"),
        }
    if kind == "session_interrupted":
        reason = str(record.get("reason") or "user")
        return "session.interrupted", {
            "by": record.get("by") or _INTERRUPT_BY.get(reason, "engine_exit"), "at": iso(record.get("ts")),
            "exec_id": record.get("exec_id"),
            "position_label": record.get("position_label") or record.get("message") or "这一步被打断了",
            "saved": record.get("saved"),
        }
    if kind == "queue_position":
        position = int(record.get("position") or 0)
        return "session.queued", {"position": position, "ahead": max(0, position - 1),
                                  "pool": record.get("pool") or {"active": 0, "max": 0},
                                  "progress_pct": record.get("progress_pct") or 0}
    if kind == "permission_request":
        tool = str(record.get("tool_name") or "")
        data = record.get("tool_input")
        return "permission.requested", {
            "request_id": record.get("request_id"), "title": record.get("title") or "助手请求执行一个操作",
            "description": record.get("description") or permission_description(tool, data),
            "details_text": details_text(tool, data), "timeout_s": int(record.get("timeout_s") or 600),
            "expires_at": iso(record.get("expires_at")),
        }
    if kind == "permission_resolved":
        behavior = record.get("behavior")
        return "permission.resolved", {
            "request_id": record.get("request_id"), "behavior": "allow" if behavior == "allow" else "deny",
            "by": record.get("by") or ("timeout" if behavior == "expired" else "user"),
            "message": record.get("message") or ("会话已重启，确认已失效" if behavior == "expired" else ""),
        }
    if kind in ("session_config_pending", "session_reconnected"):
        data = {key: value for key, value in record.items() if key not in ("type", "seq", "ts")}
        return ("session.config_pending" if kind == "session_config_pending" else "session.reconnected"), data
    if kind == "workspace_changes":
        return "card", round_changes_card(record)
    return None, None


_MD_LINE = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.、)]\s*)")


def _plain(text: str) -> str:
    """工作过程里一行一句：去掉标题、引用、列表记号与粗体、代码记号，空行和分隔线不要。"""
    lines = (_MD_LINE.sub("", line).replace("**", "").replace("`", "").strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line and not re.fullmatch(r"[-*_=|: ]{3,}", line))


def _sub_step(record: dict) -> dict:
    """子代理的一步（契约 EC-22 `step`，F2 补充）：同一个 id 后到的只改状态；说法为 null 时沿用先到的。"""
    step_id = str(record.get("step_id") or "")
    if record.get("kind") == "text":
        return {"id": step_id, "kind": "text", "text": _plain(str(record.get("text") or ""))[:300], "state": "done"}
    text = record.get("step_text")
    if text is None and not record.get("status"):  # F2 之前的历史：开始的那条按参数说，收尾的不知道
        text = step_text(str(record.get("tool") or ""), record.get("input"))
    state = {"ok": "done", "error": "failed"}.get(str(record.get("status") or ""), "running")
    return {"id": step_id, "kind": "tool", "text": text or None, "state": state}


def _leading_command(text) -> str | None:
    """消息开头的斜杠命令名（契约 F3 补充：前端据此认出对话正在跑哪个节点的命令）。"""
    head = str(text or "").strip().split(maxsplit=1)
    return head[0] if head and head[0].startswith("/") and len(head[0]) > 1 else None


def project(record: dict, project_id: str, conversation_id: str) -> dict | None:
    """一条记录 → 对话流信封（契约 1.11）；只给内部用的记录返回 None。"""
    kind, data = payload(record)
    if kind is None:
        return None
    envelope = {
        "type": kind,
        "ts": iso(record.get("ts") or time.time()),
        "project_id": project_id,
        "conversation_id": conversation_id,
        "data": data,
    }
    seq = external_id(record)
    if seq is not None and kind not in TRANSIENT:
        envelope = {"id": str(seq), **envelope}
    return envelope


def collapse(envelopes: list[dict]) -> list[dict]:
    """S-06：卡片、工作过程组、子任务卡按最新内容放在第一次出现的位置；问题带上回答后的状态；
    插话的去向、代存的命令只留最后一条。"""
    latest_card: dict[str, dict] = {}
    latest_group: dict[str, dict] = {}
    latest_sub: dict[str, dict] = {}
    question_state: dict[str, dict] = {}
    last_delivery: dict[str, int] = {}
    last_next = None
    for index, envelope in enumerate(envelopes):
        if envelope["type"] == "message.delivery":
            last_delivery[(envelope.get("data") or {}).get("message_id")] = index
        elif envelope["type"] == "next.updated":
            last_next = index
    for envelope in envelopes:
        data = envelope.get("data") or {}
        kind = envelope["type"]
        if kind in ("card", "card.updated") and data.get("card_id"):
            latest_card[data["card_id"]] = data
        elif kind == "subagent.updated" and data.get("task_id"):
            # 子任务卡只要最新的状态；逐步的明细由子任务步骤接口分页取
            prev = latest_sub.get(data["task_id"]) or {}
            latest_sub[data["task_id"]] = {
                **prev, **data, "progress_text": data.get("progress_text") or prev.get("progress_text") or "",
                "brief": data.get("brief") or prev.get("brief"),
                "step_count": max(int(prev.get("step_count") or 0), int(data.get("step_count") or 0))}
        elif kind in ("progress", "progress.updated") and data.get("group_id"):
            latest_group[data["group_id"]] = data
        elif kind == "question.answered":
            question_state[data.get("question_id")] = {"state": "answered", "answer": data.get("answer")}
        elif kind == "question.withdrawn":
            question_state[data.get("question_id")] = {"state": "withdrawn"}
    result, seen_cards, seen_groups, seen_subs = [], set(), set(), set()
    for index, envelope in enumerate(envelopes):
        data = envelope.get("data") or {}
        kind = envelope["type"]
        if kind == "message.delivery" and last_delivery.get(data.get("message_id")) != index:
            continue
        if kind == "next.updated" and index != last_next:
            continue
        if kind == "subagent.updated" and data.get("task_id"):
            if data["task_id"] in seen_subs:
                continue
            seen_subs.add(data["task_id"])
            envelope = {**envelope, "data": latest_sub[data["task_id"]]}
        elif kind in ("card", "card.updated") and data.get("card_id"):
            if data["card_id"] in seen_cards:
                continue
            seen_cards.add(data["card_id"])
            envelope = {**envelope, "type": "card", "data": latest_card[data["card_id"]]}
        elif kind in ("progress", "progress.updated") and data.get("group_id"):
            if data["group_id"] in seen_groups:
                continue
            seen_groups.add(data["group_id"])
            envelope = {**envelope, "type": "progress", "data": latest_group[data["group_id"]]}
        elif kind == "question.asked" and data.get("question_id") in question_state:
            envelope = {**envelope, "data": {**data, **question_state[data["question_id"]]}}
        result.append(envelope)
    return result


# 正文里的出处角标 `[^n]` 与文末的定义行 `[^n]: <锚点>`（锚点写法同契约 1.10，如 `resource/采购制度.docx#p:3`）
_CITATION_DEF = re.compile(r"^\[\^(\d{1,3})\]:\s*(\S.*?)\s*$")


def split_citations(text: str) -> tuple[str, list[dict]]:
    """把文末的出处定义行收成 `anchors`（`[{n, anchor}]`）并从正文里去掉；正文里的 `[^n]` 留给前端画角标。
    只认锚点（带 scheme 或 `resource/` 开头）的定义，别的脚注原样留着。"""
    if "[^" not in text:
        return text, []
    anchors, kept = [], []
    for line in text.split("\n"):
        match = _CITATION_DEF.match(line.strip())
        target = match.group(2).strip("`<> ") if match else ""
        if match and (target.startswith("resource/") or re.match(r"^[a-z]+:[^\s]", target)):
            anchors.append({"n": int(match.group(1)), "anchor": target})
            continue
        kept.append(line)
    if not anchors:
        return text, []
    return "\n".join(kept).rstrip(), anchors

