"""自主构建给模型的注入（方案 B12「注入台账摘要」）：运行器每发一步之前写一份，宿主注入时读。

放在数据目录（`autobuild/<项目>/current.json`）而不是内存里：Jiuwen 在独立 worker 进程里算注入（ADR-0038），
两个进程读同一份文件。只给这次运行的对话注入，运行停下就删掉。
"""

from __future__ import annotations

from pathlib import Path

from . import store

INSTRUCTIONS = (
    "自主构建：不要停下来等人确认。拿不准时照常用 AskUserQuestion 提问，平台按策略立即代答——推荐项放第一个、"
    "label 末尾写「（推荐）」，保守的选项写「（保守）」；header 里写「置信度 0.xx」，问题里写依据的出处（如 resource/资料.docx#p:3）。"
    "命令里「确认无误请回复确认」这类产物确认不用问，按已确认往下做。"
    "回答以「挂起」开头时，这一项先跳过，继续做能做的部分。只做这一步；不发布、不删已确认的内容、不改资料区。"
)


ANSWER_LATER = "defer_to_next_run"


def _path(project_id: str) -> Path:
    return store.project_dir(project_id) / "current.json"


def write(project_id: str, conversation_id: str, text: str) -> None:
    store.write_json(_path(project_id), {"conversation_id": conversation_id, "text": text})


def clear(project_id: str) -> None:
    _path(project_id).unlink(missing_ok=True)


HANDOVER_NAMES = 8


def _run_end_commit(run: dict) -> str | None:
    """运行最后一次合入的提交（按单元顺序，撤回的不算）。"""
    commits = [info.get("commit") for info in (store.unit_info(run, unit["id"]) for unit in run.get("units") or [])
               if info.get("commit") and not info.get("reverted")]
    return commits[-1] if commits else None


def _changed_entries(root: Path, base: str, end: str) -> dict[str, list[str]]:
    """两个提交之间 ontology/ 下增删改的条目名（目录页不算）：{"A": [...], "M": [...], "D": [...]}。"""
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        return {}
    try:
        command = [git, "-C", str(root), "-c", "core.quotepath=false", "diff", "--name-status", "--no-renames",
                   base, end, "--", "ontology"]
        out = subprocess.run(  # NOSONAR(S4721) 固定参数列表，不经 shell
            command, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    changed: dict[str, list[str]] = {}
    for line in out.splitlines():
        status, _, rel = line.partition("\t")
        name = Path(rel).stem
        if not rel.endswith(".md") or name.lower() == "index":  # 只数条目，不数目录页
            continue
        if status[:1] in ("A", "M", "D") and name not in changed.setdefault(status[:1], []):
            changed[status[:1]].append(name)
    return changed


def handover(project_root: Path, conversation_id: str | None) -> str:
    """切回陪伴建模后的交接说明（宿主注入，和阶段提示一起按摘要去重：引擎实例第一条必带，内容不变不重复）。

    这条对话建在某次自主构建开始之前：它记得的条目、数量、状态可能已经被运行改过（实测：问「现在几个对象」答旧数，
    还说运行提交过的对象「还没验收」）。说清哪几次运行、从哪个版本开始、增删改了哪些条目，之前说过的以主线为准。
    运行中、运行自己的对话、运行开始之后才建的对话、运行没合入任何内容时返回空串。"""
    from ...kernel.changesets import worktree
    from .. import conversation_store

    root = worktree.main_root(Path(project_root)) or Path(project_root)
    project_id = worktree.project_id_of(root)
    if not project_id or not conversation_id or store.active(project_id) is not None:
        return ""
    entry = conversation_store.entry(project_id, conversation_id) or {}
    created = float(entry.get("created_at") or 0)
    if not created or entry.get("run_id"):
        return ""
    runs = sorted((run for run in store.runs(project_id)
                   if run.get("state") in store.ENDED and float(run.get("created_at") or 0) > created
                   and _run_end_commit(run) and run.get("base_commit")),
                  key=lambda run: float(run.get("created_at") or 0))
    if not runs:
        return ""
    first, last = runs[0], runs[-1]
    changed = _changed_entries(root, first["base_commit"], _run_end_commit(last))
    parts = []
    for status, verb in (("A", "新增"), ("M", "修改"), ("D", "删除")):
        names = changed.get(status) or []
        if names:
            more = f" 等 {len(names)} 个" if len(names) > HANDOVER_NAMES else ""
            parts.append(f"{verb} {'、'.join(names[:HANDOVER_NAMES])}{more}")
    numbers = "、".join(f"#{run['run_no']}" for run in runs)
    since = f"（从 {first['base_version_label']} 开始）" if first.get("base_version_label") else ""
    what = "：" + "；".join(parts) if parts else ""
    return (f"交接：这条对话之后，自主构建运行 {numbers}{since}改过主线{what}。这条对话里之前说到的条目、数量和状态"
            "都以现在的主线为准，回答前先读 ontology/ 下现在的文件；运行做完的步骤不要重做。")


def for_engine(project_root: Path, conversation_id: str | None) -> str:
    """宿主注入的登记点（`injection.register_autobuild_hint_provider`）：这条对话正在自主构建时返回注入文本；
    没有运行时，自主构建项目开始前的资料分析给 prerun 的说明。"""
    from ...kernel.changesets import worktree

    try:
        project_id = worktree.project_id_of(Path(project_root))
    except Exception:  # noqa: BLE001 - 不是项目工作区
        return ""
    data = store.read_json(_path(project_id)) if project_id else None
    if not data:
        from . import prerun

        return prerun.hint(project_root, conversation_id)
    if conversation_id and data.get("conversation_id") != conversation_id:
        return ""
    return str(data.get("text") or "")


def compose(run: dict, unit: dict, *, notes: list[str], carried: list[dict] = ()) -> str:
    """一步的注入：说明、这是第几步、上次留到这一次的问题与回答、最近的自动决定、用户补充。"""
    units = run.get("units") or []
    position = next((index + 1 for index, item in enumerate(units) if item["id"] == unit["id"]), 0)
    parts = [f"自主构建 · 运行 #{run['run_no']} · {unit['name']}（{position}/{len(units)}）", INSTRUCTIONS]
    for step in unit.get("steps") or []:
        if "resume_from" in step:
            where = f"（停在「{step['resume_from']}」）" if step["resume_from"] else ""
            parts.append(f"「{step['title']}」是从陪伴建模接过来的、没做完的一步{where}：已经保存的条目不要重做，"
                         "核对一遍再补齐没写完的。")
    if carried:
        lines = [f"- {item.get('title')}：" + (item["answer"] if item.get("answer") not in (None, "", ANSWER_LATER)
                                               else "还没回答，拿不准照常提问") for item in carried]
        parts.append("上次挂起、留到这一次的问题（按回答把跳过的部分补上）：\n" + "\n".join(lines))
    recent = [entry for entry in run.get("ledger") or []][-20:]
    if recent:
        lines = [f"- {(entry.get('slice') or {}).get('name') or ''}：{entry.get('question')} → {entry.get('chosen')}"
                 + ("（假设）" if entry.get("assumption") else "") for entry in recent]
        parts.append("已做的自动决定：\n" + "\n".join(lines))
    held = [item for item in run.get("suspensions") or [] if item.get("handed_over") and item.get("state") == "open"]
    if held:
        parts.append("开始前挂起、等用户回答的问题（和它相关的部分先跳过，不要再问）：\n"
                     + "\n".join(f"- {item.get('title')}" for item in held))
    if notes:
        parts.append("用户补充：\n" + "\n".join(f"- {text}" for text in notes))
    return "\n\n".join(parts)
