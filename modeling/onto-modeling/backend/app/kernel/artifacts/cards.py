"""对话里的通用产物卡（契约 4.7 `card_type: artifact`；ADR-0055）。

插件产物在一轮里新增或修改时，对话里出现它的卡片。卡片保持声明式：形状按包的 `views.<类型>.card` 画，
平台不跑包的代码。`card.component` 选平台的专用卡片组件（如 `flowchart`），专用组件要的数据一并带上；
平台不认识的组件按通用卡画。双击按 `views.<类型>.detail` 打开（`detail.kind`：generic / component / editor）。
卡片会写进对话历史，所以不带「只读」这类会变的状态：前端按实时的写入范围锁（CS-06）判断。

一轮最多出 ROUND_CARDS_MAX 张卡；删除的产物不出卡（「这一轮改了什么」里有）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import index, parsers
from .registry import ArtifactType, types_for_project

ROUND_CARDS_MAX = 8
THUMB_NODES_MAX = 40
TEXT_MAX = 120
_CHANGE_LABEL = {"add": "本轮新增", "mod": "本轮修改"}


def _text(value) -> str:
    """字段值 → 卡片上的一行字。"""
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("key") or ""
    elif isinstance(value, list):
        value = "、".join(_text(item) for item in value if item not in (None, ""))
    text = " ".join(str(value).split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def _label(atype: ArtifactType, name: str) -> str:
    spec = atype.field(name)
    return spec["label"] if spec else {"title": "标题", "key": "键", "updated_at": "更新时间"}.get(name, name)


def _chip(atype: ArtifactType, name: str, value) -> dict | None:
    if value in (None, "", []):
        return None
    spec = atype.field(name) or {}
    raw = str(value) if not isinstance(value, (dict, list)) else _text(value)
    option = next((item for item in spec.get("enum") or [] if item["value"] == raw), None)
    if option is None and atype.status and atype.status.get("field") == name:
        text = atype.status["map"].get(raw, raw)
        tone = (atype.status.get("tones") or {}).get(raw) or "gray"
    else:
        text = option["label"] if option else _text(value)
        tone = (option or {}).get("tone") or "gray"
    return {"field": name, "label": _label(atype, name), "value": raw, "text": text, "tone": tone}


def _detail(atype: ArtifactType) -> dict:
    detail = atype.view.get("detail") or {}
    if detail.get("editor"):
        return {"kind": "editor", "editor": dict(detail["editor"])}
    if detail.get("component"):
        return {"kind": "component", "component": detail["component"]}
    return {"kind": "generic", "layout": detail.get("layout") or "sections"}


def _flowchart_extras(atype: ArtifactType, root: Path, row: dict) -> dict:
    """平台流程图卡组件要的数据（契约 4.7 flowchart 卡的特有字段）。"""
    model = None
    try:
        items = parsers.parse_file(atype, root, row["rel"])
        model = next((item.content for item in items if item.key == row["key"]), None)
    except (OSError, parsers.ParseError):
        model = None
    nodes = [node for node in (model or {}).get("nodes") or [] if node.get("type") != "data"][:THUMB_NODES_MAX]
    kept = {node["id"] for node in nodes}
    edges = [edge for edge in (model or {}).get("edges") or [] if edge.get("from") in kept and edge.get("to") in kept]
    fields = row.get("fields") or {}
    return {
        "state": "draft" if fields.get("state") == "draft" else "confirmed",
        "summary": _text(fields.get("summary")),
        "thumbnail": {
            "nodes": [{"id": node["id"], "type": node.get("type") or "step", "label": _text(node.get("label")),
                       "pending": bool(node.get("pending"))} for node in nodes],
            "edges": [{"from": edge["from"], "to": edge["to"], "label": _text(edge.get("label"))} for edge in edges],
        },
    }


def card_for(atype: ArtifactType, root: Path, row: dict, *, change: str, pack_label: str = "",
             seed: str = "") -> dict:
    """一项产物 → 对话卡片（契约 4.7 artifact）。row 是产物索引的一行（key、title、fields、rel）。"""
    spec = atype.view.get("card") or {}
    fields = row.get("fields") or {}
    ref = {"type": atype.type, "key": row["key"], "title": row.get("title") or row["key"]}
    title_field = spec.get("title") or "title"
    title = _text(fields.get(title_field)) if title_field not in ("title", "key") else ""
    title = title or ref["title"]
    chips = [chip for chip in (_chip(atype, name, fields.get(name)) for name in spec.get("chips") or []) if chip]
    shown = [{"field": name, "label": _label(atype, name), "text": _text(fields.get(name))}
             for name in spec.get("fields") or [] if _text(fields.get(name))]
    digest = hashlib.sha256(f"{seed}\0{atype.type}\0{row['key']}".encode("utf-8")).hexdigest()[:12]
    card = {
        "card_id": f"ac-{digest}",
        "card_type": "artifact",
        "eyebrow": f"{atype.title} · {_CHANGE_LABEL.get(change, _CHANGE_LABEL['mod'])}",
        "title": title,
        "subtitle": _text(fields.get(spec["subtitle"])) if spec.get("subtitle") else "",
        "status": None,
        "plugin": {"name": atype.pack, "label": pack_label or atype.pack},
        "open": {"kind": "dialog", "target": "artifact", "ref": ref},
        "artifact": ref,
        "change": change if change in _CHANGE_LABEL else "mod",
        "component": spec.get("component"),
        "chips": chips,
        "fields": shown,
        "detail": _detail(atype),
        # 这一轮所属的变更集：会话宿主出卡时填（这一轮还没合入时双击按它读副本，AR-03 ?changeset=）
        "changeset_id": None,
    }
    if spec.get("component") == "flowchart" and atype.format == "drawio":
        card.update(_flowchart_extras(atype, root, row))
    return card


def _previous_items(atype: ArtifactType, root: Path, base: str, rel: str) -> dict[str, str] | None:
    """一个文件多项的格式（md-sections）：改之前那份里每项的修订号；取不到返回 None（整份算修改）。"""
    import json

    from .service import _cat_files

    blob = _cat_files(root, base, [rel]).get(rel)
    if blob is None:
        return {}
    try:
        items = parsers.parse(atype, rel, blob, file_key=atype.key_from_path(rel) or Path(rel).stem)
    except parsers.ParseError:
        return None
    return {item.key: index.sha(json.dumps([item.heading, item.fields, item.sections], ensure_ascii=False,
                                           sort_keys=True).encode()) for item in items}


def round_cards(manager, project_id: str, changes: dict, *, base: str = "", seed: str = "",
                root: Path | None = None) -> list[dict]:
    """一轮结束时改到的插件产物 → 卡片（新增在前）。changes 是 {added, modified, deleted} 路径列表；
    base 是这一轮开始时的提交或树：一个文件多项的格式按项比较，只出真的变了的那几项。
    root 是读文件的地方：缺省是项目工作区；助手在变更集副本里改时传副本（改动还没进主线）。
    产物类型与包名按项目工作区的锁文件算（卡片打开时看到的是同一套）。"""
    from ...services.packs import registry

    project_root = manager.project_root(project_id)
    source = Path(root) if root else project_root
    added = {str(path).lstrip("./") for path in changes.get("added") or []}
    modified = {str(path).lstrip("./") for path in changes.get("modified") or []}
    if not added and not modified:
        return []
    labels = {view.name: view.manifest.label for view in registry.desired_packs(manager, project_root)}
    cards: list[dict] = []
    for atype in types_for_project(manager, project_root):
        if (atype.view.get("card") or {}).get("hidden"):
            continue
        try:
            rows = index.rows_of(atype, source, added | modified)
        except Exception:  # noqa: BLE001 - 单个类型解析失败不影响别的卡
            continue
        previous: dict[str, dict | None] = {}
        for row in rows:
            change = "add" if row["rel"] in added else "mod" if row["rel"] in modified else None
            if change is None:
                continue
            if change == "mod" and base and atype.format == "md-sections":
                if row["rel"] not in previous:
                    previous[row["rel"]] = _previous_items(atype, source, base, row["rel"])
                before = previous[row["rel"]]
                if before is not None:
                    if row["key"] not in before:
                        change = "add"
                    elif before[row["key"]] == row.get("revision"):
                        continue
            cards.append(card_for(atype, source, row, change=change, pack_label=labels.get(atype.pack, ""), seed=seed))
    cards.sort(key=lambda card: card["change"] != "add")
    return cards[:ROUND_CARDS_MAX]
