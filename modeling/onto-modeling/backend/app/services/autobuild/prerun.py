"""自主构建开始前的资料分析也由策略代答（F2 第 7 条）。

构建方式选了自主构建的项目，运行从资料分析之后开始（详设 8.5），资料分析在它前面，原来照陪伴建模出决策卡等人选：
测试部署上用户在自主构建的项目里点「资料分析」，连着出了「还有没有别的资料」「编号沿不沿用」两张卡。用户选了自主构建
就是不逐个确认，资料分析也按策略答：

- 每个问题按决定策略选：达到阈值取推荐项，否则取保守项记为假设。这一步没有「下一步」可跳，不挂起（「一律挂起」的类型
  同样按保守项记为假设）；没有选项的让助手按资料判断。开始前的人工关是开始前预览；
- 对话里每个决定一行「已自动决定」（question.answered，by=autobuild_policy），不出卡片、不进待处理、不发通知；
  这时还没有运行，不进台账；
- 产物确认（decide.template_confirm）直接按确认，不出那一行；工具确认按运行中的规矩（decide.permit）。

只管资料分析：之后不在运行里的步骤是用户自己点的，照常等人。

只代答业务歧义（F3 修 F2 第 7 条引入的回归）：「资料分析和蓝图都完成了，接下来怎么建？」这类决定流程走向的问题不代答
——测试部署上它被代答成「提取对象」，助手在蓝图还是草案时就把第 1 段的对象建了出来。资料分析里（任何构建方式）这类问题从
提问里拿掉，回答一律是 STOP_ANSWER：停在蓝图确认，下一步由用户在界面上点（`flow_stops`，会话宿主在出卡、代答之前调）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from . import store

RESEARCH_NODE = "core.research"
INSTRUCTIONS = (
    "自主构建（开始前的资料分析）：不要停下来等人确认。拿不准时照常用 AskUserQuestion 提问，平台按策略立即代答——"
    "推荐项放第一个、label 末尾写「（推荐）」，保守的选项写「（保守）」；header 里写「置信度 0.xx」。"
    "命令里「确认无误请回复确认」这类产物确认不用问，按已确认往下做。"
    "只问业务上的歧义；接下来做什么不要问——蓝图写好就 release 停下，等用户开始下一步。"
    "资料和建模目标对不上（或目标太笼统）时不要用 AskUserQuestion 问，写进蓝图草稿的目标问题，由用户在蓝图上回答。"
)
OPEN_ANSWER = "自主构建：这个问题不等人回答。按资料能确定的往下做，拿不准的写成假设并在报告里注明。"
STOP_ANSWER = ("停在这里：资料分析到建模蓝图为止，下一步由用户在界面上确认建模蓝图后开始。不要开始建模，也不要再问接下来"
               "做什么；先 release research，再用一两句小结结束这一轮。")
_TITLE_MAX = 40


def _project(project_root: Path) -> tuple[str, dict]:
    """(项目 ID, 元数据)；副本路径按主线工作区读。Jiuwen worker 进程里也要用，不经工作区管理器。"""
    from ...kernel.changesets import worktree
    from ..workspace.layout import META_FILE

    main = worktree.main_root(Path(project_root))
    try:
        meta = json.loads((main / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return main.name, meta if isinstance(meta, dict) else {}


def applies(project_root: Path, record: dict | None) -> bool:
    """这次执行是自主构建项目开始前的资料分析：项目选了自主构建、没有运行、执行的是资料分析。"""
    if not record or (record.get("node") or {}).get("id") != RESEARCH_NODE:
        return False
    project_id, meta = _project(project_root)
    return meta.get("build_mode") == "L4" and store.active(project_id) is None


def flow_stops(record: dict | None, tool_input) -> tuple[dict, dict]:
    """资料分析里（任何构建方式、没有运行）决定流程走向的问题：从这次提问里拿掉，回答一律 STOP_ANSWER。

    返回 (剩下的 tool_input, {原问题: STOP_ANSWER})；不是资料分析、没有这类问题时原样返回和 {}。"""
    if not record or (record.get("node") or {}).get("id") != RESEARCH_NODE:
        return tool_input, {}
    if record.get("run_id") or not isinstance(tool_input, dict):
        return tool_input, {}
    from . import decide

    kept, stops = [], {}
    for item in tool_input.get("questions") or []:
        raw = item if isinstance(item, dict) else {"question": str(item)}
        if decide.flow_direction(raw):
            stops[str(raw.get("question") or "")] = STOP_ANSWER
        else:
            kept.append(item)
    if not stops:
        return tool_input, {}
    return {**tool_input, "questions": kept}, stops


def hint(project_root: Path, conversation_id: str | None) -> str:
    """注入（hint.for_engine 在没有运行时调）：这条对话在做开始前的资料分析时返回说明。"""
    from ..session_host import exec_records

    if not conversation_id:
        return ""
    project_id, _meta = _project(project_root)
    return INSTRUCTIONS if applies(project_root, exec_records.open_record(project_id, conversation_id)) else ""


async def respond(session, tool_name: str, tool_input, title: str | None, description: str | None) -> dict | None:
    """策略登记点在没有运行时转到这里；不是开始前的资料分析返回 None（照常等人）。"""
    from ..session_host import exec_records, timeline
    from . import decide, policy

    root, exec_id = getattr(session, "cwd", None), getattr(session, "exec_id", None)
    if root is None or not exec_id:
        return None
    record = await asyncio.to_thread(exec_records.get, session.project_id, exec_id)
    if not await asyncio.to_thread(applies, root, record):
        return None
    if tool_name != decide.ASK_TOOL:
        return decide.permit(session, tool_name, tool_input, title, description)
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    questions = timeline.build_questions(uuid.uuid4().hex[:12], tool_input, conversation_id=session.conversation_id,
                                         exec_id=exec_id, step_label=((record.get("phase") or {}).get("label") or ""))
    raws = [item if isinstance(item, dict) else {"question": str(item)} for item in tool_input.get("questions") or []]
    raws = raws or [{"question": question["title"]} for question in questions]
    advanced = {**await asyncio.to_thread(policy.advanced, session.project_id),
                "must_suspend_kinds": [], "below_threshold": "conservative_assumption"}
    answers: dict[str, str] = {}
    for question, raw in zip(questions, raws):
        original = str(raw.get("question") or question["title"])
        confirmed = decide.template_confirm(question)
        if confirmed is not None:
            answers[original] = confirmed.get("label") or ""
            continue
        outcome = decide.decide(question, raw, advanced)
        if outcome["kind"] == "decision":
            chosen = outcome["option"]
            answers[original] = chosen.get("label") or ""
            _post(session, question, {"option_id": chosen["id"]}, decide.plain_label(chosen), outcome["assumption"])
        else:  # 没有选项可选
            answers[original] = OPEN_ANSWER
            _post(session, question, {}, "按资料判断", True)
    return {"behavior": "allow", "updated_input": {**tool_input, "answers": answers}}


def _post(session, question: dict, answer: dict, label: str, assumption: bool) -> None:
    """对话里一行「已自动决定」：问题记成已回答（auto：不发「需要你回答」通知），随后是回答。"""
    from ..session_host.views import iso

    title = question.get("title") or ""
    short = title if len(title) <= _TITLE_MAX else title[:_TITLE_MAX] + "…"
    summary = f"已自动决定「{short}」：{label}" + ("（假设）" if assumption else "")
    session.broadcast({"type": "question_asked", "auto": True,
                       "payload": {**question, "state": "answered", "answer": answer}})
    session.broadcast({"type": "question_answered", "payload": {
        "question_id": question["question_id"], "answer": answer, "by": "autobuild_policy", "summary": summary,
        "answered_at": iso(time.time())}})
