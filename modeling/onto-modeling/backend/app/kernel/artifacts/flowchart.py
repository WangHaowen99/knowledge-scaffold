"""内核内置的 `diagram.flowchart` 解析与写出（详设 5.9.4、5.9.5；契约 7.11 FlowchartModel）。

包只登记产物类型，不在内核里跑代码：draw.io 文件由这里解析成 FlowchartModel，标准编辑器保存时再写回
draw.io。只用标准库；解析前拒绝 DOCTYPE、ENTITY 声明，输入限 5 MiB。

内核元数据写成 draw.io 的用户对象属性（draw.io「编辑数据」里可见），再解析时原样读回：
- 流程元数据与所描述的操作挂在根单元格 id="0" 上：`<object id="0" process_name process_description
  process_roles process_inputs process_exceptions process_handoffs operation><mxCell/></object>`，
  四个列表是 JSON 字符串数组，`operation` 是 EntryRef 的 JSON（没有时为空串）。
- 节点：`<object id label onto_type description refs pending pending_note><mxCell vertex="1" …/></object>`，
  `refs` 是 EntryRef 数组的 JSON，`pending` 为 "1"/"0"，`pending_note` 为空时不写。
- 连线：`<object id label onto_type condition><mxCell edge="1" source target …/></object>`。
- 坐标：模型里是节点中心，文件里是左上角，按宽高换算；容器里的节点按父级累加成绝对坐标。

没有 `onto_type` 的图按样式推断类型。带 `onto_type` 时以它为准，除非高级编辑器里把形状换成了另一类
特征形状（椭圆、菱形、数据形状；连线改成虚线），这时按新形状重新推断。
压缩格式的 `<diagram>` 只解出节点、连线供展示，`storage="drawio_compressed"`，标准编辑器只读。只建模第一页；
写回时只改第一页里内核认得的单元格，别的单元格与其他页原样保留（`render`）。
"""

from __future__ import annotations

import base64
import html
import json
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field

MAX_BYTES = 5 * 1024 * 1024
_MAX_INFLATED = 4 * MAX_BYTES

STORAGES = ("standard", "drawio_compressed")
NODE_TYPES = ("start", "step", "decision", "data", "end")
EDGE_TYPES = ("seq", "cond", "exc", "data")
REF_KINDS = ("object", "operation", "rule", "metric", "script", "flowchart")
PROCESS_LISTS = ("roles", "inputs", "exceptions", "handoffs")
RESERVED_IDS = ("0", "1")

NODE_SIZES = {
    "start": (120, 50), "end": (120, 50), "step": (160, 60), "decision": (120, 80), "data": (140, 60),
}

_NODE_STYLES = {
    "start": "ellipse;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;",
    "end": "ellipse;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;",
    "step": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "decision": "rhombus;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;",
    "data": ("shape=parallelogram;perimeter=parallelogramPerimeter;fixedSize=1;whiteSpace=wrap;html=1;"
             "fillColor=#f5f5f5;strokeColor=#666666;"),
}
_EDGE_BASE = "edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;"
_EDGE_STYLES = {
    "seq": _EDGE_BASE + "endArrow=block;endFill=1;",
    "cond": _EDGE_BASE + "endArrow=block;endFill=1;",
    "exc": _EDGE_BASE + "endArrow=block;endFill=1;dashed=1;strokeColor=#b85450;fontColor=#b85450;",
    "data": _EDGE_BASE + "endArrow=open;dashed=1;dashPattern=1 3;strokeColor=#666666;",
}
_GRAPH_ATTRS = {
    "dx": "1200", "dy": "800", "grid": "1", "gridSize": "10", "guides": "1", "tooltips": "1",
    "connect": "1", "arrows": "1", "fold": "1", "page": "1", "pageScale": "1",
    "pageWidth": "1169", "pageHeight": "827", "math": "0", "shadow": "0",
}

_TERMINAL_SHAPES = {
    "ellipse", "doubleEllipse", "mxgraph.flowchart.terminator", "mxgraph.flowchart.start_1",
    "mxgraph.flowchart.start_2",
}
_DECISION_SHAPES = {"rhombus", "mxgraph.flowchart.decision"}
_DATA_SHAPES = {
    "parallelogram", "cylinder", "cylinder3", "document", "datastore", "mxgraph.flowchart.data",
    "mxgraph.flowchart.database", "mxgraph.flowchart.document", "mxgraph.flowchart.stored_data",
}
_CLASS_OF = {"start": "terminal", "end": "terminal", "step": "step", "decision": "decision", "data": "data"}

_NODE_ATTRS = ("description", "refs", "pending", "pending_note")
_ROOT_ATTRS = ("process_name", "process_description", *(f"process_{k}" for k in PROCESS_LISTS), "operation")

_UNSAFE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_HTML_HINT = re.compile(r"<\s*/|<\s*(br|div|p|span|b|i|u|font)\b|&(nbsp|lt|gt|amp|quot|#\d+);", re.IGNORECASE)
_BREAK_TAG = re.compile(r"<\s*(br|/?(div|p|li|tr|h[1-6]))\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_DEFAULT_PAGE = re.compile(r"^(Page-\d+|第\s*\d+\s*页)$")


class FlowchartError(ValueError):
    """draw.io 内容或 FlowchartModel 形状不合法。"""


@dataclass
class _Cell:
    id: str
    parent: str | None
    label: str
    style: str
    vertex: bool
    edge: bool
    source: str | None
    target: str | None
    x: float
    y: float
    w: float | None
    h: float | None
    attrs: dict[str, str]


@dataclass
class _Doc:
    model: dict
    carried: dict[str, set[str]] = field(default_factory=dict)   # 单元格 id → 文件里带了的内核属性
    root_carried: set[str] = field(default_factory=set)
    sizes: dict[str, tuple[str, float, float]] = field(default_factory=dict)   # 节点 id → (类型, 宽, 高)
    page_name: str = ""
    page_id: str = ""


# ── 公开接口 ────────────────────────────────────────────────────────────────

def parse(data: bytes | str, *, key: str = "", include_xml: bool = False) -> dict:
    """draw.io → FlowchartModel；`include_xml` 为真时标准格式也带回原文。"""
    return _parse_doc(data, key=key, include_xml=include_xml).model


def render(model: dict, *, previous: str | bytes | None = None) -> str:
    """FlowchartModel → 不压缩的 draw.io XML。

    `previous` 是要改的那份文件（标准编辑器保存时是上一版，高级编辑器保存时是提交的 XML）：只改第一页里
    内核认得的节点、连线与根单元格上的流程元数据，别的单元格（泳道、分组、批注、连线上的文字）与其他页按 id
    原样保留；没改位置、类型的节点不动坐标和样式。解析不了时当作没有，整份按模型写。
    """
    m = normalize(model)
    _check_ids(m)
    base = _base_file(previous)
    if base is not None:
        return _merge(m, *base)

    mxfile = ET.Element("mxfile", {"host": "onto-modeling", "compressed": "false"})
    diagram = ET.SubElement(mxfile, "diagram", {"id": "flowchart", "name": m["process"]["name"] or "Page-1"})
    graph = ET.SubElement(diagram, "mxGraphModel", dict(_GRAPH_ATTRS))
    root = ET.SubElement(graph, "root")
    root.append(_meta_element(m))
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    node_ids = {node["id"] for node in m["nodes"]}
    root.extend(_node_element(node) for node in m["nodes"])
    # 端点缺失的连线写进去 draw.io 打不开，校验会报 FLOW-EDGE-DANGLING
    root.extend(_edge_element(edge) for edge in m["edges"] if edge["from"] in node_ids and edge["to"] in node_ids)
    ET.indent(mxfile, space="  ")
    return ET.tostring(mxfile, encoding="unicode") + "\n"


def _meta_element(m: dict, existing: ET.Element | None = None) -> ET.Element:
    """根单元格 id="0"：流程元数据与所描述的操作；原来是带别的用户属性的 object 时保留那些属性。"""
    process = m["process"]
    attrs = dict(existing.attrib) if existing is not None and existing.tag in ("object", "UserObject") else {}
    attrs.update({"id": "0", "label": attrs.get("label", ""), "process_name": process["name"],
                  "process_description": process["description"]})
    for k in PROCESS_LISTS:
        attrs[f"process_{k}"] = _dumps(process[k])
    attrs["operation"] = _dumps(m["operation"]) if m["operation"] else ""
    obj = ET.Element(existing.tag if existing is not None and existing.tag == "UserObject" else "object", attrs)
    cell = existing.find("mxCell") if existing is not None and existing.tag != "mxCell" else None
    obj.append(cell if cell is not None else ET.Element("mxCell"))
    return obj


def _node_attrs(node: dict) -> dict:
    attrs = {"onto_type": node["type"], "description": node["description"], "refs": _dumps(node["refs"]),
             "pending": "1" if node["pending"] else "0"}
    if node["pending_note"] is not None:
        attrs["pending_note"] = node["pending_note"]
    return attrs


def _node_element(node: dict) -> ET.Element:
    ntype = node["type"] if node["type"] in NODE_TYPES else "step"
    w, h = NODE_SIZES[ntype]
    obj = ET.Element("object", {"id": node["id"], "label": _label_html(node["label"]), **_node_attrs(node)})
    cell = ET.SubElement(obj, "mxCell", {"style": _NODE_STYLES[ntype], "vertex": "1", "parent": "1"})
    ET.SubElement(cell, "mxGeometry", {
        "x": _fmt(node["x"] - w / 2), "y": _fmt(node["y"] - h / 2), "width": _fmt(w), "height": _fmt(h), "as": "geometry",
    })
    return obj


def _edge_element(edge: dict) -> ET.Element:
    etype = edge["type"] if edge["type"] in EDGE_TYPES else "seq"
    obj = ET.Element("object", {"id": edge["id"], "label": _label_html(edge["label"]), "onto_type": edge["type"],
                                "condition": edge["condition"]})
    cell = ET.SubElement(obj, "mxCell", {"style": _EDGE_STYLES[etype], "edge": "1", "parent": "1",
                                         "source": edge["from"], "target": edge["to"]})
    ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
    return obj


def _base_file(previous: str | bytes | None):
    """要在上面改的那份文件：(mxfile, 第一页 diagram, 第一页 mxGraphModel)；第一页是压缩的解开成明文写回。"""
    if previous is None:
        return None
    try:
        top = _xml(_text(previous))
        if top.tag == "mxGraphModel":
            mxfile = ET.Element("mxfile", {"host": "onto-modeling", "compressed": "false"})
            diagram = ET.SubElement(mxfile, "diagram", {"id": "flowchart", "name": ""})
            diagram.append(top)
            graph = top
        elif top.tag == "mxfile" and top.find("diagram") is not None:
            mxfile, diagram = top, top.find("diagram")
            graph = diagram.find("mxGraphModel")
            if graph is None:
                payload = (diagram.text or "").strip()
                if not payload:
                    return None
                graph = _inflate(payload)
                diagram.text = None
                diagram.append(graph)
        else:
            return None
    except FlowchartError:
        return None
    if graph.find("root") is None:
        ET.SubElement(graph, "root")
    return mxfile, diagram, graph


def _as_object(el: ET.Element) -> ET.Element:
    """裸 mxCell 改写成 object 包 mxCell（内核属性要写在 object 上），id 与文字挪到 object。"""
    if el.tag != "mxCell":
        return el
    obj = ET.Element("object", {"id": el.get("id", ""), "label": el.get("value", "")})
    el.attrib.pop("id", None)
    el.attrib.pop("value", None)
    obj.append(el)
    return obj


def _merge(m: dict, mxfile: ET.Element, diagram: ET.Element, graph: ET.Element) -> str:
    root = graph.find("root")
    cells = _cells(graph)
    by_id = {c.id: c for c in cells}
    before = _build(cells, key="", page_name=diagram.get("name", ""), storage="standard")
    old_nodes = {n["id"]: n for n in before.model["nodes"]}
    old_edges = {e["id"]: e for e in before.model["edges"]}
    managed = set(old_nodes) | set(old_edges)
    nodes = {n["id"]: n for n in m["nodes"]}
    edges = {e["id"]: e for e in m["edges"] if e["from"] in nodes and e["to"] in nodes}
    for cid in (*nodes, *edges):
        if cid in by_id and cid not in managed:
            raise FlowchartError(f"id「{cid}」和图里的另一个单元格（泳道、分组、批注等）重名，换一个 id")

    # 这次去掉的节点、连线，连同挂在它们上面的单元格一起删；改了文字的连线，原来挂着的文字单元格也删
    kept = set(nodes) | set(edges)
    relabeled = {eid for eid, e in edges.items() if eid in old_edges and e["label"] != old_edges[eid]["label"]}
    dropped = managed - kept
    grew = True
    while grew:
        grew = False
        for c in cells:
            if c.id in dropped or c.id in kept or c.id in RESERVED_IDS:
                continue
            if ({c.parent, c.source, c.target} & dropped) or (c.vertex and c.parent in relabeled):
                dropped.add(c.id)
                grew = True

    out: list[ET.Element] = []
    written: set[str] = set()
    for el in list(root):
        root.remove(el)
        cid = el.get("id")
        if cid == "0":
            out.append(_meta_element(m, el))
        elif cid in dropped:
            continue
        elif cid in nodes:
            out.append(_update_node(el, nodes[cid], old_nodes[cid], by_id, dropped))
        elif cid in edges:
            out.append(_update_edge(el, edges[cid], old_edges[cid]))
        else:
            out.append(el)
        if cid:
            written.add(cid)
    if "0" not in written:
        out.insert(0, _meta_element(m))
    if "1" not in written:
        out.insert(1, ET.Element("mxCell", {"id": "1", "parent": "0"}))
    out += [_node_element(node) for nid, node in nodes.items() if nid not in written]
    out += [_edge_element(edge) for eid, edge in edges.items() if eid not in written]
    root.extend(out)
    if not diagram.get("name"):
        diagram.set("name", m["process"]["name"] or "Page-1")
    ET.indent(mxfile, space="  ")
    return ET.tostring(mxfile, encoding="unicode") + "\n"


def _update_node(el: ET.Element, node: dict, old: dict, by_id: dict[str, _Cell], dropped: set[str]) -> ET.Element:
    obj = _as_object(el)
    if node["label"] != old["label"]:
        obj.set("label", _label_html(node["label"]))
    obj.attrib.pop("pending_note", None)
    obj.attrib.update(_node_attrs(node))
    cell = obj.find("mxCell")
    if cell is None:
        cell = ET.SubElement(obj, "mxCell", {"vertex": "1", "parent": "1"})
    ntype = node["type"] if node["type"] in NODE_TYPES else "step"
    retyped = node["type"] != old["type"]
    if retyped:
        cell.set("style", _NODE_STYLES[ntype])
    if cell.get("parent") in dropped:
        cell.set("parent", "1")
    geo = cell.find("mxGeometry")
    if geo is None:
        geo = ET.SubElement(cell, "mxGeometry", {"as": "geometry"})
    unmoved = not retyped and (node["x"], node["y"]) == (old["x"], old["y"])
    if unmoved and geo.get("width") and geo.get("height"):
        return obj
    w, h = NODE_SIZES[ntype]
    if not retyped and _float(geo.get("width")) and _float(geo.get("height")):
        w, h = _float(geo.get("width")), _float(geo.get("height"))
    ox, oy = _offset(cell.get("parent"), by_id)
    geo.attrib.update({"x": _fmt(node["x"] - w / 2 - ox), "y": _fmt(node["y"] - h / 2 - oy),
                       "width": _fmt(w), "height": _fmt(h)})
    return obj


def _update_edge(el: ET.Element, edge: dict, old: dict) -> ET.Element:
    obj = _as_object(el)
    if edge["label"] != old["label"]:
        obj.set("label", _label_html(edge["label"]))
    obj.attrib.update({"onto_type": edge["type"], "condition": edge["condition"]})
    cell = obj.find("mxCell")
    if cell is None:
        cell = ET.SubElement(obj, "mxCell", {"edge": "1", "parent": "1"})
        ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
    if edge["type"] != old["type"]:
        cell.set("style", _EDGE_STYLES[edge["type"] if edge["type"] in EDGE_TYPES else "seq"])
    cell.attrib.update({"source": edge["from"], "target": edge["to"]})
    return obj


def from_drawio_edit(xml: str, previous_model: dict | None) -> dict:
    """高级编辑器保存（契约 AR-05）：节点、连线以提交的 XML 为准；同 id 的节点在 XML 没带时沿用上一版的
    描述、引用、待确认标记，流程元数据与操作同理。"""
    doc = _parse_doc(xml, key="", include_xml=False)
    model = doc.model
    if not previous_model:
        return model
    prev = normalize(previous_model)

    prev_nodes = {n["id"]: n for n in prev["nodes"]}
    for node in model["nodes"]:
        old = prev_nodes.get(node["id"])
        if old is None:
            continue
        carried = doc.carried.get(node["id"], set())
        for k in _NODE_ATTRS:
            if k not in carried:
                node[k] = json.loads(json.dumps(old[k]))
    prev_edges = {e["id"]: e for e in prev["edges"]}
    for edge in model["edges"]:
        old = prev_edges.get(edge["id"])
        if old is not None and "condition" not in doc.carried.get(edge["id"], set()) and old["condition"]:
            edge["condition"] = old["condition"]

    process, old_process = model["process"], prev["process"]
    if "process_name" not in doc.root_carried and old_process["name"]:
        process["name"] = old_process["name"]
    if "process_description" not in doc.root_carried:
        process["description"] = old_process["description"]
    for k in PROCESS_LISTS:
        if f"process_{k}" in doc.root_carried:
            continue
        # 泳道给出的角色并到上一版的角色后面
        process[k] = _dedupe(old_process[k] + process[k]) if k == "roles" else list(old_process[k])
    if "operation" not in doc.root_carried:
        model["operation"] = dict(prev["operation"]) if prev["operation"] else None
    return model


def validate(model: dict) -> tuple[list[dict], list[dict]]:
    """确定性检查，返回 (errors, warnings)，field 为 `nodes/<id>`、`edges/<id>`。"""
    try:
        m = normalize(model)
    except FlowchartError as exc:
        return [_finding("FLOW-SHAPE", "error", str(exc))], []
    errors: list[dict] = []
    warnings: list[dict] = []
    seen: set[str] = set()

    def check_id(cid: str, what: str, index: int, path: str) -> bool:
        if not cid:
            errors.append(_finding("FLOW-ID", "error", f"第 {index + 1} 个{what}缺少 id", path,
                                   "给它补一个 id"))
            return False
        if cid in RESERVED_IDS:
            errors.append(_finding("FLOW-ID", "error", f"{what} id「{cid}」是 draw.io 保留的根、图层 id",
                                   f"{path}/{cid}", "换一个 id"))
            return False
        if cid in seen:
            errors.append(_finding("FLOW-ID", "error", f"{what} id「{cid}」重复", f"{path}/{cid}",
                                   "节点、连线的 id 在整张图里唯一"))
            return False
        seen.add(cid)
        return True

    types: dict[str, str] = {}
    labels: dict[str, str] = {}
    for i, node in enumerate(m["nodes"]):
        nid = node["id"]
        unique = check_id(nid, "节点", i, "nodes")
        path = f"nodes/{nid}" if nid else "nodes"
        name = node["label"].strip()
        if not name:
            errors.append(_finding("FLOW-LABEL", "error", "节点没有名称", path, "给节点写上名称"))
        if node["type"] not in NODE_TYPES:
            errors.append(_finding("FLOW-TYPE", "error", f"节点类型「{node['type']}」不认识", path,
                                   "可选 " + "、".join(NODE_TYPES)))
        if unique:
            types[nid] = node["type"]
            labels[nid] = name or nid
        if node["pending"]:
            note = (node["pending_note"] or "").strip()
            message = f"「{name or nid}」待确认" + (f"：{note}" if note else "")
            warnings.append(_finding("FLOW-PENDING", "warning", message, path, "业务确认后取消待确认标记"))

    links: list[tuple[str, str, str]] = []
    for i, edge in enumerate(m["edges"]):
        eid = edge["id"]
        check_id(eid, "连线", i, "edges")
        path = f"edges/{eid}" if eid else "edges"
        if edge["type"] not in EDGE_TYPES:
            errors.append(_finding("FLOW-TYPE", "error", f"连线类型「{edge['type']}」不认识", path,
                                   "可选 " + "、".join(EDGE_TYPES)))
        missing = [end for end in (edge["from"], edge["to"]) if end not in types]
        if missing:
            ends = "、".join(f"「{end}」" if end else "（空）" for end in missing)
            errors.append(_finding("FLOW-EDGE-DANGLING", "error", f"连线指向不存在的节点 {ends}", path,
                                   "删除这条连线或补上端点节点"))
        else:
            links.append((edge["from"], edge["to"], edge["type"]))
        if edge["type"] == "cond" and not edge["label"].strip() and not edge["condition"].strip():
            warnings.append(_finding("FLOW-COND-LABEL", "warning", "条件分支没有写条件", path,
                                     "写上分支条件，如「是」「否」"))

    starts = [nid for nid, t in types.items() if t == "start"]
    if not starts:
        warnings.append(_finding("FLOW-NO-START", "warning", "流程图没有开始节点", hint="加一个开始节点"))
    if not any(t == "end" for t in types.values()):
        warnings.append(_finding("FLOW-NO-END", "warning", "流程图没有结束节点", hint="加一个结束节点"))

    for nid, t in types.items():
        if t != "decision":
            continue
        out = sum(1 for a, _, et in links if a == nid and et != "data")
        if out < 2:
            warnings.append(_finding("FLOW-DECISION", "warning", f"判断节点「{labels[nid]}」只有 {out} 条分支",
                                     f"nodes/{nid}", "判断节点至少要有两条分支"))

    if starts:
        reached = _reachable(starts, links, types)
        for nid in types:
            if nid not in reached:
                warnings.append(_finding("FLOW-UNREACHABLE", "warning", f"从开始节点走不到「{labels[nid]}」",
                                         f"nodes/{nid}", "从上游连一条线过来，或删掉这个节点"))
    return errors, warnings


def summary(model: dict) -> str:
    nodes = model.get("nodes") if isinstance(model, dict) else None
    edges = model.get("edges") if isinstance(model, dict) else None
    n = len(nodes) if isinstance(nodes, list) else 0
    e = len(edges) if isinstance(edges, list) else 0
    return f"{n} 个节点 · {e} 条连线"


def normalize(model: dict) -> dict:
    """核对客户端提交的 FlowchartModel 的形状并补默认值；枚举值、id 唯一这类内容问题留给 validate。"""
    if not isinstance(model, dict):
        raise FlowchartError("流程图内容应为对象")
    storage = _str(model.get("storage"), "storage") or "standard"
    if storage not in STORAGES:
        raise FlowchartError(f"storage 只能是 {' 或 '.join(STORAGES)}")
    drawio_xml = model.get("drawio_xml")
    if drawio_xml is not None and not isinstance(drawio_xml, str):
        raise FlowchartError("drawio_xml 应为字符串")
    operation = model.get("operation")
    return {
        "storage": storage,
        "process": _norm_process(model.get("process")),
        "nodes": [_norm_node(n, f"nodes[{i}]") for i, n in enumerate(_list(model.get("nodes"), "nodes"))],
        "edges": [_norm_edge(e, f"edges[{i}]") for i, e in enumerate(_list(model.get("edges"), "edges"))],
        "drawio_xml": drawio_xml,
        "operation": None if operation is None else _norm_ref(operation, "operation"),
    }


# ── 解析 ────────────────────────────────────────────────────────────────────

def _parse_doc(data: bytes | str, *, key: str, include_xml: bool) -> _Doc:
    text = _text(data)
    top = _xml(text)
    page_name = page_id = ""
    storage = "standard"
    if top.tag == "mxfile":
        diagram = top.find("diagram")
        graph = None
        if diagram is not None:
            page_name, page_id = diagram.get("name", ""), diagram.get("id", "")
            graph = diagram.find("mxGraphModel")
            payload = (diagram.text or "").strip()
            if graph is None and payload:
                storage = "drawio_compressed"
                graph = _inflate(payload)
    elif top.tag == "mxGraphModel":
        graph = top
    else:
        raise FlowchartError(f"不是 draw.io 文件（根元素是 <{top.tag}>）")

    doc = _build(_cells(graph) if graph is not None else [], key=key, page_name=page_name, storage=storage)
    doc.page_name, doc.page_id = page_name, page_id
    if storage == "drawio_compressed" or include_xml:
        doc.model["drawio_xml"] = text
    return doc


def _text(data: bytes | str) -> str:
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if len(raw) > MAX_BYTES:
            raise FlowchartError("draw.io 文件超过 5 MiB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FlowchartError("draw.io 文件不是 UTF-8 编码") from exc
    elif isinstance(data, str):
        if len(data) > MAX_BYTES or len(data.encode("utf-8")) > MAX_BYTES:
            raise FlowchartError("draw.io 文件超过 5 MiB")
        text = data.lstrip("﻿")
    else:
        raise FlowchartError("draw.io 内容应为文本或字节")
    if not text.strip():
        raise FlowchartError("draw.io 文件为空")
    return text


def _xml(text: str) -> ET.Element:
    if _UNSAFE.search(text):
        raise FlowchartError("不接受带 DOCTYPE 或实体声明的 XML")
    try:
        return ET.fromstring(_DECL.sub("", text, count=1))
    except ET.ParseError as exc:
        raise FlowchartError(f"draw.io 文件不是合法的 XML：{exc}") from exc


def _inflate(payload: str) -> ET.Element:
    """压缩页面：base64 → raw deflate → URL 解码。"""
    try:
        raw = base64.b64decode(payload)
        inflater = zlib.decompressobj(-15)
        out = inflater.decompress(raw, _MAX_INFLATED)
        if inflater.unconsumed_tail:
            raise FlowchartError("压缩的流程图解开后过大")
        text = out.decode("utf-8")
    except (ValueError, zlib.error) as exc:
        raise FlowchartError("压缩的流程图内容解不开") from exc
    if not text.lstrip().startswith("<"):
        text = urllib.parse.unquote(text)
    if not text.strip():
        raise FlowchartError("压缩的流程图内容为空")
    graph = _xml(text)
    if graph.tag != "mxGraphModel":
        raise FlowchartError("压缩的流程图里没有 mxGraphModel")
    return graph


def _cells(graph: ET.Element) -> list[_Cell]:
    root = graph.find("root")
    if root is None:
        return []
    cells: dict[str, _Cell] = {}
    for el in root:
        if el.tag == "mxCell":
            cell, attrs, label = el, {}, el.get("value", "")
        elif el.tag in ("object", "UserObject"):
            cell = el.find("mxCell")
            if cell is None:
                cell = ET.Element("mxCell")
            attrs, label = dict(el.attrib), el.get("label", "")
        else:
            continue
        cid = el.get("id")
        if not cid or cid in cells:
            continue
        geo = cell.find("mxGeometry")
        get = geo.get if geo is not None else (lambda _k: None)
        cells[cid] = _Cell(
            id=cid, parent=cell.get("parent"), label=label, style=cell.get("style", "") or "",
            vertex=cell.get("vertex") == "1", edge=cell.get("edge") == "1",
            source=cell.get("source"), target=cell.get("target"),
            x=_float(get("x")) or 0.0, y=_float(get("y")) or 0.0,
            w=_float(get("width")), h=_float(get("height")), attrs=attrs,
        )
    return list(cells.values())


def _build(cells: list[_Cell], *, key: str, page_name: str, storage: str) -> _Doc:
    by_id = {c.id: c for c in cells}
    root_id = "0" if "0" in by_id and not by_id["0"].parent else next((c.id for c in cells if not c.parent), None)
    edge_ids = {c.id for c in cells if c.edge}

    lanes: list[_Cell] = []
    edge_labels: dict[str, list[str]] = {}
    pre: dict[str, str] = {}
    node_cells: list[_Cell] = []
    for c in cells:
        if not c.vertex or c.edge:
            continue
        if c.parent in edge_ids:
            text = _plain(c.label, c.style)
            if text:
                edge_labels.setdefault(c.parent, []).append(text)
            continue
        kind = _vertex_kind(c)
        if kind == "lane":
            lanes.append(c)
        if kind in ("lane", "group", "ignore"):
            continue
        onto = c.attrs.get("onto_type")
        pre[c.id] = onto if onto in NODE_TYPES and kind in ("step", _CLASS_OF[onto]) else kind
        node_cells.append(c)

    edges: list[dict] = []
    carried: dict[str, set[str]] = {}
    indeg: dict[str, int] = {}
    outdeg: dict[str, int] = {}
    for c in cells:
        if not c.edge or c.source not in pre or c.target not in pre:
            continue
        label = _plain(c.label, c.style) or " ".join(edge_labels.get(c.id, []))
        etype = _edge_type(c, label, pre[c.source])
        if "condition" in c.attrs:
            condition = c.attrs["condition"]
        else:
            condition = label if etype == "cond" else ""
        edges.append({"id": c.id, "type": etype, "from": c.source, "to": c.target,
                      "label": label, "condition": condition})
        carried[c.id] = {"condition"} & c.attrs.keys()
        outdeg[c.source] = outdeg.get(c.source, 0) + 1
        indeg[c.target] = indeg.get(c.target, 0) + 1

    nodes: list[dict] = []
    sizes: dict[str, tuple[str, float, float]] = {}
    for c in node_cells:
        ntype = pre[c.id]
        if ntype == "terminal":
            ntype = "start" if not indeg.get(c.id) else "end" if not outdeg.get(c.id) else "step"
        dw, dh = NODE_SIZES[ntype]
        w = c.w if c.w is not None else dw
        h = c.h if c.h is not None else dh
        ox, oy = _offset(c.parent, by_id)
        attrs = c.attrs
        has_desc = "description" in attrs or "tooltip" in attrs
        note = attrs.get("pending_note")
        nodes.append({
            "id": c.id, "type": ntype, "label": _plain(c.label, c.style),
            "description": attrs.get("description", attrs.get("tooltip", "")),
            "x": _num(ox + c.x + w / 2), "y": _num(oy + c.y + h / 2),
            "refs": _refs_attr(attrs.get("refs")),
            "pending": attrs.get("pending", "").strip().lower() in ("1", "true", "yes"),
            "pending_note": note if note else None,
        })
        carried[c.id] = {k for k in _NODE_ATTRS if k in attrs} | ({"description"} if has_desc else set())
        if c.w is not None and c.h is not None:
            sizes[c.id] = (ntype, c.w, c.h)

    meta = by_id[root_id].attrs if root_id in by_id else {}
    root_carried = {k for k in _ROOT_ATTRS if k in meta}
    name = meta.get("process_name", "")
    if not name:
        root_carried.discard("process_name")
        page = page_name if not _DEFAULT_PAGE.match(page_name or "") else ""
        name = page or key or page_name
    pools = {lane.parent for lane in lanes}
    lane_roles = [_plain(lane.label, lane.style) for lane in lanes if lane.id not in pools]
    roles = _dedupe(_list_attr(meta.get("process_roles")) + [r for r in lane_roles if r])
    process = {"name": name, "description": meta.get("process_description", "")}
    for k in PROCESS_LISTS:
        process[k] = roles if k == "roles" else _list_attr(meta.get(f"process_{k}"))
    model = {
        "storage": storage,
        "process": process,
        "nodes": nodes,
        "edges": edges,
        "drawio_xml": None,
        "operation": _ref_attr(meta.get("operation")),
    }
    return _Doc(model=model, carried=carried, root_carried=root_carried, sizes=sizes)


def _vertex_kind(c: _Cell) -> str:
    if c.attrs.get("onto_type") == "note":
        return "ignore"
    names = _style_names(c.style)
    if "swimlane" in c.style:
        return "lane"
    if "group" in names:
        return "group"
    if names & {"text", "edgeLabel"}:
        return "ignore"
    if names & _TERMINAL_SHAPES:
        return "terminal"
    if names & _DECISION_SHAPES:
        return "decision"
    if names & _DATA_SHAPES:
        return "data"
    return "step"


def _edge_type(c: _Cell, label: str, source_type: str) -> str:
    style = _style_map(c.style)
    dashed = style.get("dashed") == "1"
    onto = c.attrs.get("onto_type")
    if onto in EDGE_TYPES and (not dashed or onto in ("exc", "data")):
        return onto
    if dashed:
        return "exc" if _is_red(style.get("strokeColor", "")) or "异常" in label else "data"
    return "cond" if source_type == "decision" else "seq"


def _offset(parent: str | None, by_id: dict[str, _Cell]) -> tuple[float, float]:
    """容器（泳道、分组）里的子单元格坐标相对父级，累加到绝对坐标。"""
    x = y = 0.0
    seen: set[str] = set()
    while parent and parent not in seen:
        seen.add(parent)
        cell = by_id.get(parent)
        if cell is None or not cell.vertex:
            break
        x, y = x + cell.x, y + cell.y
        parent = cell.parent
    return x, y


def _reachable(starts: list[str], links: list[tuple[str, str, str]], types: dict[str, str]) -> set[str]:
    out: dict[str, list[str]] = {}
    for a, b, _ in links:
        out.setdefault(a, []).append(b)
    reached = set(starts)
    stack = list(starts)
    while stack:
        for nxt in out.get(stack.pop(), []):
            if nxt not in reached:
                reached.add(nxt)
                stack.append(nxt)
    # 数据节点常是被读的一方（数据 → 步骤），挨着走得到的节点就算走得到
    for a, b, _ in links:
        if types.get(a) == "data" and b in reached:
            reached.add(a)
        if types.get(b) == "data" and a in reached:
            reached.add(b)
    return reached


# ── 小工具 ──────────────────────────────────────────────────────────────────

def _style_map(style: str) -> dict[str, str]:
    out = {}
    for token in (style or "").split(";"):
        k, sep, v = token.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def _style_names(style: str) -> set[str]:
    """样式里的形状名：不带等号的样式名（如 `ellipse;`）加上 `shape=` 的值。"""
    names = {t.strip() for t in (style or "").split(";") if t.strip() and "=" not in t}
    shape = _style_map(style).get("shape")
    if shape:
        names.add(shape)
    return names


def _is_red(color: str) -> bool:
    c = color.strip().lower()
    if c in ("red", "darkred", "crimson", "firebrick"):
        return True
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", c)
    if not m:
        return False
    hexes = m.group(1) if len(m.group(1)) == 6 else "".join(ch * 2 for ch in m.group(1))
    r, g, b = (int(hexes[i:i + 2], 16) for i in (0, 2, 4))
    return r >= 0x99 and g <= 0x70 and b <= 0x70 and r - max(g, b) >= 0x50


def _plain(value: str, style: str) -> str:
    """单元格文字转纯文本：HTML 标签、`&nbsp;`、实体去掉，空白压成一个空格。"""
    text = value or ""
    if "html=1" in (style or "") or _HTML_HINT.search(text):
        text = _BREAK_TAG.sub(" ", text)
        text = _TAG.sub("", text)
        text = html.unescape(text)
    return " ".join(text.split())


def _label_html(label: str) -> str:
    return html.escape(label, quote=False).replace("\n", "<br>")


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except ValueError:
        return None
    return num if math.isfinite(num) else None


def _num(value: float) -> int | float:
    value = round(value, 2)
    return int(value) if value.is_integer() else value


def _fmt(value: float) -> str:
    value = round(float(value), 2)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _list_attr(value: str | None) -> list[str]:
    """列表属性：内核写的是 JSON 数组；人在 draw.io 里手填的按逗号、顿号、分号、换行拆。"""
    value = (value or "").strip()
    if not value:
        return []
    try:
        data = json.loads(value)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, str)]
    return [part.strip() for part in re.split(r"[,，、;；\n]", value) if part.strip()]


def _refs_attr(value: str | None) -> list[dict]:
    try:
        data = json.loads(value) if value else []
    except ValueError:
        return []
    refs = []
    for item in data if isinstance(data, list) else []:
        try:
            refs.append(_norm_ref(item, "refs"))
        except FlowchartError:
            continue
    return refs


def _ref_attr(value: str | None) -> dict | None:
    try:
        return _norm_ref(json.loads(value), "operation") if value else None
    except (ValueError, FlowchartError):
        return None


def _check_ids(m: dict) -> None:
    seen: set[str] = set()
    for item in [*m["nodes"], *m["edges"]]:
        cid = item["id"]
        if not cid or cid in RESERVED_IDS or cid in seen:
            raise FlowchartError(f"节点、连线 id「{cid}」为空、重复或是保留 id，不能写出 draw.io")
        seen.add(cid)


def _finding(code: str, level: str, message: str, field_path: str | None = None, hint: str | None = None) -> dict:
    out = {"code": code, "level": level, "message": message}
    if field_path:
        out["field"] = field_path
    if hint:
        out["hint"] = hint
    return out


# ── 形状核对 ────────────────────────────────────────────────────────────────

def _list(value, path: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FlowchartError(f"{path} 应为数组")
    return value


def _str(value, path: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise FlowchartError(f"{path} 应为字符串")
    return value


def _number(value, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FlowchartError(f"{path} 应为数字")
    if not math.isfinite(value):
        raise FlowchartError(f"{path} 应为有限数字")
    return value


def _dict(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise FlowchartError(f"{path} 应为对象")
    return value


def _norm_process(value) -> dict:
    p = _dict(value, "process") if value is not None else {}
    out = {"name": _str(p.get("name"), "process.name"),
           "description": _str(p.get("description"), "process.description")}
    for k in PROCESS_LISTS:
        items = _list(p.get(k), f"process.{k}")
        if not all(isinstance(item, str) for item in items):
            raise FlowchartError(f"process.{k} 的每一项应为字符串")
        out[k] = list(items)
    return out


def _norm_ref(value, path: str) -> dict:
    ref = _dict(value, path)
    kind = _str(ref.get("kind"), f"{path}.kind")
    if kind not in REF_KINDS:
        raise FlowchartError(f"{path}.kind 只能是 {'、'.join(REF_KINDS)}")
    key = _str(ref.get("key"), f"{path}.key")
    rid = _str(ref.get("id"), f"{path}.id")
    if not key and rid.startswith(f"{kind}:"):
        key = rid[len(kind) + 1:]
    if not key:
        raise FlowchartError(f"{path} 缺少 key")
    name = _str(ref.get("name"), f"{path}.name") or key
    return {"kind": kind, "id": rid or f"{kind}:{key}", "key": key, "name": name}


def _norm_node(value, path: str) -> dict:
    n = _dict(value, path)
    if "x" not in n or "y" not in n:
        raise FlowchartError(f"{path} 缺少坐标 x、y")
    note = _str(n.get("pending_note"), f"{path}.pending_note")
    pending = n.get("pending", False)
    if not isinstance(pending, bool):
        raise FlowchartError(f"{path}.pending 应为布尔值")
    return {
        "id": _str(n.get("id"), f"{path}.id"),
        "type": _str(n.get("type"), f"{path}.type"),
        "label": _str(n.get("label"), f"{path}.label"),
        "description": _str(n.get("description"), f"{path}.description"),
        "x": _number(n.get("x"), f"{path}.x"),
        "y": _number(n.get("y"), f"{path}.y"),
        "refs": [_norm_ref(r, f"{path}.refs[{i}]") for i, r in enumerate(_list(n.get("refs"), f"{path}.refs"))],
        "pending": pending,
        "pending_note": note or None,
    }


def _norm_edge(value, path: str) -> dict:
    e = _dict(value, path)
    return {
        "id": _str(e.get("id"), f"{path}.id"),
        "type": _str(e.get("type"), f"{path}.type"),
        "from": _str(e.get("from"), f"{path}.from"),
        "to": _str(e.get("to"), f"{path}.to"),
        "label": _str(e.get("label"), f"{path}.label"),
        "condition": _str(e.get("condition"), f"{path}.condition"),
    }
