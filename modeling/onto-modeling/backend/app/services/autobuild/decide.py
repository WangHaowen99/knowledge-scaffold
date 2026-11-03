"""运行中助手的提问由策略立即作答（方案 B13、详设 8.5「决策台账」「挂起」）。

会话宿主在确认卡、决策卡要等人回答之前问这里（`agent_sessions.register_policy_responder`）：

- 决策卡（AskUserQuestion）：每个问题按置信度与阈值决定——达到阈值取推荐项；低于阈值按策略采用保守选项并记为
  假设，或挂起；`must_suspend_kinds` 里写到的类型一律挂起；没有选项可选的问题也挂起。自动决定记一条台账，挂起记一个
  挂起的问题（进待处理），答案交回给助手；
- 工具确认卡（权限模式不是全放行时才有）：放行。发布、删除已确认内容、改资料区由守卫与写入路径挡，不靠这里。

置信度、依据怎么来：自主构建注入里要求助手把置信度写在问题的 header 里（「置信度 0.85」），问题里写出处锚点；
推荐项、保守项在选项 label 里标「（推荐）」「（保守）」。没写置信度按 0.5 算（总是低于阈值）。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

from .. import material_text
from . import store

ASK_TOOL = "AskUserQuestion"
# 硬性不做（详设 1.6 #2）：删除文件、打或删发布标签。守卫与合入另有一层，这里是工具确认卡这一层
_FORBIDDEN_BASH = re.compile(
    r"(?:^|[\s;&|(`])(?:rm|rmdir|unlink|shred)(?:\s|$)|\bgit\s+(?:-\S+\s+)*rm\b|\s-delete\b"
    r"|\bgit\b[^\n;&|]*\b(?:tag|push|update-ref)\b[^\n;&|]*release/")
FORBIDDEN_MESSAGE = "自主构建不能删除内容，也不能打发布标签"
UNSTATED_CONFIDENCE = 0.5
SUSPEND_ANSWER = "挂起：这个问题先不定，等用户回答。跳过和它相关的部分，继续做别的。"
# 数字后面不能再接数字、小数点、百分号、斜杠（「12%」「15」「1/3」不能读成 1.0，审查 D）；百分数单独处理
_CONFIDENCE = re.compile(r"(?:置信度|confidence)\s*[:：=]?\s*(\d+(?:\.\d+)?|\.\d+)(?:\s*(%))?(?![\d./%])", re.IGNORECASE)
_TAG = re.compile(r"\s*[（(](推荐|保守)[)）]\s*$")
# 资料出处（带不带 `file:` 都认，真实模型按对话里的写法写 `resource/资料.docx#p:3`）与对话、Studio 用同一套抽取规则
# （material_text.find_anchors）；其余 scheme 的锚点在这里认
_OTHER_ANCHOR = re.compile(r"(?<![\w/])(?:ev|snap|db|url):[^\s，。；、,;)）\]】「」『』“”‘’？！：*`]+")


def forbidden(tool_name: str, tool_input) -> bool:
    """工具确认卡里的删除、发布标签：策略一律拒绝。"""
    if tool_name in ("Delete", "DeleteFile"):
        return True
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    return bool(_FORBIDDEN_BASH.search(str(tool_input.get("command") or "")))


def confidence(text: str) -> float | None:
    """「置信度 0.85」「置信度 85%」→ 0.85；超出 0～1（或 0%～100%）、写成分数的都不认。"""
    found = _CONFIDENCE.search(text or "")
    if not found:
        return None
    value = float(found.group(1))
    if found.group(2):
        value /= 100
    return value if 0 <= value <= 1 else None


def tag(option: dict) -> str | None:
    """选项 label 结尾的「（推荐）」「（保守）」；「不推荐」这类写在句子里的不算。"""
    found = _TAG.search(option.get("label") or "")
    return found.group(1) if found else None


def anchors(text: str) -> list[dict]:
    text = text or ""
    others = [(match.start(), match.end(), match.group(0)) for match in _OTHER_ANCHOR.finditer(text)]
    found = [(start, anchor) for start, _, anchor in others]
    for path, fragment, start in material_text.find_anchors(text):
        if not any(begin <= start < end for begin, end, _ in others):  # `snap:resource/…` 里的资料路径不另算
            found.append((start, material_text.file_anchor(path, fragment)))
    out = []
    for _, anchor in sorted(found):
        if any(item["anchor"] == anchor for item in out):
            continue
        path, _, fragment = anchor.partition("#")
        label = path.rsplit("/", 1)[-1].split(":", 1)[-1]
        if fragment:
            label += " §" + re.sub(r"(^|~)[a-z]+:", r"\1", fragment)  # 区间两端都去掉片段类型（p:10~p:11 → 10~11）
        out.append({"n": len(out) + 1, "anchor": anchor, "label": label})
        if len(out) >= 5:
            break
    return out


def _options(question: dict) -> list[dict]:
    return [option for option in question.get("options") or [] if not option.get("custom")]


def _label(option: dict) -> str:
    return _TAG.sub("", option.get("label") or "").strip()


def decide(question: dict, raw: dict, advanced: dict) -> dict:
    """一个问题怎么答：{kind: decision|suspension, option?, confidence, assumption, text}。"""
    header = str(raw.get("header") or "")
    descriptions = " ".join(str(option.get("description") or "") for option in raw.get("options") or []
                            if isinstance(option, dict))
    text = " ".join((question.get("title") or "", header, descriptions))
    value = confidence(header) or confidence(text)
    value = UNSTATED_CONFIDENCE if value is None else value
    threshold = float(advanced.get("confidence_threshold") or 0.8)
    options = _options(question)
    must = next((kind for kind in advanced.get("must_suspend_kinds") or [] if kind and kind in text), None)
    below = value < threshold
    if must or not options or (below and advanced.get("below_threshold") == "suspend"):
        # 挂起的原因给人看（待处理「原因」），不放问题的 header
        reason = (f"涉及「{must}」，按策略一律挂起" if must else "没有可选的选项" if not options
                  else f"置信度 {value:.2f}，低于阈值 {threshold:.2f}")
        return {"kind": "suspension", "confidence": value, "text": text, "reason": reason}
    recommended = next((option for option in options if tag(option) == "推荐"), None)
    conservative = next((option for option in options if tag(option) == "保守"), None)
    chosen = (conservative or recommended or options[0]) if below else (recommended or options[0])
    return {"kind": "decision", "option": chosen, "confidence": value, "assumption": below, "text": text}


def _slice_ref(run: dict) -> dict:
    cursor = run.get("cursor") or {}
    unit = next((item for item in run.get("units") or [] if item["id"] == cursor.get("unit")), None)
    return {"id": unit["id"], "name": unit["name"]} if unit else {"id": None, "name": ""}


def record(project_id: str, run_id: str, questions: list[dict], raws: list[dict], exec_id: str | None) -> tuple[dict, list, list]:
    """按策略答一组问题，台账与挂起记进运行记录。返回 (问题原文 → 答案, 台账条目, 挂起的问题)。"""
    answers: dict[str, str] = {}
    entries, suspensions = [], []

    def change(run: dict) -> None:
        advanced = (run.get("policy") or {}).get("advanced") or {}
        threshold = float(advanced.get("confidence_threshold") or 0.8)
        slice_ref = _slice_ref(run)
        step = (run.get("cursor") or {}).get("step")
        info = (run.setdefault("slices", {})).setdefault(slice_ref["id"] or "", {})
        now = time.time()
        for question, raw in zip(questions, raws):
            original = str(raw.get("question") or question.get("title") or "")
            outcome = decide(question, raw, advanced)
            options = [{**option, "recommended": False} for option in _options(question)]
            found = anchors(outcome["text"])
            if outcome["kind"] == "suspension":
                item = {"id": f"sp-{uuid.uuid4().hex[:8]}", "run_id": run["run_id"], "at": now, "slice": slice_ref,
                        "title": question.get("title") or "", "reason": outcome["reason"],
                        "handled_as": "跳过这一项，先做下一步", "kind": "question", "options": options,
                        "allow_custom": True, "actions": [], "state": "open", "step": step, "exec_id": exec_id,
                        "confidence": outcome["confidence"], "anchors": found}
                run.setdefault("suspensions", []).append(item)
                info.setdefault("suspensions", []).append(item["id"])
                info.setdefault("timeline", []).append({"type": "suspension", "id": item["id"]})
                suspensions.append(item)
                answers[original] = SUSPEND_ANSWER
                continue
            chosen = outcome["option"]
            options = [{**option, "recommended": option["id"] == chosen["id"]} for option in options]
            entry = {
                "id": f"led-{uuid.uuid4().hex[:8]}", "run_id": run["run_id"], "at": now, "slice": slice_ref,
                "question": question.get("title") or "", "chosen": _label(chosen), "options": options,
                "confidence": round(outcome["confidence"], 2), "threshold": threshold,
                "assumption": outcome["assumption"], "anchors": found, "affected": question.get("affected") or [],
                "review_state": "pending", "rejudged_to": None,
                "reason_text": "低于阈值，采用保守选项并记为假设" if outcome["assumption"] else "达到阈值，采用推荐选项",
                "summary": f"{slice_ref['name']}：{_label(chosen)}" if slice_ref["name"] else _label(chosen),
                "step": step, "exec_id": exec_id, "option_id": chosen["id"],
            }
            run.setdefault("ledger", []).append(entry)
            info.setdefault("decisions", []).append(entry["id"])
            info.setdefault("timeline", []).append({"type": "auto_decision", "id": entry["id"]})
            entries.append(entry)
            answers[original] = chosen.get("label") or ""

    store.update(project_id, run_id, change)
    return answers, entries, suspensions


async def respond(session, tool_name: str, tool_input: dict, title: str | None, description: str | None) -> dict | None:
    """会话宿主的登记点：返回给引擎的决定；不是运行中的会话返回 None（照常等人）。"""
    from ..session_host import timeline
    from .runner import runner

    run_id = getattr(session, "run_id", None)
    if not run_id:
        return None
    run = await asyncio.to_thread(store.get, session.project_id, run_id)
    if run is None or run.get("state") not in store.ACTIVE:
        return None
    request_id = uuid.uuid4().hex[:12]
    if tool_name != ASK_TOOL:
        refused = forbidden(tool_name, tool_input)
        session.broadcast({"type": "permission_request", "request_id": request_id, "tool_name": tool_name,
                           "tool_input": tool_input if isinstance(tool_input, dict) else {}, "title": title,
                           "description": description, "timeout_s": 0, "expires_at": time.time()})
        session.broadcast({"type": "permission_resolved", "request_id": request_id,
                           "behavior": "deny" if refused else "allow", "by": "autobuild_policy",
                           "message": FORBIDDEN_MESSAGE if refused else ""})
        if refused:
            return {"behavior": "deny", "message": FORBIDDEN_MESSAGE}
        return {"behavior": "allow", "updated_input": None}
    raws = [item if isinstance(item, dict) else {"question": str(item)} for item in (tool_input or {}).get("questions") or []]
    questions = timeline.build_questions(request_id, tool_input if isinstance(tool_input, dict) else {},
                                         conversation_id=session.conversation_id, exec_id=session.exec_id)
    if not raws:
        raws = [{"question": question["title"]} for question in questions]
    answers, entries, suspensions = await asyncio.to_thread(record, session.project_id, run_id, questions, raws,
                                                            session.exec_id)
    await runner.on_decisions(session.project_id, run_id, entries, suspensions)
    return {"behavior": "allow", "updated_input": {**(tool_input or {}), "answers": answers}}
