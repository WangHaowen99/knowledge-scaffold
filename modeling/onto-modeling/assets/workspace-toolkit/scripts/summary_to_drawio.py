#!/usr/bin/env python3
"""Reverse bridge — convert a flowchart summary markdown back to .drawio XML.

Summary format (produced by summarize_diagram.py):
    # <name> — 流程摘要
    ## 节点列表
    - [类型] 节点名
      可选的注释行（缩进）
    ## 流程连接
    - src --标签--> tgt
    - src --> tgt

Node type → drawio style mapping is the inverse of summarize_diagram.classify().
Layout: auto-generated grid (non-technical users pick up where they left off —
they can tidy in drawio, but the graph already has valid source/target wiring).

Usage:
    python3 .claude/scripts/summary_to_drawio.py <summary.md> [out.drawio]
"""

import re
import sys
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

TYPE_STYLE = {
    '终止':       'strokeWidth=2;html=1;shape=mxgraph.flowchart.terminator;whiteSpace=wrap;fillStyle=auto;',
    '判断':       'rhombus;whiteSpace=wrap;html=1;',
    '子流程判断': 'shape=rhombus;double=1;perimeter=rhombusPerimeter;whiteSpace=wrap;html=1;align=center;fillColor=#ffe6cc;strokeColor=#d79b00;',
    '子流程':     'shape=process;whiteSpace=wrap;html=1;backgroundOutline=1;',
    '动作':       'shape=card;whiteSpace=wrap;html=1;',
    '说明':       'shape=tape;whiteSpace=wrap;html=1;size=0.2;',
    '递归':       'shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;fixedSize=1;fillColor=#FF3333;',
    '循环':       'swimlane;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;',
    '业务活动':   'rounded=1;whiteSpace=wrap;html=1;fillColor=default;strokeColor=#000000;',
}
TYPE_STYLE.update({
    '开始/结束': TYPE_STYLE['终止'],
    '开始/结束节点': TYPE_STYLE['终止'],
    '业务活动节点': TYPE_STYLE['业务活动'],
})
DEFAULT_STYLE = TYPE_STYLE['业务活动']

NODE_RE = re.compile(r'^-\s*\[(?P<type>[^\]]+)\]\s*(?P<label>.+)\s*$')


def parse_edge_line(line: str):
    """Parse `- src --> tgt` or `- src --label--> tgt`. Return (src, label, tgt) or None."""
    m = re.match(r'^-\s*(.+)$', line.strip())
    if not m:
        return None
    body = m.group(1)
    if '-->' not in body:
        return None
    left, tgt = body.rsplit('-->', 1)
    left = left.rstrip()
    tgt = tgt.strip()
    label = ''
    # Accept either "src --label" or "src"
    lm = re.match(r'^(.*?)\s*--(.*)$', left)
    if lm:
        src, label = lm.group(1).strip(), lm.group(2).strip()
    else:
        src = left.strip()
    if not src or not tgt:
        return None
    return src, label, tgt


def parse_summary(text: str) -> tuple:
    """Return (diagram_name, [(type, label)], [(src, label, tgt)])."""
    name = 'untitled'
    m = re.search(r'^#\s+(.+?)\s*(?:—|--)\s*流程摘要', text, re.MULTILINE)
    if m:
        name = m.group(1).strip()

    # Find 节点列表 / 流程连接 blocks
    sections = re.split(r'^##\s+', text, flags=re.MULTILINE)
    nodes = []
    edges = []
    for sec in sections:
        header, _, body = sec.partition('\n')
        header = header.strip()
        if header.startswith('节点列表'):
            for line in body.split('\n'):
                m = NODE_RE.match(line.strip())
                if m:
                    nodes.append((m.group('type').strip(), m.group('label').strip()))
        elif header.startswith('流程连接'):
            for line in body.split('\n'):
                parsed = parse_edge_line(line)
                if parsed:
                    edges.append(parsed)

    return name, nodes, edges


def layout(nodes: list, existing: Optional[dict] = None) -> dict:
    """Grid layout, reusing existing positions when they exist.

    When the user edits the summary after an earlier round-trip, we want new
    nodes to slot into empty grid cells while existing nodes keep the positions
    the user (or drawio) already arranged. This avoids wiping manual layout
    adjustments on every re-export.
    """
    existing = existing or {}
    positions = {}
    COLS = 3
    CELL_W, CELL_H = 200, 100
    PAD = 40

    # Reuse existing positions for nodes that already had one.
    reused_cells = set()
    for _, label in nodes:
        if label in existing:
            x, y, w, h = existing[label]
            positions[label] = (x, y, w, h)
            # Mark the grid cell this occupies so we don't overlay new nodes.
            col_guess = max(0, round((x - PAD) / (CELL_W + PAD)))
            row_guess = max(0, round((y - PAD) / (CELL_H + PAD)))
            reused_cells.add((row_guess, col_guess))

    # Assign grid cells to new nodes, skipping any cell already occupied.
    new_nodes = [label for _, label in nodes if label not in positions]
    cell_iter = iter(sorted(
        ((r, c) for r in range(max(50, len(nodes))) for c in range(COLS)),
        key=lambda rc: (rc[0], rc[1])
    ))
    for label in new_nodes:
        while True:
            try:
                row, col = next(cell_iter)
            except StopIteration:
                row, col = 0, 0
                break
            if (row, col) not in reused_cells:
                break
        x = PAD + col * (CELL_W + PAD)
        y = PAD + row * (CELL_H + PAD)
        positions[label] = (x, y, 140, 60)
        reused_cells.add((row, col))

    return positions


def read_existing_positions(path: Path) -> dict:
    """Extract {label: (x, y, w, h)} from a previously written .drawio file.

    Returns empty dict if the file doesn't exist or can't be parsed — this is
    a best-effort layout-preservation pass, not a correctness guarantee.
    """
    if not path.exists():
        return {}
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return {}
    positions = {}
    for cell in tree.iter('mxCell'):
        if cell.get('vertex') != '1':
            continue
        label = cell.get('value', '').strip()
        if not label:
            continue
        geom = cell.find('mxGeometry')
        if geom is None:
            continue
        try:
            x = float(geom.get('x', '0'))
            y = float(geom.get('y', '0'))
            w = float(geom.get('width', '140'))
            h = float(geom.get('height', '60'))
        except (TypeError, ValueError):
            continue
        positions[label] = (x, y, w, h)
    return positions


def build_drawio(name: str, nodes: list, edges: list, existing: Optional[dict] = None) -> str:
    positions = layout(nodes, existing=existing)
    node_ids = {label: f'node-{i+2}' for i, (_, label) in enumerate(nodes)}

    cells_xml = []
    for ntype, label in nodes:
        nid = node_ids[label]
        style = TYPE_STYLE.get(ntype, DEFAULT_STYLE)
        x, y, w, h = positions[label]
        cells_xml.append(
            f'<mxCell id="{nid}" value="{escape(label)}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )

    for i, (src, label, tgt) in enumerate(edges):
        eid = f'edge-{i+1000}'
        src_id = node_ids.get(src)
        tgt_id = node_ids.get(tgt)
        if not src_id or not tgt_id:
            continue  # skip edges that don't resolve
        cells_xml.append(
            f'<mxCell id="{eid}" value="{escape(label)}" '
            f'style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;exitX=0.5;exitY=1;entryX=0.5;entryY=0;"'
            f' edge="1" parent="1" source="{src_id}" target="{tgt_id}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<mxfile host="summary_to_drawio.py" modified="{name}">
  <diagram name="{escape(name)}" id="diagram-1">
    <mxGraphModel dx="1000" dy="700" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1169" pageHeight="826" math="0" shadow="0">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        {chr(10).join("        " + c for c in cells_xml)}
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
'''


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    summary_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else summary_path.with_suffix('.drawio')

    text = summary_path.read_text(encoding='utf-8')
    name, nodes, edges = parse_summary(text)

    if not nodes:
        print(f'Error: no nodes parsed from {summary_path}', file=sys.stderr)
        sys.exit(1)

    existing_positions = read_existing_positions(out_path)
    xml = build_drawio(name, nodes, edges, existing=existing_positions)
    out_path.write_text(xml, encoding='utf-8')

    unresolved = [e for e in edges if e[0] not in {lbl for _, lbl in nodes} or e[2] not in {lbl for _, lbl in nodes}]
    print(f'Wrote {out_path}  nodes={len(nodes)} edges={len(edges)} unresolved={len(unresolved)}')
    if unresolved:
        print('Unresolved edges (源或目标找不到对应节点):')
        for src, lbl, tgt in unresolved:
            print(f'  {src} --{lbl}--> {tgt}')


if __name__ == '__main__':
    main()
