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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

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


def external_id(record: dict) -> int | None:
    seq = record.get("seq")
    return int(seq) + 1 if isinstance(seq, int) else None


def message_id(record: dict) -> str:
    """用户消息的 ID：宿主写入时就定下；旧记录按对外 id 补（与 S-07 返回的一致）。"""
    return record.get("message_id") or f"m-{external_id(record) or 0}"


# ---------------------------------------------------------------------------
# 工具调用 → 业务话
# ---------------------------------------------------------------------------


def _base(path) -> str:
    text = str(path or "").strip()
    return PurePosixPath(text.replace("\\", "/")).name or text


def step_text(tool: str, data) -> str:
    """一次工具调用在「工作过程」里的说法；只给文件名，不给命令原文。"""
    data = data if isinstance(data, dict) else {}
    target = _base(data.get("file_path") or data.get("path") or data.get("notebook_path") or "")
    if tool == "Read":
        return f"读取「{target}」" if target else "读取资料"
    if tool == "Write":
        return f"写入「{target}」" if target else "写入文件"
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"修改「{target}」" if target else "修改文件"
    if tool == "Bash":
        description = str(data.get("description") or "").strip()
        return description[:60] if description else "运行脚本"
    if tool in ("Glob", "Grep", "LS"):
        return "查找相关文件"
    if tool in ("Task", "Agent"):
        description = str(data.get("description") or "").strip()
        return f"派出子任务「{description[:40]}」" if description else "派出子任务"
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
    if ms is None:
        return "进行中"
    seconds = max(0, int(round(ms / 1000)))
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
    open_steps: dict = field(default_factory=dict)  # 工具调用 / 子代理 ID → (步骤 ID, 开始时间)
    tool_names: dict = field(default_factory=dict)
    stream_message_id: str | None = None
    had_text: bool = False
    state: str = "running"

    @classmethod
    def new(cls, title: str = "") -> "Turn":
        return cls(turn_id=f"t-{uuid.uuid4().hex[:10]}", title=title or "工作过程")

    @property
    def group_id(self) -> str:
        return f"g-{self.turn_id}"

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
            tool = turn.tool_names.get(tool_use_id, "")
            failed = bool(block.get("is_error"))
            records.append(({"type": "tool_activity", "tool_use_id": tool_use_id, "tool": tool,
                             "summary": step_text(tool, {}), "state": "failed" if failed else "done"}, False))
            changed = turn.close_step(f"tool:{tool_use_id}", failed) or changed
        if changed:
            records.append((turn.progress_record(), False))
        return records
    if kind == "subagent_started":
        title = str(event.get("description") or event.get("agent_type") or "子任务")
        # 子代理由一次 Task 调用派出：那一步已经在组里，不再另加一步
        key = f"tool:{event.get('tool_use_id')}" if event.get("tool_use_id") else ""
        records = [({**event, "group_id": turn.group_id}, False)]
        if not key or key not in turn.open_steps:
            turn.add_step(f"task:{event.get('task_id')}", f"子任务「{title[:40]}」")
            records.append((turn.progress_record(), False))
        return records
    if kind == "subagent_finished":
        records = [({**event, "group_id": turn.group_id}, False)]
        failed = _SUBAGENT_STATUS.get(str(event.get("status") or ""), "failed") == "failed"
        if turn.close_step(f"task:{event.get('task_id')}", failed):
            records.append((turn.progress_record(), False))
        return records
    if kind == "subagent_activity":
        return [({**event, "group_id": turn.group_id}, False)]
    return [(event, False)]


def _assistant(turn: Turn, event: dict) -> list[tuple[dict, bool]]:
    if event.get("subagent") or event.get("parent_tool_use_id"):
        return [(event, False)]  # 子代理的内部消息：只留档，不进用户可见的事件
    blocks = event.get("blocks") or []
    text_blocks = [block for block in blocks if block.get("kind") in ("text", "thinking")]
    tool_blocks = [block for block in blocks if block.get("kind") == "tool_use"]
    records: list[tuple[dict, bool]] = []
    has_text = any(block.get("kind") == "text" and str(block.get("text") or "").strip() for block in text_blocks)
    if text_blocks:
        record = {key: value for key, value in event.items() if key != "blocks"}
        record["blocks"] = text_blocks
        if has_text:
            record["message_id"] = turn.stream_message_id or f"a-{uuid.uuid4().hex[:10]}"
            record["continued"] = turn.had_text
            turn.stream_message_id = None
            turn.had_text = True
        records.append((record, False))
    for block in tool_blocks:
        tool = str(block.get("tool") or "")
        tool_use_id = block.get("tool_use_id")
        turn.tool_names[tool_use_id] = tool
        text = step_text(tool, block.get("input"))
        records.append(({"type": "tool_activity", "tool_use_id": tool_use_id, "tool": tool, "summary": text,
                         "state": "started"}, False))
        turn.add_step(f"tool:{tool_use_id}", text)
    if tool_blocks:
        records.append((turn.progress_record(), False))
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

_GROUP_LABELS = {"object": "对象", "operation": "操作", "rule": "规则", "metric": "指标", "script": "脚本",
                 "other": "其他文件"}
_CHANGE = {"added": "add", "modified": "mod", "deleted": "del"}
_VERB = {"add": "新增", "mod": "修改", "del": "删除"}


def round_changes_card(record: dict) -> dict:
    from ...kernel import metamodel

    groups: dict[str, list] = {}
    total = 0
    for bucket in ("added", "modified", "deleted"):
        for path in record.get(bucket) or []:
            change = _CHANGE[bucket]
            entry = metamodel.mm.entry_key(path)
            if entry:
                kind, _, key = entry.partition(":")
                target = {"kind": kind, "id": entry, "key": key, "name": key}
                name = key
            else:
                kind = "other"
                name = _base(path)
                target = {"type": "file", "key": path, "title": name}
            groups.setdefault(kind, []).append({
                "change": change, "target": target, "text": f"{_VERB[change]}「{name}」", "comparable": change == "mod",
            })
            total += 1
    order = list(_GROUP_LABELS)
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
        "groups": [{"kind": kind, "label": _GROUP_LABELS[kind], "items": groups[kind]}
                   for kind in sorted(groups, key=order.index)],
        "lock_note": "",
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
        if record.get("subagent") or record.get("parent_tool_use_id"):
            return None, None
        text = "\n\n".join(str(block.get("text") or "") for block in record.get("blocks") or []
                           if block.get("kind") == "text" and str(block.get("text") or "").strip())
        if not text:
            return None, None
        return "message.assistant", {
            "message_id": record.get("message_id") or f"a-{external_id(record) or 0}", "text_md": text,
            "anchors": [], "entry_links": [], "continued": bool(record.get("continued")),
        }
    if kind == "tool_activity":
        return "tool.activity", {"tool_use_id": record.get("tool_use_id"), "visibility": "internal",
                                 "tool": record.get("tool") or "", "summary": record.get("summary") or "",
                                 "state": record.get("state") or "started"}
    if kind in ("subagent_started", "subagent_activity", "subagent_finished"):
        status = {"subagent_started": "started", "subagent_activity": "running"}.get(kind) or _SUBAGENT_STATUS.get(
            str(record.get("status") or ""), "failed")
        progress_text = ""
        if kind == "subagent_activity":
            progress_text = str(record.get("text") or "") if record.get("kind") == "text" else step_text(
                str(record.get("tool") or ""), record.get("input"))
        return "subagent.updated", {
            "task_id": record.get("task_id") or "", "group_id": record.get("group_id"),
            "title": str(record.get("description") or record.get("agent_type") or "子任务"), "status": status,
            "progress_text": progress_text[:120], "summary": record.get("summary") or None,
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
    """S-06：卡片、工作过程组按最新内容放在第一次出现的位置；问题带上回答后的状态。"""
    latest_card: dict[str, dict] = {}
    latest_group: dict[str, dict] = {}
    question_state: dict[str, dict] = {}
    for envelope in envelopes:
        data = envelope.get("data") or {}
        kind = envelope["type"]
        if kind in ("card", "card.updated") and data.get("card_id"):
            latest_card[data["card_id"]] = data
        elif kind in ("progress", "progress.updated") and data.get("group_id"):
            latest_group[data["group_id"]] = data
        elif kind == "question.answered":
            question_state[data.get("question_id")] = {"state": "answered", "answer": data.get("answer")}
        elif kind == "question.withdrawn":
            question_state[data.get("question_id")] = {"state": "withdrawn"}
    result, seen_cards, seen_groups = [], set(), set()
    for envelope in envelopes:
        data = envelope.get("data") or {}
        kind = envelope["type"]
        if kind in ("card", "card.updated") and data.get("card_id"):
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
