"""建模蓝图（契约 BP-01～BP-03、4.7 blueprint / research_summary 卡；详设第 9 章建议 2、6.8）。

蓝图是工作区里的 `research/blueprint.json`，格式、校验、调整都在 core 的 `blueprint.py`（契约库按项目锁定
版本加载，与助手在工作区里跑的是同一份）。这里只管它放在哪、怎么写进去：

- 资料分析那一轮助手在变更集副本里写出草案，确认生效之前蓝图只在副本里：这时读、调整都对副本
  （调整写进同一个变更集，助手在跑时 423）；确认蓝图（BP-03）先把这个变更集确认生效，再在主线上标已确认；
- 蓝图已经在主线上时，调整与确认经写入事务提交（范围锁照常检查）。

BP-03 选「按蓝图一路建完」（L4）时返回自主构建的开始前预览：由自主构建（B5b）经
`register_autobuild_preview` 登记，登记之前回 `409 autobuild_not_available`，蓝图不动。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from ..workspace.errors import WorkspaceError

_log = logging.getLogger(__name__)

BLUEPRINT_REL = "research/blueprint.json"
CARDS_MAX = 20
_lock = threading.Lock()

# 自主构建开始前预览（AutobuildPreview）的提供方：(manager, project_id, blueprint 视图, actor) → dict
_autobuild_preview: Callable[..., dict] | None = None


def register_autobuild_preview(provider: Callable[..., dict] | None) -> None:
    """B5b 登记：BP-03 next=L4 时算开始前预览（同 AB-03 的响应）。"""
    global _autobuild_preview
    _autobuild_preview = provider


def autobuild_available() -> bool:
    return _autobuild_preview is not None


def script(root: Path):
    """本项目锁定版本的蓝图脚本；旧 core 没有它时用镜像版本。"""
    from ..packs import contracts

    return contracts.module_for(Path(root), "blueprint")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _open_record(root: Path) -> dict | None:
    from ...kernel.changesets import store

    record = store.current(root)
    return record if record and record.get("state") in ("open", "pending_confirm", "conflict") else None


def locate(manager, project_id: str) -> tuple[Path, str, dict | None]:
    """蓝图在哪：没确认的变更集改过它时在副本里（"changeset"），否则在主线（"mainline"）。"""
    from ...kernel.changesets import service as changesets

    root = manager.project_root(project_id)
    record = _open_record(root)
    copy = changesets.existing_copy(root)
    if record and copy is not None and BLUEPRINT_REL in (record.get("paths") or []) \
            and (copy / BLUEPRINT_REL).is_file():
        return copy, "changeset", record
    return root, "mainline", record


def read(manager, project_id: str) -> dict | None:
    """{doc, revision, where, base, record, root}；没有蓝图时 None，文件坏了抛 422。"""
    base, where, record = locate(manager, project_id)
    module = script(manager.project_root(project_id))
    try:
        text = (base / BLUEPRINT_REL).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError("蓝图读不出来", 503, "service_unavailable", {"dependency": "blueprint"}) from exc
    try:
        doc = module.parse(text)
    except module.BlueprintError as exc:
        raise WorkspaceError(f"蓝图文件不合法：{exc}", 422, "check_rejected",
                             {"errors": [{"code": "blueprint_invalid", "level": "error", "message": str(exc),
                                          "path": BLUEPRINT_REL}], "warnings": []}) from exc
    return {"doc": doc, "revision": module.revision(text), "where": where, "base": base, "record": record,
            "root": manager.project_root(project_id)}


def load_doc(root: Path) -> dict | None:
    """主线上的蓝图（流程计算、写入范围用）；没有或读不出时 None。"""
    module = script(root)
    try:
        return module.load(root)
    except (OSError, module.BlueprintError):
        _log.warning("项目 %s 的蓝图读不出来，按全量模式处理", Path(root).name, exc_info=True)
        return None


def effective_doc(manager, project_id: str) -> dict | None:
    """界面上看到的那份（没确认的变更集改过就取副本里的）。"""
    try:
        loaded = read(manager, project_id)
    except WorkspaceError:
        return None
    return loaded["doc"] if loaded else None


def _research(root: Path) -> dict:
    from ..packs import contracts

    try:
        review = contracts.module_for(root, "research_review").review_status(root)
    except Exception:  # noqa: BLE001 - 验收状态是附加信息
        return {"verified": False, "report_id": None}
    # 资料分析报告在 research/ 下，报告接口（RP-02）只读 reports/：先不给编号（见 gaps B4d）
    return {"verified": review.get("status") == "passed", "report_id": None}


def view(manager, project_id: str, loaded: dict | None = None) -> dict:
    """BP-01：契约 7.8 Blueprint。"""
    loaded = loaded or read(manager, project_id)
    if loaded is None:
        raise WorkspaceError("还没有建模蓝图", 404, "blueprint_not_found")
    root = loaded["root"]
    data = script(root).view(loaded["doc"])
    data.update(revision=loaded["revision"], research=_research(loaded["base"]), evidence_hint=None)
    return data


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def _check_revision(loaded: dict, base_revision) -> None:
    if not isinstance(base_revision, str):
        raise WorkspaceError("要带上 base_revision", 400, "invalid_argument", {"fields": {"base_revision": "required"}})
    if base_revision and base_revision != loaded["revision"]:
        raise WorkspaceError("蓝图已经被改过，请刷新后再保存", 409, "revision_conflict",
                             {"target": BLUEPRINT_REL, "expected": base_revision, "actual": loaded["revision"],
                              "changed_by": "assistant" if loaded["where"] == "changeset" else "web",
                              "changed_at": None, "version_label": None})


def _invalid(exc) -> WorkspaceError:
    """脚本的校验错误 → 契约错误：切片编辑不合法是 slice_invalid（带 reason），其余是字段错误。"""
    fields = {"term_invalid": "term_choices", "answer_invalid": "answers"}
    if exc.reason in fields:
        return WorkspaceError(str(exc), 400, "invalid_argument", {"fields": {fields[exc.reason]: "invalid"}})
    if exc.reason == "format" and exc.details.get("field"):
        return WorkspaceError(str(exc), 400, "invalid_argument", {"fields": {exc.details["field"]: "invalid"}})
    return WorkspaceError(str(exc), 400, "slice_invalid", {"reason": exc.reason, **exc.details})


def _write(manager, project_id: str, loaded: dict, doc: dict, *, actor: str, summary: str) -> None:
    """写回原处：副本里直接写（这一轮的修改的一部分），主线经写入事务提交。"""
    module = script(loaded["root"])
    text = module.dumps(doc)
    if loaded["where"] == "changeset":
        from ...kernel.changesets import service as changesets

        lock = changesets.scope_lock(manager, project_id)
        if lock["state"] == "assistant_running":
            raise WorkspaceError("助手正在改这个项目，先打断或等它停下", 423, "write_scope_locked", lock)
        with manager._project_lock(project_id):
            module.save(loaded["base"], doc)
        changesets.observe(manager, project_id, running=False)
        return
    from ...kernel.writepath import ChangeTx, Origin

    with ChangeTx(manager, project_id, Origin("web", actor), summary=summary) as tx:
        tx.write(BLUEPRINT_REL, text)
        tx.commit(f"web: {summary}")


def _edit(body: dict) -> dict:
    edit = {"mode": body.get("mode")}
    for key in ("out_of_scope", "slices", "term_choices", "answers"):
        if body.get(key) is not None:
            edit[key] = body[key]
    for key in ("term_choices", "answers"):
        if key in edit and not isinstance(edit[key], list):
            raise WorkspaceError(f"{key} 要是列表", 400, "invalid_argument", {"fields": {key: "list"}})
    return edit


def save(manager, project_id: str, body: dict, *, actor: str) -> dict:
    """BP-02：草案（草案号不变）或调整（草案号加 1）。"""
    loaded = read(manager, project_id)
    if loaded is None:
        raise WorkspaceError("还没有建模蓝图", 404, "blueprint_not_found")
    _check_revision(loaded, body.get("base_revision"))
    module = script(loaded["root"])
    try:
        doc = module.apply_edit(loaded["doc"], _edit(body))
    except module.BlueprintError as exc:
        raise _invalid(exc) from exc
    _write(manager, project_id, loaded, doc, actor=actor, summary="调整建模蓝图")
    return view(manager, project_id)


def answer(manager, project_id: str, question_id: str, reply: dict, *, actor: str) -> dict:
    """待处理里回答蓝图的待答问题（IN-03 answer）：等同 BP-02 只带 answers。"""
    loaded = read(manager, project_id)
    if loaded is None:
        raise WorkspaceError("还没有建模蓝图", 404, "blueprint_not_found")
    module = script(loaded["root"])
    payload = {"question_id": question_id, **{key: reply.get(key) for key in ("option_id", "custom_text")
                                               if reply.get(key)}}
    try:
        doc = module.apply_edit(loaded["doc"], {"mode": "draft", "answers": [payload]})
    except module.BlueprintError as exc:
        raise _invalid(exc) from exc
    _write(manager, project_id, loaded, doc, actor=actor, summary="回答建模蓝图的问题")
    return view(manager, project_id)


def prepare_confirm(manager, project_id: str, body: dict) -> dict:
    """BP-03 第一步（不改任何东西）：核对版本号、构建方式；蓝图在没确认的变更集里时返回要先确认生效的变更集。"""
    next_mode = body.get("next") or (manager.get_project(project_id) or {}).get("build_mode") or "L2"
    if next_mode not in ("L2", "L4"):
        raise WorkspaceError("next 只能是 L2 或 L4", 400, "invalid_argument", {"fields": {"next": "enum"}})
    if next_mode == "L4" and not autobuild_available():
        raise WorkspaceError("自主构建还不能用", 409, "autobuild_not_available")
    loaded = read(manager, project_id)
    if loaded is None:
        raise WorkspaceError("还没有建模蓝图", 404, "blueprint_not_found")
    _check_revision(loaded, body.get("base_revision", ""))
    return {"next": next_mode, "changeset_id": loaded["record"]["id"] if loaded["where"] == "changeset" else None}


def mark_confirmed(manager, project_id: str, *, actor: str) -> dict:
    """BP-03 第二步：蓝图已在主线上（变更集已确认生效），标已确认；已确认的不再写。返回蓝图视图。"""
    loaded = read(manager, project_id)
    if loaded is None:
        raise WorkspaceError("还没有建模蓝图", 404, "blueprint_not_found")
    if loaded["where"] == "changeset":
        raise WorkspaceError("这一轮的修改还没确认生效", 409, "changeset_in_progress",
                             {"conversation_id": (loaded["record"] or {}).get("conversation_id")})
    module = script(loaded["root"])
    if loaded["doc"].get("state") != "confirmed":
        _write(manager, project_id, loaded, module.confirm(loaded["doc"]), actor=actor, summary="确认建模蓝图")
    return view(manager, project_id)


def first_command(manager, project_id: str) -> tuple[str, dict]:
    """「开始第一个切片」：第一片（全量模式不带切片）的对象提取。"""
    doc = load_doc(manager.project_root(project_id))
    module = script(manager.project_root(project_id))
    live = module.live_slices(doc)
    if module.mode(doc) == "sliced" and live:
        return "/ontology-extract-object", {"slice": live[0]["id"]}
    return "/ontology-extract-object", {}


def run_slices(manager, project_id: str) -> list[dict]:
    """按片执行用的切片列表（自主构建 B5b）：按建议顺序、没排除的片，带依赖、这一片的说明与现在的进度。

    [{id, name, core_flow, depends_on, brief, group, stages}]；没有蓝图或全量模式时是一片「全部」。
    说明（brief）与建模命令在工作区里 `blueprint.py brief <片>` 输出的是同一份。"""
    from . import model, slices

    root = manager.project_root(project_id)
    doc = effective_doc(manager, project_id)
    module = script(root)
    states = {item["id"]: item for item in (slices.compute(root, model.load_for(root), doc=doc, persist=False)
                                            or {}).get("slices") or []}
    items = module.live_slices(doc) if module.mode(doc) == "sliced" else [dict(slices.FULL_SLICE)]
    out = []
    for item in items:
        state = states.get(item["id"]) or {}
        out.append({"id": item["id"], "name": item["name"], "core_flow": item.get("core_flow") or "",
                    "depends_on": list(item.get("depends_on") or []), "brief": module.brief(doc, item["id"]),
                    "group": state.get("group") or "todo", "stages": state.get("stages") or {}})
    return out


def autobuild_preview(manager, project_id: str, blueprint_view: dict, *, actor: str) -> dict:
    if _autobuild_preview is None:
        raise WorkspaceError("自主构建还不能用", 409, "autobuild_not_available")
    return _autobuild_preview(manager, project_id, blueprint_view, actor=actor)


# ---------------------------------------------------------------------------
# 对话里的卡片（契约 4.7）
# ---------------------------------------------------------------------------


def _cards_path(root: Path) -> Path:
    from ...kernel.changesets.worktree import data_dir_of

    return data_dir_of(root) / "flow-state" / f"{Path(root).name}.blueprint-cards.json"


def _registered(root: Path) -> list[dict]:
    import json

    try:
        data = json.loads(_cards_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _register(root: Path, conversation_id: str, card_id: str) -> None:
    import json
    import os
    import uuid

    with _lock:
        items = [item for item in _registered(root) if item.get("card_id") != card_id]
        items.append({"conversation_id": conversation_id, "card_id": card_id})
        path = _cards_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(items[-CARDS_MAX:], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def purge(root: Path) -> None:
    try:
        _cards_path(root).unlink()
    except OSError:
        pass


def blueprint_card(module, doc: dict, card_id: str, *, research_verified: bool) -> dict:
    data = module.view(doc)
    return {
        "card_id": card_id, "card_type": "blueprint", "eyebrow": "", "title": f"建模蓝图 · {data['version_label']}",
        "subtitle": "", "status": {"key": "confirmed", "label": "已确认"} if data["state"] == "confirmed" else None,
        "plugin": None, "open": {"kind": "dialog", "target": "blueprint"},
        "version_label": data["version_label"], "state": data["state"], "goal_md": data["goal_md"],
        "goal_anchor": data["goal_anchor"], "goal_anchor_label": data["goal_anchor_label"],
        "out_of_scope": data["out_of_scope"], "slices": data["slices"],
        "terms": {"aligned": len(data["terms"]["aligned"]), "ambiguous": len(data["terms"]["ambiguous"])},
        "questions_pending": sum(1 for item in data["questions"] if not item.get("answer")),
        "research_verified": research_verified,
        # 卡片底部的两个按钮（设计调整 R3，取代「确认后怎么建」卡）：草案时给，一路建完要等自主构建可用
        "confirm_options": confirm_options(data["state"]),
    }


def confirm_options(state: str) -> list[dict]:
    """蓝图卡底部的确认按钮（BP-03 的 next）：草案时「确认，一片一片来」，自主构建可用时再加「确认，一路建完…」。"""
    if state == "confirmed":
        return []
    options = [{"mode": "L2", "label": "确认，一片一片来"}]
    if autobuild_available():
        options.append({"mode": "L4", "label": "确认，一路建完…"})
    return options


def research_card(root: Path, card_id: str) -> dict | None:
    """资料分析小结卡：数字取资料分析验收回执（读过的资料、核对过的出处）。"""
    from ..packs import contracts

    try:
        review_module = contracts.module_for(root, "research_review")
        status = review_module.review_status(root)
        receipt = review_module.load_review(root) or {}
    except Exception:  # noqa: BLE001
        return None
    if status.get("status") == "none":
        return None
    evidence = receipt.get("source_evidence") or {}
    files = len(evidence) or len([rel for rel in receipt.get("inputs") or {} if rel.startswith("resource/")])
    readable = sum(1 for item in evidence.values() if isinstance(item, dict) and item.get("status") == "ok")
    verified = status.get("status") == "passed"
    return {
        "card_id": card_id, "card_type": "research_summary", "eyebrow": "", "title": "资料分析完成", "subtitle": "",
        "status": {"key": "confirmed", "label": "已核验"} if verified else {"key": "pending", "label": "待核验"},
        "plugin": None, "open": None,
        "files": files, "coverage_pct": round(100 * readable / len(evidence)) if evidence else 0,
        "verified_anchors": sum(len(item.get("anchors") or []) for item in evidence.values() if isinstance(item, dict)),
        "problems_carried": len(status.get("manual_pending") or []) + len(status.get("errors") or []),
        "verified": verified, "report_id": None,
    }


def round_cards(manager, project_id: str, changes: dict, seed: str, root: Path | None = None) -> list[dict]:
    """一轮结束：这一轮写出或改了蓝图 → 资料分析小结卡（报告也改了时）、蓝图卡（底部带确认按钮）。"""
    touched = [rel for bucket in ("added", "modified") for rel in changes.get(bucket) or []]
    if BLUEPRINT_REL not in touched:
        return []
    main = manager.project_root(project_id)
    base = Path(root) if root is not None else main
    module = script(main)
    try:
        doc = module.load(base)
    except (OSError, module.BlueprintError):
        return []
    if doc is None:
        return []
    record = _open_record(main) or {}
    conversation_id = record.get("conversation_id")
    seed = str(seed or "")[:24] or "bp"
    cards = []
    if any(rel.startswith("research/") and rel.endswith(".md") for rel in touched):
        summary = research_card(base, f"{seed}-research")
        if summary:
            cards.append(summary)
    research = _research(base)
    card = blueprint_card(module, doc, f"{seed}-blueprint", research_verified=research["verified"])
    cards.append(card)
    if conversation_id:
        _register(main, conversation_id, card["card_id"])
    return cards


def card_updates(manager, project_id: str) -> list[tuple[str, dict]]:
    """蓝图改了之后，对话里登记过的蓝图卡全部更新到最新（card.updated）。"""
    root = manager.project_root(project_id)
    doc = effective_doc(manager, project_id)
    if doc is None:
        return []
    loaded_base = locate(manager, project_id)[0]
    module = script(root)
    verified = _research(loaded_base)["verified"]
    return [(item["conversation_id"], {"type": "card_updated",
                                       "payload": blueprint_card(module, doc, item["card_id"], research_verified=verified)})
            for item in _registered(root) if item.get("conversation_id") and item.get("card_id")]
