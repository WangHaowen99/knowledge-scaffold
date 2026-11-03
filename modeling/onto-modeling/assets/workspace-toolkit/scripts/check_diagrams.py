#!/usr/bin/env python3
"""Unified diagram structure check — consolidates basic + deep mode.

Modes:
    --mode basic   Directory-wide scan, issue listing only (no fix suggestions)
    --mode deep    File-level scan (supports incremental) + fix suggestions (default)

Usage:
    python3 .claude/scripts/check_diagrams.py                                  # deep, default dir
    python3 .claude/scripts/check_diagrams.py ontology/diagrams/                # deep, given dir
    python3 .claude/scripts/check_diagrams.py a.drawio b.drawio                 # deep, incremental
    python3 .claude/scripts/check_diagrams.py --mode basic ontology/diagrams/   # basic, given dir
"""

import argparse
import html
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DIR = "ontology/diagrams"
LEGEND_NAME = "图示说明定义.drawio"
# 资料区里的图例：存量项目建项目时从资料区模板复制来的（模板里已去掉），有就用它
DEFAULT_LEGEND = "resource/diagrams/" + LEGEND_NAME
# 随 core 包的图例（装配目录 reference/）
PACKAGED_LEGEND = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "reference", LEGEND_NAME)
try:
    REPORT_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows may not ship an IANA timezone database. Report time stays UTC+8.
    REPORT_TZ = timezone(timedelta(hours=8))

FIX_SUGGESTIONS = [
    (r"缺少开始节点",              "添加 terminator 节点，连接到流程第一个节点"),
    (r"缺少结束节点",              "添加 terminator 节点，连接到流程最后一个节点"),
    (r"孤立节点",                  "确认是否应删除，或补充入边和出边"),
    (r"死路节点（有入无出）",       "从该节点添加边，连接到逻辑上的下一节点或结束节点"),
    (r"无入边节点",                "从上一个逻辑节点添加边连接到此节点"),
    (r"判断节点出边=\d+（至少应为2）",  "补全缺失的分支边；二值判断补正反两支，分类判断按枚举分类补齐，必要时增加默认/其他分支"),
    (r"悬空连线 起点缺失",         "在画布上找到该连线（终点节点已知），拖拽起点连接到正确的源节点"),
    (r"悬空连线 终点缺失",         "在画布上找到该连线（起点节点已知），拖拽终点连接到正确的目标节点"),
    (r"悬空连线 起点和终点均缺失", "在 XML 中定位该连线，手动补全 source 和 target 属性"),
]


def fix_suggestion(issue):
    for pattern, suggestion in FIX_SUGGESTIONS:
        if re.search(pattern, issue):
            return suggestion
    return "人工确认"


# ── 解析 ────────────────────────────────────────────────────────────────────

def parse_diagram(filepath):
    tree = ET.parse(filepath)
    cells = {}
    parents = {}
    wrappers = {child: parent for parent in tree.iter() for child in parent}
    pages = list(tree.getroot().iter("mxGraphModel"))
    page_prefixes = {cell: f"page{index}:" for index, page in enumerate(pages)
                     for cell in page.iter("mxCell")} if len(pages) > 1 else {}
    for cell in tree.getroot().iter("mxCell"):
        wrapper = wrappers.get(cell)
        cid = cell.get("id") or (
            wrapper.get("id") if wrapper is not None and wrapper.tag in ("object", "UserObject") else None
        )
        if not cid:
            if cell.get("edge") == "1" or cell.get("vertex") == "1":
                raise ValueError("节点或连线缺少 id，无法可靠核验结构")
            continue
        prefix = page_prefixes.get(cell, "")
        cid = prefix + cid
        if cid in cells:
            raise ValueError(f"节点或连线 id 重复：{cid}")
        cells[cid] = {
            "value":  cell.get("value", wrapper.get("label", "") if wrapper is not None else ""),
            "style":  cell.get("style", "") or "",
            "edge":   cell.get("edge") == "1",
            "vertex": cell.get("vertex") == "1",
            "source": prefix + cell.get("source") if cell.get("source") else None,
            "target": prefix + cell.get("target") if cell.get("target") else None,
        }
        parents[cid] = prefix + cell.get("parent") if cell.get("parent") else None
    return cells, parents


def clean_value(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def style_tokens(style):
    return {
        token.strip()
        for token in (style or "").split(";")
        if token.strip() and token.strip() not in {"html=1", "whiteSpace=wrap"}
    }


def find_legend(root="."):
    """图例：资料区里有的用资料区的，没有用随包的。"""
    own = os.path.join(root, DEFAULT_LEGEND)
    return own if os.path.isfile(own) else PACKAGED_LEGEND


def virtual_style_signatures(legend_path=None):
    legend_path = legend_path or find_legend()
    if not os.path.isfile(legend_path):
        return []

    try:
        cells, _ = parse_diagram(legend_path)
    except Exception:
        return []

    signatures = []
    for cell in cells.values():
        if not cell["edge"]:
            continue
        source = cell["source"]
        target = cell["target"]
        if source not in cells or target not in cells:
            continue
        target_text = clean_value(cells[target]["value"])
        if "虚拟项" not in target_text:
            continue
        signature = style_tokens(cells[source]["style"])
        if signature:
            signatures.append(signature)
    return signatures


def is_virtual_node(style, virtual_signatures):
    tokens = style_tokens(style)
    return any(signature <= tokens for signature in virtual_signatures)


def is_passthrough_node(style):
    return "shape=offPageConnector" in (style or "")


def classify(style):
    if "mxgraph.flowchart.terminator" in style:
        return "终止"
    if "shape=process" in style:
        return "子流程"
    if "rhombus" in style and "double=1" in style:
        return "子流程判断"
    if "rhombus" in style:
        return "判断"
    if "shape=card" in style:
        return "动作"
    if "shape=tape" in style:
        return "说明"
    if "shape=hexagon" in style:
        return "递归"
    if "swimlane" in style:
        return "循环"
    if "text;" in style:
        return "文本"
    if "edgeLabel" in style:
        return "边标签"
    if "rounded=1" in style:
        return "业务活动"
    if "whiteSpace=wrap" in style and "shape=" not in style:
        return "业务活动"
    return None


def is_annotation_target(cid, cells):
    if not cid or cid not in cells:
        return False
    style = cells[cid]["style"]
    if is_passthrough_node(style):
        return False
    return classify(style) in ("文本", "边标签")


# ── 结构检查（共享引擎） ─────────────────────────────────────────────────────

def check_diagram(filepath, legend_path=None):
    try:
        cells, _ = parse_diagram(filepath)
    except (ET.ParseError, ValueError, OSError) as exc:
        return {"nodes": 0, "edges": 0, "start": 0, "end": 0,
                "issues": [f"流程图无法解析：{exc}"], "dangling_count": 0}
    virtual_signatures = virtual_style_signatures(legend_path)

    edges = {k: v for k, v in cells.items() if v["edge"]}
    nodes = {}
    for k, v in cells.items():
        if not v["vertex"] or is_virtual_node(v["style"], virtual_signatures):
            continue
        kind = classify(v["style"])
        if kind and kind not in ("文本", "边标签"):
            nodes[k] = v

    all_out  = {nid: [] for nid in nodes}
    all_in   = {nid: [] for nid in nodes}
    flow_out = {nid: [] for nid in nodes}

    for eid, e in edges.items():
        s, t = e["source"], e["target"]
        if s in all_out:
            all_out[s].append(eid)
        if t in all_in:
            all_in[t].append(eid)
        if s in flow_out and not is_annotation_target(t, cells):
            flow_out[s].append(eid)

    node_types  = {k: classify(v["style"]) for k, v in nodes.items()}
    start_nodes = [k for k, t in node_types.items() if t == "终止" and not all_in.get(k)]
    end_nodes   = [k for k, t in node_types.items() if t == "终止" and not flow_out.get(k)]

    issues = []

    if not start_nodes:
        issues.append("缺少开始节点")
    if not end_nodes:
        issues.append("缺少结束节点")

    for nid, ntype in node_types.items():
        if ntype in ("文本", "说明", "边标签", "其他"):
            continue
        val      = re.sub(r"<[^>]+>", "", nodes[nid]["value"])[:30]
        has_in   = bool(all_in.get(nid))
        has_fout = bool(flow_out.get(nid))
        is_start = nid in start_nodes
        is_end   = nid in end_nodes

        is_isolated = not (has_in or has_fout or is_start or is_end)
        if is_isolated:
            if ntype == "循环":
                continue
            issues.append("孤立节点[%s]「%s」" % (ntype, val))
        elif not has_fout and ntype not in ("终止", "递归", "循环"):
            issues.append("死路节点（有入无出）[%s]「%s」" % (ntype, val))
        elif not has_in and not is_start:
            issues.append("无入边节点[%s]「%s」" % (ntype, val))

    for nid, ntype in node_types.items():
        if ntype in ("判断", "子流程判断"):
            cnt = len(flow_out.get(nid, []))
            if cnt < 2:
                val = re.sub(r"<[^>]+>", "", nodes[nid]["value"])[:30]
                issues.append("判断节点出边=%d（至少应为2）[%s]「%s」" % (cnt, ntype, val))

    dangling = []
    for eid, e in edges.items():
        absent = [key for key in ("source", "target") if e[key] and e[key] not in cells]
        if absent:
            issues.append("连线 %s 引用了不存在的端点：%s" % (eid, "、".join(e[key] for key in absent)))
            dangling.append(eid)
        if not e["source"] or not e["target"]:
            label = re.sub(r"<[^>]+>", "", e["value"] or "").strip()
            if not e["source"] and e["target"]:
                nbr_val = re.sub(r"<[^>]+>", "", cells.get(e["target"], {}).get("value", "") or "").strip()[:20]
                context = "（终点：「%s」）" % nbr_val if nbr_val else ""
                issues.append("悬空连线 起点缺失%s%s" % (context, "，标签「%s」" % label if label else ""))
            elif e["source"] and not e["target"]:
                nbr_val = re.sub(r"<[^>]+>", "", cells.get(e["source"], {}).get("value", "") or "").strip()[:20]
                context = "（起点：「%s」）" % nbr_val if nbr_val else ""
                issues.append("悬空连线 终点缺失%s%s" % (context, "，标签「%s」" % label if label else ""))
            else:
                issues.append("悬空连线 起点和终点均缺失%s" % ("，标签「%s」" % label if label else ""))
            dangling.append(eid)

    return {
        "nodes": len(nodes), "edges": len(edges),
        "start": len(start_nodes), "end": len(end_nodes),
        "issues": issues,
        "dangling_count": len(dangling),
    }


# ── 路径解析 ───────────────────────────────────────────────────────────────

def resolve_paths(args):
    """Resolve CLI args to concrete .drawio file paths."""
    if not args:
        args = [DEFAULT_DIR]
    paths = []
    for arg in args:
        if os.path.isdir(arg):
            paths.extend(sorted(
                os.path.join(arg, f) for f in os.listdir(arg)
                if f.endswith(".drawio")
            ))
        elif arg.endswith(".drawio") and os.path.isfile(arg):
            paths.append(arg)
        else:
            print("跳过：%s（不是 .drawio 文件或不存在）" % arg)
    return paths


# ── 报告生成 ───────────────────────────────────────────────────────────────

def run(target_paths, mode="deep"):
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)

    incremental = (mode == "deep") and not any(os.path.isdir(p) for p in target_paths)
    suffix = "（增量）" if incremental else ""
    print("共 %d 个流程图待检查%s  模式=%s" % (len(target_paths), suffix, mode))
    print()

    check_results = []
    for fpath in target_paths:
        name = os.path.basename(fpath).replace(".drawio", "")
        result = check_diagram(fpath)
        issues = result["issues"]
        check_results.append({"name": name, "result": result, "has_issues": bool(issues)})
        status = "OK" if not issues else "!! (%d 个问题)" % len(issues)
        print("  [%s] %s" % (status, name))

    has_issues = [r for r in check_results if r["has_issues"]]
    clean = [r for r in check_results if not r["has_issues"]]

    now = datetime.now(REPORT_TZ)
    filename_stamp = now.strftime("%Y%m%d-%H%M%S")
    display_stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # Unified naming convention: {YYYYMMDD}-{HHMMSS}-{scope}-{phase}.md
    # scope = "diagrams" (always, for this script)
    # phase = "rule" for deep structural check, "check" for the lighter basic scan
    phase = "rule" if mode == "deep" else "check"
    report_path = os.path.join(reports_dir, "%s-diagrams-%s.md" % (filename_stamp, phase))

    if incremental:
        scope_label = "增量检查：%s" % "、".join(r["name"] for r in check_results)
    else:
        scope_label = "检查目录：`%s`" % (os.path.dirname(target_paths[0]) or DEFAULT_DIR)

    lines = [
        "# 流程图结构检查报告",
        "",
        "日期：%s Asia/Shanghai" % display_stamp,
        scope_label,
        "",
        "## 结构检查概览",
        "",
        "| 状态 | 数量 |",
        "|------|------|",
        "| 有问题 | %d |" % len(has_issues),
        "| 无问题 | %d |" % len(clean),
        "",
        "## 结构问题详情",
        "",
    ]

    for r in has_issues:
        res = r["result"]
        lines.append("### %s" % r["name"])
        lines.append("")
        lines.append("节点：%d　边：%d　开始节点：%d　结束节点：%d" % (
            res["nodes"], res["edges"], res["start"], res["end"]))
        lines.append("")
        if mode == "deep":
            lines.append("**发现的问题及修复建议：**")
            lines.append("")
            lines.append("| 问题 | 修复建议 |")
            lines.append("|------|----------|")
            for iss in res["issues"]:
                lines.append("| %s | %s |" % (iss, fix_suggestion(iss)))
        else:
            lines.append("**发现的问题：**")
            lines.append("")
            for iss in res["issues"]:
                lines.append("- %s" % iss)
        lines.append("")

    lines.append("## 无问题的流程图")
    lines.append("")
    for r in clean:
        res = r["result"]
        lines.append("- **%s**　节点：%d　边：%d" % (r["name"], res["nodes"], res["edges"]))
    lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print()
    print("=" * 65)
    print("[有问题] %d 个  [无问题] %d 个" % (len(has_issues), len(clean)))
    print("报告已保存：%s" % report_path)
    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="Check draw.io flowchart structure issues."
    )
    parser.add_argument("paths", nargs="*", help="Files or directories to check")
    parser.add_argument("--mode", choices=["basic", "deep"], default="deep",
                        help="basic = report only; deep = + fix suggestions + incremental (default)")
    args = parser.parse_args()

    target_paths = resolve_paths(args.paths)
    if not target_paths:
        print("没有找到 .drawio 文件")
        sys.exit(1)
    run(target_paths, mode=args.mode)


if __name__ == "__main__":
    main()
