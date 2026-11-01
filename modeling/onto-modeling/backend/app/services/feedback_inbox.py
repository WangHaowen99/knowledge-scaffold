"""反馈入口（三份契约之「反馈入」，API 契约 X-05，ADR-0020）：接收评测侧的失分用例与线上反馈，
落到项目工作区，供反馈演进读取。

onto 不主动调用评测服务；评测服务持服务令牌（feedback.write）把一批反馈推进来：

- 落在工作区 ``feedback/<接收时间>-<feedback_id>.json``，单独成一个提交（只提交这个文件）：
  反馈随项目克隆、导出和回退，演进报告可以引用它；不在发布读取的目录里，不进知识包；
- feedback_id 是幂等键：同一个 ID 只生效一次，重复提交返回第一次落盘的那一批；
- 只收结构化字段，字符串截断、条数有上限；不收附件、不收地址与凭据类字段。

落盘格式（format = onto-feedback/v1）写在 ADR-0020 与 README，/ontology-evolution 与
check_eval_generalization.py 按它读取最新一批。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "onto-feedback/v1"
FEEDBACK_DIR = "feedback"
VERDICTS = ("fail", "partial", "error")
MAX_ITEMS = 200
MAX_TEXT = 4000
MAX_SHORT_TEXT = 200
MAX_LIST_ITEMS = 20
MAX_BODY_BYTES = 2 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SUMMARY_FIELDS = ("score", "total", "pass", "partial", "fail", "error")

CODE_INVALID = "feedback_invalid"


class FeedbackRejected(Exception):
    def __init__(self, message: str, status: int = 422, code: str = CODE_INVALID, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


class _Collector:
    """逐字段校验，问题攒齐后一次报给调用方（details.errors）。"""

    def __init__(self):
        self.errors: list[str] = []

    def text(self, value, path: str, limit: int = MAX_TEXT, required: bool = False) -> str:
        if value is None or value == "":
            if required:
                self.errors.append(f"{path} 不能为空")
            return ""
        if not isinstance(value, str):
            self.errors.append(f"{path} 必须是字符串")
            return ""
        return value.strip()[:limit]

    def texts(self, value, path: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.errors.append(f"{path} 必须是字符串数组")
            return []
        return [item.strip()[:MAX_TEXT] for item in value[:MAX_LIST_ITEMS] if item.strip()]

    def number(self, value, path: str):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.errors.append(f"{path} 必须是数字")
            return None
        return value


def _item(check: _Collector, raw, index: int) -> dict:
    path = f"items[{index}]"
    if not isinstance(raw, dict):
        check.errors.append(f"{path} 必须是对象")
        return {}
    verdict = check.text(raw.get("verdict"), f"{path}.verdict", 20) or "fail"
    if verdict not in VERDICTS:
        check.errors.append(f"{path}.verdict 必须是 {'、'.join(VERDICTS)} 之一")
    return {
        "case_id": check.text(raw.get("case_id"), f"{path}.case_id", 80, required=True),
        "question": check.text(raw.get("question"), f"{path}.question", required=True),
        "expected": check.text(raw.get("expected"), f"{path}.expected"),
        "actual": check.text(raw.get("actual"), f"{path}.actual"),
        "loss_reason": check.text(raw.get("loss_reason"), f"{path}.loss_reason"),
        "suspected_artifacts": check.texts(raw.get("suspected_artifacts"), f"{path}.suspected_artifacts"),
        # 以下两项是契约的补充（_plan/contract-changes.md）：区分运行异常与答错，带上执行轨迹里的工具失败
        "verdict": verdict,
        "tool_failures": check.texts(raw.get("tool_failures"), f"{path}.tool_failures"),
    }


def normalize(payload) -> dict:
    """请求体 → 落盘内容（不含接收方）。不合法时抛 FeedbackRejected（422，details.errors）。"""
    if not isinstance(payload, dict):
        raise FeedbackRejected("请求体必须是 JSON 对象", details={"errors": ["请求体必须是 JSON 对象"]})
    check = _Collector()
    feedback_id = check.text(payload.get("feedback_id"), "feedback_id", 64, required=True)
    if feedback_id and not _ID_RE.match(feedback_id):
        check.errors.append("feedback_id 只能包含字母、数字、点、下划线与短横线，最长 64 个字符")
    source = payload.get("source")
    if not isinstance(source, dict):
        check.errors.append("source 必须是对象 {service, run}")
        source = {}
    version = check.text(payload.get("release_version"), "release_version", 32)
    if version and not _VERSION_RE.match(version):
        check.errors.append("release_version 必须是发布版本号（如 0.1.12）")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        check.errors.append("items 必须是非空数组")
        items = []
    elif len(items) > MAX_ITEMS:
        check.errors.append(f"一批最多 {MAX_ITEMS} 条反馈")
        items = []
    summary_raw = payload.get("summary") or {}
    if not isinstance(summary_raw, dict):
        check.errors.append("summary 必须是对象")
        summary_raw = {}
    received_at = check.text(payload.get("received_at"), "received_at", 40)
    if received_at:
        try:
            datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except ValueError:
            check.errors.append("received_at 必须是 RFC 3339 时间")
    document = {
        "format": FORMAT,
        "feedback_id": feedback_id,
        "source": {
            "service": check.text(source.get("service"), "source.service", MAX_SHORT_TEXT, required=True),
            "run": check.text(source.get("run"), "source.run", MAX_SHORT_TEXT),
        },
        "release_version": version,
        "summary": {
            key: check.number(summary_raw.get(key), f"summary.{key}")
            for key in _SUMMARY_FIELDS if summary_raw.get(key) is not None
        },
        "items": [_item(check, raw, index) for index, raw in enumerate(items)],
        "reported_at": received_at,
    }
    if check.errors:
        raise FeedbackRejected("反馈不符合反馈契约：" + "；".join(check.errors[:3]), details={"errors": check.errors[:50]})
    return document


def _existing(root: Path, feedback_id: str) -> tuple[str, dict] | None:
    folder = root / FEEDBACK_DIR
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob(f"*-{feedback_id}.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("feedback_id") == feedback_id:
            return f"{FEEDBACK_DIR}/{path.name}", document
    return None


def latest_batch(root: Path) -> str | None:
    """最新一批反馈的工作区相对路径（文件名以接收时间开头，按名排序即按时间）。"""
    folder = root / FEEDBACK_DIR
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return f"{FEEDBACK_DIR}/{files[-1].name}" if files else None


def receive(manager, project_id: str, payload, received_by: str) -> tuple[dict, bool]:
    """写入一批反馈并提交；返回 (Feedback 视图, 是否新建)。同一 feedback_id 只生效一次。"""
    from .workspace import _commit_paths

    document = normalize(payload)
    if document["release_version"] and not any(
        item["version"] == document["release_version"] for item in manager.releases(project_id)
    ):
        raise FeedbackRejected(
            f"发布版本 {document['release_version']} 不存在",
            details={"errors": ["release_version 不是本项目已有的发布版本"]},
        )
    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        existing = _existing(root, document["feedback_id"])
        if existing is not None:
            return view(*existing, ""), False
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone.utc)
        document["received_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
        document["received_by"] = received_by
        rel = f"{FEEDBACK_DIR}/{stamp:%Y%m%d-%H%M%S}-{document['feedback_id']}.json"
        manager._atomic_write_text(root / rel, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        commit = _commit_paths(
            root, [rel],
            f"feedback: 接收外部反馈 {document['feedback_id']}（{len(document['items'])} 条）by {received_by}",
        )
    return view(rel, document, commit), True


def view(rel: str, document: dict, commit: str) -> dict:
    """Feedback（X-05 响应）：批次摘要，不回显条目正文。"""
    counts = {verdict: 0 for verdict in VERDICTS}
    for item in document.get("items") or []:
        counts[item.get("verdict", "fail")] = counts.get(item.get("verdict", "fail"), 0) + 1
    return {
        "feedback_id": document.get("feedback_id"),
        "path": rel,
        "source": document.get("source"),
        "release_version": document.get("release_version") or None,
        "item_count": len(document.get("items") or []),
        "verdicts": counts,
        "received_at": document.get("received_at"),
        "received_by": document.get("received_by"),
        "commit": commit or None,
    }
