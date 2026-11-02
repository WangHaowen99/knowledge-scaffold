"""自主构建给模型的注入（方案 B12「注入片说明和台账摘要」）：运行器每发一步之前写一份，宿主注入时读。

放在数据目录（`autobuild/<项目>/current.json`）而不是内存里：Jiuwen 在独立 worker 进程里算注入（ADR-0038），
两个进程读同一份文件。只给这次运行的对话注入，运行停下就删掉。
"""

from __future__ import annotations

from pathlib import Path

from . import store

INSTRUCTIONS = (
    "自主构建：不要停下来等人确认。拿不准时照常用 AskUserQuestion 提问，平台按策略立即代答——推荐项放第一个、"
    "label 末尾写「（推荐）」，保守的选项写「（保守）」；header 里写「置信度 0.xx」和依据的出处锚点。"
    "回答以「挂起」开头时，这一项先跳过，继续做能做的部分。只改这一片的内容；不发布、不删已确认的内容、不改资料区。"
)


def _path(project_id: str) -> Path:
    return store.project_dir(project_id) / "current.json"


def write(project_id: str, conversation_id: str, text: str) -> None:
    store.write_json(_path(project_id), {"conversation_id": conversation_id, "text": text})


def clear(project_id: str) -> None:
    _path(project_id).unlink(missing_ok=True)


def for_engine(project_root: Path, conversation_id: str | None) -> str:
    """宿主注入的登记点（`injection.register_autobuild_hint_provider`）：这条对话正在自主构建时返回注入文本。"""
    from ...kernel.changesets import worktree

    try:
        project_id = worktree.project_id_of(Path(project_root))
    except Exception:  # noqa: BLE001 - 不是项目工作区
        return ""
    data = store.read_json(_path(project_id)) if project_id else None
    if not data or (conversation_id and data.get("conversation_id") != conversation_id):
        return ""
    return str(data.get("text") or "")


def compose(run: dict, unit: dict, *, brief: str, notes: list[str]) -> str:
    """一步的注入：说明、这一片（第几片）、片说明、最近的自动决定、用户补充。"""
    units = [item for item in run.get("units") or [] if item["kind"] == unit["kind"]]
    position = next((index + 1 for index, item in enumerate(units) if item["id"] == unit["id"]), 0)
    head = (f"自主构建 · 运行 #{run['run_no']} · 「{unit['name']}」（{position}/{len(units)}）"
            if unit["kind"] == "slice" else f"自主构建 · 运行 #{run['run_no']} · {unit['name']}")
    parts = [head, INSTRUCTIONS]
    if brief:
        parts.append(f"片说明：\n{brief.strip()}")
    recent = [entry for entry in run.get("ledger") or []][-20:]
    if recent:
        lines = [f"- {(entry.get('slice') or {}).get('name') or ''}：{entry.get('question')} → {entry.get('chosen')}"
                 + ("（假设）" if entry.get("assumption") else "") for entry in recent]
        parts.append("已做的自动决定：\n" + "\n".join(lines))
    if notes:
        parts.append("用户补充：\n" + "\n".join(f"- {text}" for text in notes))
    return "\n\n".join(parts)
