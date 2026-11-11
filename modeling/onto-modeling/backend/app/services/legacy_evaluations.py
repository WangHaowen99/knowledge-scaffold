"""旧版评测记录（ADR-0092）：评测移出 onto 之前留在工作区的报告，只读。

旧链路每轮写 ``reports/eval-<日期>-<时间>.md`` 和同名的 ``.answers.json``（完整回答附件），随工作区 git 保存；
评测状态（.ontology-state/eval.json）已由启动迁移删掉（eval_retirement），这里只读报告文件本身，不读也不改状态。
独立评测导入的报告文件名多一段随机后缀（eval-<日期>-<时间>-<6 位>.md），不算旧版记录。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .workspace import WorkspaceError

REPORTS_DIR = "reports"
STEM = re.compile(r"^eval-(\d{8})-(\d{6})$")
TEXT_LIMIT = 2 * 1024 * 1024
ANSWERS_LIMIT = 16 * 1024 * 1024
_SCORE = re.compile(r"^- (?:综合得分|有效用例得分)：\*\*(\d{1,3})\*\*", re.M)


def _not_found() -> WorkspaceError:
    return WorkspaceError("旧版评测记录不存在", 404, "not_found")


def _file(root: Path, name: str) -> Path | None:
    """reports/ 下的普通文件；符号链接不读（助手能在工作区里建链接指到任意文件）。"""
    folder = Path(root) / REPORTS_DIR
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    return path


def _report(root: Path, stem: str) -> Path:
    path = _file(root, f"{stem}.md") if STEM.fullmatch(stem or "") else None
    if path is None:
        raise _not_found()
    return path


def _head(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.read(8 * 1024).decode("utf-8", errors="ignore")


def listing(root: Path) -> list[dict]:
    """新的在前：{id, at, title, score}；score 是报告总览里的得分，没形成有效评测时为 None。"""
    folder = Path(root) / REPORTS_DIR
    if not folder.is_dir() or folder.is_symlink():
        return []
    rows = []
    for path in folder.glob("eval-*.md"):
        match = STEM.fullmatch(path.stem)
        if not match or _file(root, path.name) is None:
            continue
        head = _head(path)
        title = re.search(r"^#\s+(.+)$", head, re.M)
        score = _SCORE.search(head)
        at = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").isoformat()
        rows.append({"id": path.stem, "at": at, "title": (title.group(1).strip() if title else path.stem)[:120],
                     "score": int(score.group(1)) if score else None})
    return sorted(rows, key=lambda row: row["id"], reverse=True)


def report(root: Path, stem: str) -> dict:
    """报告原文，最多 2 MiB，在行边界截断。"""
    data = _report(root, stem).read_bytes()
    truncated = len(data) > TEXT_LIMIT
    if truncated:
        cut = data.rfind(b"\n", 0, TEXT_LIMIT)
        data = data[:cut if cut > 0 else TEXT_LIMIT]
    return {"id": stem, "content": data.decode("utf-8", errors="replace"), "truncated": truncated}


def answer(root: Path, stem: str, result_id: str) -> dict:
    """报告附件里的一条完整回答。旧报告的「完整回答」链接按结果 ID 指到这里。"""
    _report(root, stem)
    path = _file(root, f"{stem}.answers.json")
    if path is None or path.stat().st_size > ANSWERS_LIMIT:
        raise WorkspaceError("这份报告没有完整回答附件", 404, "not_found")
    try:
        answers = json.loads(path.read_bytes()).get("answers")
    except (ValueError, AttributeError):
        answers = None
    record = next((item for item in answers or [] if isinstance(item, dict) and item.get("resultId") == result_id), None)
    if record is None:
        raise WorkspaceError("完整回答不存在", 404, "answer_not_found")
    full = str(record.get("finalAnswer") or "")
    length = record.get("originalLength")
    return {"resultId": result_id, "question": str(record.get("question") or ""), "fullAnswer": full,
            "originalLength": length if isinstance(length, int) else len(full), "truncated": record.get("truncated") is True}
