#!/usr/bin/env python3
"""Generate ontology overview HTML report.

Usage:
    python3 .claude/scripts/generate_overview.py [BASE_DIR] [TOPIC]

BASE_DIR: ontology project root (default: current working directory)
TOPIC: short topic name embedded in report title and scale summary (default: '本体')

Output: {BASE_DIR}/reports/{YYYYMMDD}-{HHMMSS}-overview.html
Requires templates: {BASE_DIR}/.claude/templates/html-report-base.html
                    {BASE_DIR}/.claude/templates/overview-body.html
"""

import os
import re
import sys
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd().resolve()
TOPIC = sys.argv[2] if len(sys.argv) > 2 else '本体'
REPORT_TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime.now(REPORT_TZ)
NOW_STR = NOW.strftime('%Y%m%d-%H%M%S')
DISPLAY_STAMP = NOW.strftime('%Y-%m-%d %H:%M:%S') + ' Asia/Shanghai'

ONTOLOGY_DIR = BASE_DIR / 'ontology'
TEMPLATES_DIR = BASE_DIR / '.claude' / 'templates'
OUTPUT_PATH = BASE_DIR / 'reports' / f'{NOW_STR}-overview.html'

# ─── Parse Objects ───────────────────────────────────────────────────────────
object_files = sorted((ONTOLOGY_DIR / 'object').glob('*.md'))
logic_files = sorted((ONTOLOGY_DIR / 'logic').glob('*.md'))
script_dir = ONTOLOGY_DIR / 'scripts'
script_files = sorted(script_dir.glob('*.py')) if script_dir.exists() else []

OBJECTS_IDX = {}
LOGIC_IDX = {}

def parse_object(path):
    name = path.stem
    content = path.read_text(encoding='utf-8')

    aliases = []
    m = re.search(r'#\s*对象别名\s*\n((?:[-*].+\n)*)', content)
    if m:
        aliases = [l.lstrip('-* ').strip() for l in m.group(1).strip().splitlines() if l.strip()]

    desc = ''
    m = re.search(r'#\s*对象描述\s*\n(.*?)(?=\n##|\Z)', content, re.DOTALL)
    if m:
        desc = m.group(1).strip()[:200]

    attrs = []
    m = re.search(r'#\s*对象属性清单\s*\n\|.*?\n\|[-:\s|]+\n((?:\|.+\n)*)', content)
    if m:
        for line in m.group(1).strip().splitlines():
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if cells:
                attrs.append(cells[0])

    related = list(set(re.findall(r'\[\[([^\]]+)\]\]', content)))
    related = [r for r in related if r not in (name, 'logic')]

    return {
        'name': name,
        'aliases': aliases,
        'description': desc,
        'attributes': attrs,
        'related': related,
        'used_by': []
    }

for f in object_files:
    OBJECTS_IDX[f.stem] = parse_object(f)

# ─── Parse Logics ─────────────────────────────────────────────────────────────
def parse_logic(path):
    name = path.stem
    content = path.read_text(encoding='utf-8')

    desc = ''
    m = re.search(r'#\s*逻辑描述\s*\n(.*?)(?=\n##|\Z)', content, re.DOTALL)
    if m:
        desc = m.group(1).strip()[:200]

    inputs, outputs, supports = [], [], []
    for line in content.splitlines():
        m = re.match(r'-\s*输入数据的对象\s*[:：]\s*(.*)', line)
        if m:
            inputs.extend(re.findall(r'\[\[([^\]]+)\]\]', m.group(1)))
        m = re.match(r'-\s*输出数据的对象\s*[:：]\s*(.*)', line)
        if m:
            outputs.extend(re.findall(r'\[\[([^\]]+)\]\]', m.group(1)))
        m = re.match(r'-\s*支撑数据的对象\s*[:：]\s*(.*)', line)
        if m:
            supports.extend(re.findall(r'\[\[([^\]]+)\]\]', m.group(1)))

    calls = []
    m = re.search(r'#\s*逻辑描述\s*\n(.*?)(?=\n##|\Z)', content, re.DOTALL)
    if m:
        calls = re.findall(r'调用\[\[([^\]]+)\]\]逻辑文档', m.group(1))

    return {
        'name': name,
        'description': desc,
        'inputs': inputs,
        'outputs': outputs,
        'supports': supports,
        'calls': calls,
        'called_by': []
    }

for f in logic_files:
    LOGIC_IDX[f.stem] = parse_logic(f)

# ─── Cross-link: which logics reference each object ───────────────────────────
for lname, ldata in LOGIC_IDX.items():
    for obj_list in [ldata['inputs'], ldata['outputs'], ldata['supports']]:
        for oname in obj_list:
            if oname in OBJECTS_IDX:
                OBJECTS_IDX[oname]['used_by'].append(lname)

# ─── Cross-link: logic call graph ───────────────────────────────────────────
for lname, ldata in LOGIC_IDX.items():
    for callee in ldata['calls']:
        if callee in LOGIC_IDX:
            LOGIC_IDX[callee]['called_by'].append(lname)

# ─── Compute derived metrics ──────────────────────────────────────────────────
object_use_count = {oname: len(odata['used_by']) for oname, odata in OBJECTS_IDX.items()}
orphan_objects = [oname for oname, cnt in object_use_count.items() if cnt == 0]
top_logic = [lname for lname, ldata in LOGIC_IDX.items() if not ldata['called_by']]

missing_refs = []
for lname, ldata in LOGIC_IDX.items():
    for oname in list(ldata['inputs']) + list(ldata['outputs']) + list(ldata['supports']):
        if oname not in OBJECTS_IDX:
            missing_refs.append((lname, oname, 'object'))
    for cname in ldata['calls']:
        if cname not in LOGIC_IDX:
            missing_refs.append((lname, cname, 'logic'))
for oname, odata in OBJECTS_IDX.items():
    for rname in odata['related']:
        if rname not in OBJECTS_IDX and rname not in LOGIC_IDX:
            missing_refs.append((oname, rname, 'related'))

sorted_objects = sorted(object_use_count.items(), key=lambda x: -x[1])[:20]

# ─── Build vis.js nodes/edges ──────────────────────────────────────────────────
max_use = max(object_use_count.values()) if object_use_count else 1
top_10pct_threshold = max_use * 0.9

object_nodes = []
object_edges = []
for oname, odata in OBJECTS_IDX.items():
    cnt = object_use_count[oname]
    if cnt == 0:
        color = {'background': '#ffd8a8', 'border': '#bf8700'}
        font_color = '#7d4e00'
    elif cnt >= top_10pct_threshold:
        color = {'background': '#0969da', 'border': '#0969da'}
        font_color = '#fff'
    else:
        color = {'background': '#9ec5ff', 'border': '#0969da'}
        font_color = '#1f2328'

    object_nodes.append({
        'id': oname, 'label': oname,
        'value': max(cnt, 1),
        'color': color,
        'font': {'color': font_color},
        'title': f"{oname}\n被 {cnt} 个逻辑引用" if cnt else oname
    })

for oname, odata in OBJECTS_IDX.items():
    for rname in odata['related']:
        if rname in OBJECTS_IDX:
            object_edges.append({'from': oname, 'to': rname, 'arrows': 'to'})

tool_logics = {lname for lname, ldata in LOGIC_IDX.items() if len(ldata['called_by']) >= 3}
logic_nodes = []
logic_edges = []
for lname, ldata in LOGIC_IDX.items():
    if ldata['called_by']:
        color = {'background': '#c7eed8', 'border': '#1a7f37'}
    elif lname in top_logic:
        color = {'background': '#ffd8a8', 'border': '#bf8700'}
    else:
        color = {'background': '#a7f0ba', 'border': '#1a7f37'}

    logic_nodes.append({
        'id': lname, 'label': lname, 'color': color,
        'title': f"{lname}\n调用 {len(ldata['calls'])} 个子流程"
    })

for lname, ldata in LOGIC_IDX.items():
    for callee in ldata['calls']:
        if callee in LOGIC_IDX:
            logic_edges.append({'from': lname, 'to': callee, 'arrows': 'to'})

object_meta = {oname: {
    'name': oname,
    'aliases': odata['aliases'],
    'description': odata['description'],
    'used_by': odata['used_by'],
    'related': odata['related']
} for oname, odata in OBJECTS_IDX.items()}

logic_meta = {lname: {
    'name': lname,
    'description': ldata['description'],
    'is_top': lname in top_logic,
    'inputs': ldata['inputs'],
    'outputs': ldata['outputs'],
    'supports': ldata['supports'],
    'calls': ldata['calls'],
    'called_by': ldata['called_by']
} for lname, ldata in LOGIC_IDX.items()}

N_OBJECTS = len(OBJECTS_IDX)
N_LOGICS = len(LOGIC_IDX)
N_SCRIPTS = len(script_files)
N_ACTIONS = 0  # action phase 默认关闭

warn_badges = []
if orphan_objects:
    warn_badges.append(f'<div class="stat warn"><span class="num">{len(orphan_objects)}</span><span class="label">孤立对象</span></div>')
stats_warn_badges = '\n'.join(warn_badges)

top_logic_items = []
for lname in sorted(top_logic):
    desc = LOGIC_IDX[lname]['description']
    top_logic_items.append(f'<li><strong>{lname}</strong> — {desc}</li>')
top_logic_list = '\n'.join(top_logic_items)

scale_parts = []
if N_OBJECTS:
    scale_parts.append(f'{N_OBJECTS} 个业务对象')
if N_LOGICS:
    scale_parts.append(f'{N_LOGICS} 个业务逻辑')
if N_SCRIPTS:
    scale_parts.append(f'{N_SCRIPTS} 个生成脚本')
scale_summary = '、'.join(scale_parts) + f'，覆盖{TOPIC}核心业务全流程。'

matrix_rows = []
for lname in sorted(LOGIC_IDX.keys()):
    ldata = LOGIC_IDX[lname]
    inputs_str = ', '.join(ldata['inputs']) or '—'
    outputs_str = ', '.join(ldata['outputs']) or '—'
    supports_str = ', '.join(ldata['supports']) or '—'
    matrix_rows.append(f'<tr><td><strong>{lname}</strong></td><td>{inputs_str}</td><td>{outputs_str}</td><td>{supports_str}</td></tr>')
matrix_rows_str = '\n'.join(matrix_rows)

hotspot_rows = []
for oname, cnt in sorted_objects:
    used_by_str = ', '.join(OBJECTS_IDX[oname]['used_by']) if OBJECTS_IDX[oname]['used_by'] else '（无）'
    bar_width = int(100 * cnt / max(max_use, 1))
    hotspot_rows.append(f'<tr><td>{oname}</td><td><span class="bar" style="width:{bar_width}px;display:inline-block"></span>{cnt}</td><td>{used_by_str}</td></tr>')
hotspot_rows_str = '\n'.join(hotspot_rows)

if script_files:
    script_rows = []
    for sf in script_files:
        name = sf.stem
        desc = re.search(r'"""(.*?)"""', sf.read_text(encoding='utf-8'), re.DOTALL)
        desc = desc.group(1).strip().split('\n')[0] if desc else ''
        script_rows.append(f'<tr><td><code>{name}</code></td><td>{desc}</td></tr>')
    scripts_panel = f'<table><thead><tr><th>脚本</th><th>描述</th></tr></thead><tbody>{"".join(script_rows)}</tbody></table>'
else:
    scripts_panel = '<p style="color:var(--text-muted)">未发现脚本文件</p>'

problems_parts = []
if missing_refs:
    prob_items = '\n'.join([f'<li><code>{src}</code> → <code>{tgt}</code> (缺失 {kind})</li>' for src, tgt, kind in missing_refs[:50]])
    problems_parts.append(f'<div class="problem-section danger"><h3>缺失引用 <span class="badge danger">{len(missing_refs)}</span></h3><ul>{prob_items}</ul></div>')
if orphan_objects:
    prob_items = '\n'.join([f'<li>{o}</li>' for o in orphan_objects])
    problems_parts.append(f'<div class="problem-section"><h3>孤立对象 <span class="badge warn">{len(orphan_objects)}</span></h3><ul>{prob_items}</ul></div>')
problems_panel = '\n'.join(problems_parts) if problems_parts else '<div class="card"><p style="color:var(--success)">✓ 未发现问题</p></div>'

n_problems = len(missing_refs)
n_problems_badge = f'<span class="badge danger">{n_problems}</span>' if n_problems > 0 else ''

# ─── Load templates ────────────────────────────────────────────────────────────
base_html = (TEMPLATES_DIR / 'html-report-base.html').read_text(encoding='utf-8')
body_html = (TEMPLATES_DIR / 'overview-body.html').read_text(encoding='utf-8')

# ─── Assemble ─────────────────────────────────────────────────────────────────
output_html = base_html
output_html = output_html.replace('{{TITLE}}', f'{TOPIC} Ontology 全景视图')
output_html = output_html.replace('{{TIMESTAMP}}', DISPLAY_STAMP)
output_html = output_html.replace('{{SCOPE_HINT}}', '')
output_html = output_html.replace('{{HEAD_EXTRA}}', '')
output_html = output_html.replace('{{FOOTER_NOTE}}', '生成于 ' + DISPLAY_STAMP)

output_html = output_html.replace(
    '{{HEADER_EXTRA}}',
    f'<div class="stats-row">'
    f'<div class="stat"><span class="num">{N_OBJECTS}</span><span class="label">对象</span></div>'
    f'<div class="stat"><span class="num">{N_LOGICS}</span><span class="label">逻辑</span></div>'
    f'<div class="stat"><span class="num">{N_ACTIONS}</span><span class="label">动作</span></div>'
    f'<div class="stat"><span class="num">{N_SCRIPTS}</span><span class="label">脚本</span></div>'
    f'{stats_warn_badges}'
    f'</div>'
)

output_html = output_html.replace('{{BODY_CONTENT}}', body_html)
output_html = output_html.replace('{{N_ACTIONS}}', str(N_ACTIONS))
output_html = output_html.replace('{{N_PROBLEMS_BADGE}}', n_problems_badge)
output_html = output_html.replace('{{TOP_LOGIC_LIST}}', top_logic_list)
output_html = output_html.replace('{{SCALE_SUMMARY}}', scale_summary)
output_html = output_html.replace('{{MATRIX_ROWS}}', matrix_rows_str)
output_html = output_html.replace('{{HOTSPOT_ROWS}}', hotspot_rows_str)
output_html = output_html.replace('{{SCRIPTS_PANEL_CONTENT}}', scripts_panel)
output_html = output_html.replace('{{PROBLEMS_PANEL_CONTENT}}', problems_panel)

output_html = output_html.replace('{{BODY_SCRIPTS}}', f'''
<script src="https://unpkg.com/vis-network@9/standalone/umd/vis-network.min.js"></script>
<script>
  const objectNodes = {json.dumps(object_nodes, ensure_ascii=False)};
  const objectEdges = {json.dumps(object_edges, ensure_ascii=False)};
  const objectMeta = {json.dumps(object_meta, ensure_ascii=False)};
  const logicNodes = {json.dumps(logic_nodes, ensure_ascii=False)};
  const logicEdges = {json.dumps(logic_edges, ensure_ascii=False)};
  const logicMeta = {json.dumps(logic_meta, ensure_ascii=False)};

  function escapeHtml(s) {{
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
  }}

  function renderObjectDetail(node) {{
    if (!node) return '<div class="placeholder">无详情数据</div>';
    const aliasHtml = (node.aliases || []).map(a => `<span class="badge">{{escapeHtml(a)}}</span>`).join('');
    const usedBy = (node.used_by || []).map(l => `<li>{{escapeHtml(l)}}</li>`).join('') || '<li>（无）</li>';
    const related = (node.related || []).map(r => `<li>{{escapeHtml(r)}}</li>`).join('') || '<li>（无）</li>';
    return `
      <h3 class="detail-name">{{escapeHtml(node.name)}}</h3>
      ${{aliasHtml ? `<div>${{aliasHtml}}</div>` : ''}}
      <div class="detail-section"><h4>业务描述</h4><div>{{escapeHtml(node.description) || '（无）'}}</div></div>
      <div class="detail-section"><h4>被引用的逻辑（${{(node.used_by || []).length}}）</h4><ul>${{usedBy}}</ul></div>
      <div class="detail-section"><h4>关联对象</h4><ul>${{related}}</ul></div>
    `;
  }}

  function renderLogicDetail(node) {{
    if (!node) return '<div class="placeholder">无详情数据</div>';
    const tag = node.is_top ? '<span class="badge accent">顶层流程</span>' : '';
    const inputs = (node.inputs || []).map(o => `<li>{{escapeHtml(o)}}</li>`).join('') || '<li>（无）</li>';
    const outputs = (node.outputs || []).map(o => `<li>{{escapeHtml(o)}}</li>`).join('') || '<li>（无）</li>';
    const calls = (node.calls || []).map(o => `<li>{{escapeHtml(o)}}</li>`).join('') || '<li>（无）</li>';
    const calledBy = (node.called_by || []).map(o => `<li>{{escapeHtml(o)}}</li>`).join('') || '<li>（无）</li>';
    return `
      <h3 class="detail-name">{{escapeHtml(node.name)}}</h3>
      ${{tag}}
      <div class="detail-section"><h4>业务描述</h4><div>{{escapeHtml(node.description) || '（无）'}}</div></div>
      <div class="detail-section"><h4>输入对象</h4><ul>${{inputs}}</ul></div>
      <div class="detail-section"><h4>输出对象</h4><ul>${{outputs}}</ul></div>
      <div class="detail-section"><h4>调用的子流程</h4><ul>${{calls}}</ul></div>
      <div class="detail-section"><h4>被哪些逻辑使用</h4><ul>${{calledBy}}</ul></div>
    `;
  }}

  const objectGraph = new vis.Network(
    document.getElementById('object-graph-canvas'),
    {{ nodes: new vis.DataSet(objectNodes), edges: new vis.DataSet(objectEdges) }},
    {{
      nodes: {{ shape: 'dot', scaling: {{ min: 12, max: 40, label: {{ enabled: true, min: 12, max: 18 }} }}, font: {{ face: 'inherit', size: 14 }} }},
      edges: {{ arrows: 'to', smooth: {{ type: 'continuous' }}, font: {{ face: 'inherit', size: 11, color: '#656d76', strokeWidth: 0 }}, color: {{ color: '#a0a8b0', highlight: '#0969da' }} }},
      physics: {{ stabilization: {{ iterations: 200 }}, barnesHut: {{ gravitationalConstant: -3000, springLength: 140 }} }},
      interaction: {{ hover: true, tooltipDelay: 200 }}
    }}
  );
  objectGraph.on('click', params => {{
    const sidebar = document.getElementById('object-graph-detail');
    sidebar.innerHTML = params.nodes.length ? renderObjectDetail(objectMeta[params.nodes[0]]) : '<div class="placeholder">点击节点查看详情</div>';
  }});

  const logicGraph = new vis.Network(
    document.getElementById('logic-graph-canvas'),
    {{ nodes: new vis.DataSet(logicNodes), edges: new vis.DataSet(logicEdges) }},
    {{
      layout: {{ hierarchical: {{ enabled: true, direction: 'UD', sortMethod: 'directed', levelSeparation: 110, nodeSpacing: 150 }} }},
      physics: false,
      nodes: {{ shape: 'box', font: {{ face: 'inherit', size: 14 }}, margin: 10, widthConstraint: {{ maximum: 180 }} }},
      edges: {{ arrows: 'to', smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 }}, color: {{ color: '#a0a8b0', highlight: '#0969da' }} }},
      interaction: {{ hover: true, tooltipDelay: 200 }}
    }}
  );
  logicGraph.on('click', params => {{
    const sidebar = document.getElementById('logic-graph-detail');
    sidebar.innerHTML = params.nodes.length ? renderLogicDetail(logicMeta[params.nodes[0]]) : '<div class="placeholder">点击节点查看详情</div>';
  }});

  function setupSearch(inputId, network) {{
    const ds = network.body.data.nodes;
    document.getElementById(inputId).addEventListener('input', e => {{
      const q = e.target.value.trim().toLowerCase();
      const updates = ds.get().map(n => {{
        const match = !q || (n.label && String(n.label).toLowerCase().includes(q));
        return {{ id: n.id, opacity: match ? 1 : 0.18, borderWidth: match && q ? 3 : 1 }};
      }});
      ds.update(updates);
    }});
  }}
  setupSearch('object-search', objectGraph);
  setupSearch('logic-search', logicGraph);
</script>
''')

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.write_text(output_html, encoding='utf-8')
print(f"Report written to: {OUTPUT_PATH}")
print(f"\n=== STATS ===")
print(f"Objects: {N_OBJECTS} | Logics: {N_LOGICS} | Scripts: {N_SCRIPTS}")
print(f"\n=== ORPHAN OBJECTS ({len(orphan_objects)}) ===")
for o in sorted(orphan_objects):
    print(f"  - {o}")
print(f"\n=== TOP LOGICS ({len(top_logic)}) ===")
for l in sorted(top_logic):
    print(f"  - {l}")
print(f"\n=== OBJECT USAGE TOP 10 ===")
for oname, cnt in sorted_objects[:10]:
    print(f"  {oname}: {cnt}")
print(f"\n=== MISSING REFERENCES ({len(missing_refs)}) ===")
for src, tgt, kind in missing_refs[:20]:
    print(f"  [{src}] -> {tgt} ({kind})")
