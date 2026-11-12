"""旧版评测记录（ADR-0092）：评测移出 onto 之前最近一轮评测的执行详情与报告，只读。

旧链路把最近一轮的状态记在 ``.ontology-state/eval.json``（阶段、逐用例进度、得分、报告路径），每轮报告写
``reports/eval-<日期>-<时间>.md`` 和同名 ``.answers.json``（完整回答附件），随工作区 git 保存。这里照 develop
的「评测执行详情」读出来：状态按 develop 的口径收敛（没有编排却仍是 running 的显示为中断），完整回答读报告附件。
旧运行器与 modeling 服务已按 ADR-0020 删除，不能停止、续接旧任务。

旧报告的「评测优化」与新评测同一做法：整理成一批外部反馈交给反馈演进（ADR-0092）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .workspace import WorkspaceError

STATE_REL = Path(".ontology-state") / "eval.json"
REPORTS_DIR = "reports"
STEM = re.compile(r"^eval-\d{8}-\d{6}$")
TEXT_LIMIT = 2 * 1024 * 1024
ANSWERS_LIMIT = 16 * 1024 * 1024
STATE_LIMIT = 4 * 1024 * 1024
# 与 develop 的评测优化提示同一阈值：得分低于它、或有异常用例时提示评测优化
EVOLUTION_SCORE_THRESHOLD = 90
INTERRUPTED_MESSAGE = "上一轮评测被服务重启中断"


def _not_found() -> WorkspaceError:
    return WorkspaceError("旧版评测记录不存在", 404, "not_found")


def _file(root: Path, rel: str) -> Path | None:
    """工作区里的普通文件；符号链接不读（助手能在工作区里建链接指到任意文件）。"""
    path = Path(root)
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            return None
    return path if path.is_file() else None


def read_state(root: Path) -> dict:
    path = _file(root, STATE_REL.as_posix())
    if path is None or path.stat().st_size > STATE_LIMIT:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def evolution_hint(state: dict) -> dict | None:
    """最近一轮评测是否该做评测优化（develop 的 evolution_hint）：得分低于阈值、或有异常用例。"""
    report = state.get("report")
    summary = state.get("summary") or {}
    if not (report and summary) or state.get("status") not in ("done", "failed"):
        return None
    # 报告须出自最近一轮；旧版状态没有 report_round 字段，done 即视为最近一轮
    fresh = state.get("report_round") if "report_round" in state else state.get("status") == "done"
    if not fresh:
        return None
    score = summary.get("score")
    errors = int(summary.get("error") or 0)
    if not (errors or (score is not None and score < EVOLUTION_SCORE_THRESHOLD)):
        return None
    return {"report": report, "score": score, "errors": errors, "fails": int(summary.get("fail") or 0),
            "round_failed": state.get("status") == "failed"}


def status(root: Path) -> dict:
    """{state, evolution}：state 是 develop「评测执行详情」读的那份状态，没有旧记录时为空。"""
    state = read_state(root)
    # 旧运行器已经没有了：仍标 running 的一律按中断显示（develop 重启后同样改判中断），不改盘上的记录
    if state.get("status") == "running":
        state = {**state, "status": "interrupted", "message": INTERRUPTED_MESSAGE}
    if state.get("status") in ("interrupted", "failed"):
        state = {**state, "resumable": False}
    state.pop("queue_position", None)
    return {"state": state, "evolution": evolution_hint(state)}


def _stem(stem: str) -> str:
    if not STEM.fullmatch(stem or ""):
        raise _not_found()
    return stem


def _file_rel(stem: str, suffix: str) -> str:
    return f"{REPORTS_DIR}/{stem}{suffix}"


def report(root: Path, stem: str) -> dict:
    """报告原文，最多 2 MiB，在行边界截断。"""
    path = _file(root, _file_rel(_stem(stem), ".md"))
    if path is None:
        raise _not_found()
    data = path.read_bytes()
    truncated = len(data) > TEXT_LIMIT
    if truncated:
        cut = data.rfind(b"\n", 0, TEXT_LIMIT)
        data = data[:cut if cut > 0 else TEXT_LIMIT]
    return {"id": stem, "path": _file_rel(stem, ".md"), "content": data.decode("utf-8", errors="replace"),
            "truncated": truncated}


def _answers(root: Path, stem: str) -> list:
    path = _file(root, _file_rel(_stem(stem), ".answers.json"))
    if path is None or path.stat().st_size > ANSWERS_LIMIT:
        raise WorkspaceError("完整回答证据不可用", 404, "answer_not_found")
    try:
        answers = json.loads(path.read_bytes()).get("answers")
    except (ValueError, AttributeError):
        answers = None
    if not isinstance(answers, list):
        raise WorkspaceError("完整回答证据不可用", 404, "answer_not_found")
    return answers


def answer(root: Path, stem: str, result_id: str) -> dict:
    """报告附件里的一条完整回答（develop 报告预览的首选来源：报告同目录的 answers.json）。"""
    record = next((item for item in _answers(root, stem)
                   if isinstance(item, dict) and str(item.get("resultId") or item.get("result_id") or "") == result_id), None)
    if record is None:
        raise WorkspaceError("完整回答证据不存在或不属于当前评测轮次", 404, "answer_not_found")
    full = str(record.get("finalAnswer") or "")
    length = record.get("originalLength")
    return {"resultId": result_id, "question": str(record.get("question") or ""), "fullAnswer": full,
            "originalLength": length if isinstance(length, int) else len(full), "truncated": record.get("truncated") is True}


def optimize(manager, project_id: str, stem: str, actor: str) -> dict:
    """旧报告的评测优化：整理成一批外部反馈（ADR-0092），交给 /ontology-evolution。同一份报告只成一批。"""
    from . import feedback_inbox
    from .package_evaluations import feedback_items
    from .workspace import _commit_paths

    root = manager.project_root(project_id)
    report_rel, answers_rel = _file_rel(_stem(stem), ".md"), _file_rel(stem, ".answers.json")
    report_path, answers_path = _file(root, report_rel), _file(root, answers_rel)
    if report_path is None or answers_path is None:
        raise WorkspaceError("完整回答证据不可用", 404, "answer_not_found")
    try:
        items = feedback_items(answers_path.read_bytes(), report_path.read_bytes())
    except (ValueError, AttributeError) as exc:
        raise WorkspaceError("旧版评测报告的回答附件格式不对", 422, "invalid_argument") from exc
    state = read_state(root)
    summary = state.get("summary") if state.get("report") == report_rel and isinstance(state.get("summary"), dict) else {}
    payload = {
        "feedback_id": f"legacy-{stem}",
        "source": {"service": "评测服务", "run": str(state.get("round_id") or stem) if summary else stem},
        "summary": {key: summary.get(key) for key in feedback_inbox.SUMMARY_FIELDS
                    if isinstance(summary.get(key), (int, float)) and not isinstance(summary.get(key), bool)},
        "items": items, "attachments": {"report": report_rel, "answers": answers_rel},
    }
    document = feedback_inbox.normalize(payload, allow_empty=True)
    with manager._project_lock(project_id):
        rel, document, created = feedback_inbox.place(manager, root, document, actor)
        if created:
            _commit_paths(root, [rel], f"feedback: 旧版评测报告评测优化 {stem}（{len(document['items'])} 条）by {actor}")
    return {"feedback_id": document["feedback_id"], "report": report_rel, "suggestions": None,
            "command": "/ontology-evolution", "args": {"feedback_id": document["feedback_id"]}}
