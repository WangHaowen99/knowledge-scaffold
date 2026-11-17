"""旧版评测记录（ADR-0092）：评测移出 onto 之前最近一轮评测的执行详情与报告，只读。

旧链路把最近一轮的状态记在 ``.ontology-state/eval.json``（阶段、逐用例进度、得分、报告路径），每轮报告写
``reports/eval-<日期>-<时间>.md`` 和同名 ``.answers.json``（完整回答附件），随工作区 git 保存。这里照 develop
的「评测执行详情」读出来：状态按 develop 的口径收敛（没有编排却仍是 running 的显示为中断），完整回答读报告附件。
旧运行器与 modeling 服务已按 ADR-0020 删除，不能停止、续接旧任务。
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


def status(root: Path) -> dict:
    """{state}：state 是 develop「评测执行详情」读的那份状态，没有旧记录时为空。"""
    state = read_state(root)
    # 旧运行器已经没有了：仍标 running 的一律按中断显示（develop 重启后同样改判中断），不改盘上的记录
    if state.get("status") == "running":
        state = {**state, "status": "interrupted", "message": INTERRUPTED_MESSAGE}
    if state.get("status") in ("interrupted", "failed"):
        state = {**state, "resumable": False}
    state.pop("queue_position", None)
    return {"state": state}


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
