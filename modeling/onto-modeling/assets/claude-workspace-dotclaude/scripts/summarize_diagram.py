#!/usr/bin/env python3
"""
将 draw.io 单张流程图提取为可读摘要，节点类型语义来自'图示说明定义.drawio'。

用法：
    python3 scripts/summarize_diagram.py <diagram.drawio> [legend.drawio]

示例：
    python3 scripts/summarize_diagram.py source/diagrams/物料可用周期.drawio source/图示说明定义.drawio
"""

import re
import sys
import os
import xml.etree.ElementTree as ET
from collections import Counter

DEFAULT_LEGEND = "resource/diagrams/图示说明定义.drawio"


def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&amp;", "&", s)
    return re.sub(r"\s+", " ", s).strip()


def style_tokens(style):
    return {
        token.strip()
        for token in (style or "").split(";")
        if token.strip() and token.strip() not in {"html=1", "whiteSpace=wrap"}
    }


def is_virtual_style(style, virtual_signatures):
    tokens = style_tokens(style)
    return any(signature <= tokens for signature in virtual_signatures)


def is_passthrough_style(style):
    return "shape=offPageConnector" in (style or "")


TYPE_ALIASES = {
    "开始/结束节点": "终止",
    "开始/结束": "终止",
    "终止节点": "终止",
    "业务活动节点": "业务活动",
    "判断节点": "判断",
    "子流程节点": "子流程",
    "动作节点": "动作",
    "说明节点": "说明",
    "子流程判断节点": "子流程判断",
    "递归节点": "递归",
    "虚拟项": "虚拟项",
}


def normalize_type_name(desc):
    raw = desc.split("：")[0].split(":")[0].strip()
    return TYPE_ALIASES.get(raw, raw)


def parse_cells(filepath):
    tree = ET.parse(filepath)
    cells = {}
    parents = {}
    for cell in tree.getroot().iter("mxCell"):
        cid = cell.get("id")
        if not cid:
            continue
        cells[cid] = {
            "value":  clean_html(cell.get("value", "")),
            "style":  cell.get("style", "") or "",
            "edge":   cell.get("edge") == "1",
            "vertex": cell.get("vertex") == "1",
            "source": cell.get("source"),
            "target": cell.get("target"),
        }
        parents[cid] = cell.get("parent")
    return cells, parents


# ── 从图例文件动态构建分类规则 ──────────────────────────────────────────────

def load_legend(legend_path):
    """
    解析图示说明定义，返回：
      - rules: list of (priority, style_keywords, short_name, description)
      - legend_text: 格式化的图例说明字符串（用于摘要前置）
      - virtual_signatures: 图例中标为"虚拟项"的样式签名
    """
    cells, parents = parse_cells(legend_path)

    shape_nodes = {
        k: v for k, v in cells.items()
        if v["vertex"] and "text;" not in v["style"] and "edgeLabel" not in v["style"] and v["style"]
    }
    text_nodes = {k: v for k, v in cells.items() if v["vertex"] and "text;" in v["style"]}

    pairs = []
    for eid, e in cells.items():
        if not e["edge"]:
            continue
        s, t = e["source"], e["target"]
        if s in shape_nodes and t in text_nodes:
            pairs.append((shape_nodes[s]["style"], text_nodes[t]["value"]))

    # 按样式特征定义优先级匹配规则
    # 顺序很重要：更具体的规则放在前面
    STYLE_PRIORITY = [
        "shape=rhombus;double=1",     # 子流程判断（比普通rhombus更具体）
        "rhombus",
        "shape=mxgraph.flowchart.terminator",
        "shape=process",
        "shape=card",
        "shape=tape",
        "shape=hexagon",
        "rounded=0",
    ]

    rules = []
    virtual_signatures = []
    for style, desc in pairs:
        name = normalize_type_name(desc)
        if name == "虚拟项":
            signature = style_tokens(style)
            if signature:
                virtual_signatures.append(signature)
            continue
        matched_kw = None
        priority = 99
        for i, kw in enumerate(STYLE_PRIORITY):
            if kw in style:
                matched_kw = kw
                priority = i
                break
        if matched_kw:
            rules.append((priority, matched_kw, name, desc))

    # 补充业务活动兜底：draw.io 默认圆角矩形可作为业务活动节点。
    rules.append((len(STYLE_PRIORITY), "rounded=1", "业务活动", "业务活动节点"))

    rules.sort(key=lambda x: x[0])

    # 生成图例说明文本
    lines = ["## 节点类型说明（来自图示说明定义）\n"]
    seen = set()
    for _, kw, name, desc in rules:
        if name not in seen:
            lines.append(f"- [{name}] {desc}")
            seen.add(name)
    legend_text = "\n".join(lines)

    return rules, legend_text, virtual_signatures


def make_classifier(rules, virtual_signatures):
    """根据规则列表生成分类函数。"""
    TEXT_STYLES = ("text;", "edgeLabel")

    def classify(style, value: str = ""):
        for text_kw in TEXT_STYLES:
            if text_kw in style:
                return None  # 文本/标签节点，排除
        if is_virtual_style(style, virtual_signatures):
            return None
        # 值含 [[...]] Wiki 链接的节点一律视为子流程节点
        # 此约定优先于样式匹配，因为图例与流程图实际样式可能不一致
        if value and "[[" in value and "]]" in value:
            return "子流程"
        for _, kw, name, _ in rules:
            if kw in style:
                return name
        return None

    return classify


# ── 摘要提取 ────────────────────────────────────────────────────────────────

def summarize(diagram_path, legend_path):
    rules, legend_text, virtual_signatures = load_legend(legend_path)
    classify = make_classifier(rules, virtual_signatures)

    cells, parents = parse_cells(diagram_path)
    diagram_name = os.path.basename(diagram_path).replace(".drawio", "")

    # 流程节点
    flow_nodes = {}
    for cid, c in cells.items():
        if not c["vertex"]:
            continue
        ntype = classify(c["style"], c.get("value", ""))
        if ntype:
            flow_nodes[cid] = {"type": ntype, "label": c["value"], "annotation": ""}

    # 同名节点编号
    label_count = Counter(n["label"] for n in flow_nodes.values())
    label_seen = Counter()
    node_display = {}
    for cid, n in flow_nodes.items():
        lbl = n["label"]
        if label_count[lbl] > 1:
            label_seen[lbl] += 1
            node_display[cid] = f"{lbl}({label_seen[lbl]})"
        else:
            node_display[cid] = lbl

    # 把文本说明节点内容附加到对应流程节点
    text_nodes = {k: v for k, v in cells.items() if v["vertex"] and "text;" in v["style"]}
    for eid, e in cells.items():
        if not e["edge"]:
            continue
        s, t = e["source"], e["target"]
        if s in flow_nodes and t in text_nodes and text_nodes[t]["value"]:
            if not flow_nodes[s]["annotation"]:
                flow_nodes[s]["annotation"] = text_nodes[t]["value"]
        if t in flow_nodes and s in text_nodes and text_nodes[s]["value"]:
            if not flow_nodes[t]["annotation"]:
                flow_nodes[t]["annotation"] = text_nodes[s]["value"]

    # 边标签
    edge_labels = {}
    for cid, c in cells.items():
        if c["vertex"] and "edgeLabel" in c["style"] and parents.get(cid):
            edge_labels[parents[cid]] = c["value"]

    # 流程边（排除指向文本/视图虚拟项的注释边，offPageConnector 透传）
    outgoing_edges = {}
    for eid, e in cells.items():
        if e["edge"] and e["source"]:
            outgoing_edges.setdefault(e["source"], []).append((eid, e))

    def is_text(cid):
        if not cid or cid not in cells:
            return False
        style = cells[cid]["style"]
        return "text;" in style or "edgeLabel" in style

    def is_passthrough(cid):
        if not cid or cid not in cells:
            return False
        return is_passthrough_style(cells[cid]["style"])

    def resolve_targets(cid, seen=None, through_passthrough=False):
        seen = seen or set()
        if not cid or cid in seen or cid not in cells:
            return []
        if cid in flow_nodes:
            return [(cid, through_passthrough)]
        if is_text(cid):
            return []
        if is_passthrough(cid):
            seen.add(cid)
            targets = []
            for _, out_edge in outgoing_edges.get(cid, []):
                targets.extend(resolve_targets(out_edge["target"], seen, True))
            return targets
        return []

    flow_edge_entries = []
    dangling_edges = []
    for eid, e in cells.items():
        if not e["edge"]:
            continue
        s, t = e["source"], e["target"]
        if is_text(t):
            continue
        if s and s not in flow_nodes:
            continue
        # 边标签可能存放在两处：(1) 独立 mxCell + style=edgeLabel 子单元；(2) 边自身的 value 属性
        label = edge_labels.get(eid, "") or e.get("value", "") or ""
        targets = resolve_targets(t)
        if not s:
            if targets:
                for target_id, _ in targets:
                    dangling_edges.append((label, node_display[target_id]))
            elif not (t and t in cells and is_virtual_style(cells[t]["style"], virtual_signatures)):
                dangling_edges.append((label, "【无目标】"))
        else:
            src = node_display[s]
            if targets:
                for target_id, through_passthrough in targets:
                    flow_edge_entries.append((src, label, node_display[target_id], through_passthrough))
            elif not (t and t in cells and is_virtual_style(cells[t]["style"], virtual_signatures)):
                flow_edge_entries.append((src, label, "【无目标】", False))

    direct_edge_keys = {
        (src, label, target)
        for src, label, target, through_passthrough in flow_edge_entries
        if not through_passthrough
    }
    seen_passthrough_edge_keys = set()
    flow_edges = []
    for src, label, target, through_passthrough in flow_edge_entries:
        key = (src, label, target)
        if through_passthrough:
            if key in direct_edge_keys or key in seen_passthrough_edge_keys:
                continue
            seen_passthrough_edge_keys.add(key)
        flow_edges.append((src, label, target))

    # 输出
    lines = [f"# {diagram_name} — 流程摘要\n"]
    lines.append(legend_text)
    lines.append("\n## 节点列表\n")
    for cid, n in flow_nodes.items():
        display = node_display[cid]
        lines.append(f"- [{n['type']}] {display}")
        if n["annotation"]:
            lines.append(f"  {n['annotation']}")

    lines.append("\n## 流程连接\n")
    for src, lbl, tgt in flow_edges:
        arrow = f"--{lbl}-->" if lbl else "-->"
        lines.append(f"- {src} {arrow} {tgt}")

    if dangling_edges:
        lines.append("\n## 未锚定的边（浮动起点）\n")
        for lbl, tgt in dangling_edges:
            arrow = f"--{lbl}-->" if lbl else "-->"
            lines.append(f"- 【浮动起点】 {arrow} {tgt}")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    diagram_file = sys.argv[1]
    legend_file = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LEGEND

    print(summarize(diagram_file, legend_file))
