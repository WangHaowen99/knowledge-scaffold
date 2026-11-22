/* FlowchartModel ↔ drawio XML。
 *
 * 节点样式沿用提示词资产的 flowchart 约定：
 *   开始 / 结束 shape=mxgraph.flowchart.terminator | 业务步骤 process（默认）| 判断 rhombus | 数据对象 shape=card
 * drawio 高级编辑打开标准存储的图时，XML 由服务端给（AR-10）：文件原文，有未保存修改时是合进了模型的原文。
 * modelToDrawio 只在取不到时兜底（以及 mock）。保存时把 XML 交回服务端，由内核解析成模型。
 * drawioToModel 只在浏览器里用（mock 与降级预览）。
 * 分支连线旁的独立文字格子是文字标注（同 develop !607）：drawioToModel 把它归到连线上当连线文字，另带上 annotations
 * 与各连线的 geometry（原 mxGeometry，含折点），modelToDrawio 据此照原样写回。这两项不在契约 7.11 里，
 * normalizeModel 不保留，只在 drawioToModel → modelToDrawio 直接相连时起作用。
 */
import { nodeSize } from './flowchartModel.js';

export const TYPE_STYLE = {
  start: 'rounded=1;whiteSpace=wrap;html=1;shape=mxgraph.flowchart.terminator;fillColor=#EFF5FF;strokeColor=#2563EB;',
  end: 'rounded=1;whiteSpace=wrap;html=1;shape=mxgraph.flowchart.terminator;fillColor=#F1F0FF;strokeColor=#4F46E5;',
  step: 'rounded=1;whiteSpace=wrap;html=1;arcSize=20;strokeColor=#9BBEFB;',
  decision: 'rhombus;whiteSpace=wrap;html=1;fillColor=#FFF8EB;strokeColor=#F59E0B;',
  data: 'shape=card;whiteSpace=wrap;html=1;flipH=1;size=12;fillColor=#ECFAF7;strokeColor=#0D9488;',
};
const EDGE_STYLE = {
  seq: 'endArrow=block;html=1;strokeColor=#7C8CA6;',
  cond: 'endArrow=block;html=1;strokeColor=#F59E0B;',
  exc: 'endArrow=block;html=1;strokeColor=#DC2626;dashed=1;dashPattern=6 4;',
  data: 'endArrow=block;html=1;strokeColor=#0D9488;dashed=1;dashPattern=2 4;',
};

// 属性值里的换行、制表符也要转义：XML 解析时属性值里的空白会被规整成空格，多行的描述、条件就并成一行了
export function escapeXml(text) {
  return String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll('\r', '&#13;')
    .replaceAll('\n', '&#10;')
    .replaceAll('\t', '&#9;');
}

const PROCESS_LISTS = ['roles', 'inputs', 'exceptions', 'handoffs'];
const ANNOTATION_EDGE_ATTR = 'data-flowchart-annotation-edge';
const EDGE_GEOMETRY = '<mxGeometry relative="1" as="geometry" />';
// 样式里带 html=1，单元格文字按 HTML 显示：先转成 HTML 文本，和内核写出的一样
export const labelHtml = (label) => String(label ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('\n', '<br>');
const attrsXml = (attrs) => Object.entries(attrs).map(([k, v]) => ` ${k}="${escapeXml(v)}"`).join('');

// 只用 childNodes、getElementsByTagName 这类 xmldom 也有的接口：node 单测用 xmldom 的 DOMParser
const elementsOf = (el) => Array.from(el?.childNodes || []).filter((c) => c.nodeType === 1);

// XML 元素 ↔ 能随模型走的树（结构化克隆、JSON 都能过），连线与标注的原 mxGeometry 靠它原样写回
function readXml(el) {
  if (!el) return null;
  return {
    tag: el.tagName,
    attrs: Object.fromEntries(Array.from(el.attributes).map((a) => [a.name, a.value])),
    children: elementsOf(el).map(readXml),
  };
}

function writeXml(node) {
  const inner = (node.children || []).map(writeXml).join('');
  return inner ? `<${node.tag}${attrsXml(node.attrs || {})}>${inner}</${node.tag}>` : `<${node.tag}${attrsXml(node.attrs || {})} />`;
}

// 内核元数据按内核的写法挂成用户对象属性（后端 kernel/artifacts/flowchart.py 文件头）：高级编辑保存时，
// 标准编辑器里还没保存的描述、引用、待定标记、条件、流程元数据随 XML 一起交回，不会退回上一版
function rootXml(model) {
  const p = model.process || {};
  const attrs = { id: '0', label: '', process_name: p.name || '', process_description: p.description || '' };
  PROCESS_LISTS.forEach((k) => { attrs[`process_${k}`] = JSON.stringify(p[k] || []); });
  attrs.operation = model.operation ? JSON.stringify(model.operation) : '';
  return `        <object${attrsXml(attrs)}>\n          <mxCell />\n        </object>`;
}

export function modelToDrawio(model, name = '') {
  const cells = [rootXml(model), '        <mxCell id="1" parent="0" />'];
  (model.nodes || []).forEach((n) => {
    const { w, h } = nodeSize(n);
    const style = TYPE_STYLE[n.type] || TYPE_STYLE.step;
    const attrs = {
      id: n.id, label: labelHtml(n.label), onto_type: n.type, description: n.description || '',
      refs: JSON.stringify(n.refs || []), pending: n.pending ? '1' : '0',
      ...(n.pending_note ? { pending_note: n.pending_note } : {}),
    };
    cells.push(
      `        <object${attrsXml(attrs)}>\n`
      + `          <mxCell style="${escapeXml(n.pending ? `${style}dashed=1;` : style)}" vertex="1" parent="1">\n`
      + `            <mxGeometry x="${Math.round(n.x - w / 2)}" y="${Math.round(n.y - h / 2)}" width="${w}" height="${h}" as="geometry" />\n`
      + '          </mxCell>\n        </object>',
    );
  });
  const annotations = model.annotations || [];
  const owner = (a) => (a.edge ? (model.edges || []).find((e) => e.id === a.edge && e.annotation === a.id) : null);
  (model.edges || []).forEach((e) => {
    // 归属了标注的连线：文字写在标注格子上，连线本身不写，免得画两遍
    const separate = annotations.some((a) => a.id === e.annotation && a.edge === e.id);
    const attrs = { id: e.id, label: separate ? '' : labelHtml(e.label), onto_type: e.type, condition: e.condition || '' };
    cells.push(
      `        <object${attrsXml(attrs)}>\n`
      + `          <mxCell style="${escapeXml(EDGE_STYLE[e.type] || EDGE_STYLE.seq)}" edge="1" parent="1" source="${escapeXml(e.from)}" target="${escapeXml(e.to)}">\n`
      + `            ${e.geometry ? writeXml(e.geometry) : EDGE_GEOMETRY}\n`
      + '          </mxCell>\n        </object>',
    );
  });
  annotations.forEach((a) => {
    const e = owner(a);
    // 所属连线删了或文字清空了，标注跟着去掉；没归属的原样写回
    if (a.edge && !e?.label) return;
    const attrs = { id: a.id, label: e ? labelHtml(e.label) : a.label || '', ...(e ? { [ANNOTATION_EDGE_ATTR]: e.id } : {}) };
    cells.push(
      `        <object${attrsXml(attrs)}>\n`
      + `          <mxCell style="${escapeXml(a.style)}" vertex="1" parent="${escapeXml(a.parent || '1')}">\n`
      + `            ${a.geometry ? writeXml(a.geometry) : '<mxGeometry as="geometry" />'}\n`
      + '          </mxCell>\n        </object>',
    );
  });
  const title = name || model.process?.name || '流程图';
  return `<mxfile host="onto-modeling" agent="onto-modeling-flowchart">\n  <diagram name="${escapeXml(title)}">\n`
    + '    <mxGraphModel grid="1" gridSize="10" guides="1" connect="1" arrows="1" page="0">\n      <root>\n'
    + `${cells.join('\n')}\n      </root>\n    </mxGraphModel>\n  </diagram>\n</mxfile>\n`;
}

// 压缩存储：<diagram> 里不是明文 mxGraphModel
export function isCompressedDrawio(xml) {
  const m = String(xml || '').match(/<diagram\b[^>]*>([\s\S]*?)<\/diagram>/);
  return !!m && !/<mxGraphModel\b/.test(m[1]) && m[1].trim().length > 0;
}

export function parseDrawio(xml) {
  const dom = new DOMParser().parseFromString(xml, 'text/xml');
  if (dom.getElementsByTagName('parsererror').length) throw new Error('drawio XML 解析失败');
  return dom;
}

// 单元格可能包在用户对象（object / UserObject）里：id、文字和内核属性在外层，几何与样式在里面的 mxCell
function readCells(graph) {
  return elementsOf(graph.getElementsByTagName('root')[0]).flatMap((el) => {
    if (el.tagName === 'mxCell') return [{ el, cell: el, label: el.getAttribute('value'), attrs: {} }];
    if (el.tagName !== 'object' && el.tagName !== 'UserObject') return [];
    const attrs = Object.fromEntries(Array.from(el.attributes).map((a) => [a.name, a.value]));
    return [{ el, cell: el.getElementsByTagName('mxCell')[0] || el, label: attrs.label, attrs }];
  });
}

function typeOf(style, inDegree) {
  const s = style || '';
  // 开始 / 结束只按入度判，不按文字猜（同 develop !547）
  if (s.includes('mxgraph.flowchart.terminator')) return inDegree === 0 ? 'start' : 'end';
  if (s.includes('rhombus')) return 'decision';
  if (s.includes('shape=card')) return 'data';
  return 'step';
}

export function nodeTypeOf(attrs, style, inDegree) {
  const shaped = typeOf(style, inDegree);
  return TYPE_STYLE[attrs.onto_type] && !['decision', 'data'].includes(shaped) ? attrs.onto_type : shaped;
}

// 单元格文字转纯文本，同内核 _plain：HTML 文字里换行类标签当空格、去掉其余标签、解开实体，空白压成一个。
// 不借 document 转：node 单测里没有
const HTML_HINT = /<\s*\/|<\s*(br|div|p|span|b|i|u|font)\b|&(nbsp|lt|gt|amp|quot|#\d+);/i;
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
function textOf(value, style) {
  let text = String(value || '');
  if (/html=1/.test(style || '') || HTML_HINT.test(text)) {
    text = text.replace(/<\s*(br|\/?(div|p|li|tr|h[1-6]))\b[^>]*>/gi, ' ')
      .replace(/<[^>]*>/g, '')
      .replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
        if (e[0] !== '#') return ENTITIES[e] ?? m;
        const code = /^#x/i.test(e) ? parseInt(e.slice(2), 16) : Number(e.slice(1));
        return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : m;
      });
  }
  return text.split(/\s+/).filter(Boolean).join(' ');
}

const TEXT_STYLE = /(?:^|;)\s*text\s*(?:;|$)/;

function segmentDistance(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = dx * dx + dy * dy;
  const t = len ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len)) : 0;
  return Math.hypot(p.x - a.x - t * dx, p.y - a.y - t * dy);
}

// 点到连线折线（起点中心 → 折点 → 终点中心）的距离
function edgeDistance(edge, point, nodeById) {
  const a = nodeById.get(edge.from);
  const b = nodeById.get(edge.to);
  if (!a || !b) return Infinity;
  const bends = (edge.geometry?.children || [])
    .filter((c) => c.tag === 'Array' && c.attrs.as === 'points')
    .flatMap((c) => c.children.filter((pt) => pt.tag === 'mxPoint'))
    .map((pt) => ({ x: Number(pt.attrs.x) || 0, y: Number(pt.attrs.y) || 0 }));
  const pts = [a, ...bends, b];
  return Math.min(...pts.slice(1).map((pt, i) => segmentDistance(point, pts[i], pt)));
}

/**
 * 文字标注归到连线（同 develop !607）：带 data-flowchart-annotation-edge 的直接归过去；其余在起点出度大于 1、
 * 还没有文字和条件的分支连线里找离标注中心最近的，最近不超过 60 且比第二近的至少近 20 才归，归过去的记为条件分支。
 * 归不了的当普通批注，modelToDrawio 原样写回（同内核 flowchart.py，不像 develop 那样退回 drawio 编辑器）。
 */
function attachAnnotations(annotations, nodes, edges) {
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const outDeg = new Map();
  edges.forEach((e) => outDeg.set(e.from, (outDeg.get(e.from) || 0) + 1));
  annotations.forEach((a) => {
    const text = textOf(a.label, a.style);
    const linked = a.edge ? edges.find((e) => e.id === a.edge && !e.annotation) : null;
    if (linked) {
      if (!linked.label) linked.label = text;
      linked.annotation = a.id;
      return;
    }
    const g = a.geometry?.attrs || {};
    const center = { x: (Number(g.x) || 0) + (Number(g.width) || 0) / 2, y: (Number(g.y) || 0) + (Number(g.height) || 0) / 2 };
    const near = edges
      .filter((e) => !e.label && !e.condition && (outDeg.get(e.from) || 0) > 1)
      .map((e) => ({ e, d: edgeDistance(e, center, nodeById) }))
      .sort((p, q) => p.d - q.d);
    if (near[0]?.d <= 60 && (near.length === 1 || near[1].d - near[0].d >= 20)) {
      Object.assign(near[0].e, { label: text, type: 'cond', annotation: a.id });
      a.edge = near[0].e.id;
    } else {
      a.edge = '';
    }
  });
}

export function edgeTypeOf(attrs, style) {
  if (EDGE_STYLE[attrs.onto_type]) return attrs.onto_type;
  const s = style || '';
  if (s.includes('#DC2626')) return 'exc';
  if (s.includes('#0D9488')) return 'data';
  if (s.includes('#F59E0B')) return 'cond';
  return 'seq';
}

// 样式里的名字：不带 = 的段，加上 shape= 的值
const styleNames = (style) => style.split(';').map((s) => s.trim())
  .flatMap((s) => (!s.includes('=') ? [s] : s.startsWith('shape=') ? [s.slice(6)] : [])).filter(Boolean);

// 单元格的身份，同内核 flowchart.py 的 _build / _vertex_kind：连线上的文字、文字标注、泳道分组和批注都不是流程节点
function cellKind({ cell, attrs, style, text }, edgeIds) {
  if (cell.getAttribute('edge') === '1') return 'edge';
  if (cell.getAttribute('vertex') !== '1') return 'other';
  if (edgeIds.has(cell.getAttribute('parent') || '')) return 'label';
  const names = styleNames(style);
  if (names.includes('text') && !('onto_type' in attrs) && text) return 'annotation';
  if (attrs.onto_type === 'note' || style.includes('swimlane')) return 'shape';
  return names.some((n) => ['group', 'text', 'edgeLabel'].includes(n)) ? 'shape' : 'node';
}

/**
 * 第一页的单元格（内核只认第一页），按文档顺序。attached(id)：连线的端点连在节点、泳道、分组或批注上。
 * 没有明文 mxGraphModel 时抛 Error('compressed')。
 */
export function drawioPage(dom) {
  const graph = dom.getElementsByTagName('mxGraphModel')[0];
  if (!graph) throw new Error('compressed');
  const raw = readCells(graph).map(({ el, cell, label, attrs }, index) => {
    const style = cell.getAttribute('style') || '';
    return {
      index, el, cell, attrs, style,
      id: el.getAttribute('id') || '',
      text: textOf(label, style),
      parent: cell.getAttribute('parent') || '',
      source: cell.getAttribute('source') || '',
      target: cell.getAttribute('target') || '',
      annotationEdge: el.getAttribute(ANNOTATION_EDGE_ATTR) || cell.getAttribute(ANNOTATION_EDGE_ATTR) || '',
    };
  });
  const edgeIds = new Set(raw.filter((c) => c.cell.getAttribute('edge') === '1' && c.id).map((c) => c.id));
  const cells = raw.map((c) => ({ ...c, kind: cellKind(c, edgeIds) }));
  const ids = new Set(cells.filter((c) => c.id && (c.kind === 'node' || c.kind === 'shape')).map((c) => c.id));
  return {
    root: graph.getElementsByTagName('root')[0] || null,
    cells,
    nodes: cells.filter((c) => c.kind === 'node'),
    attached: (id) => ids.has(id),
  };
}

/**
 * 标准编辑器打不开的结构问题（develop !613），没有时返回空串。内核解析时同号的单元格只认第一个、指不到节点的连线直接略过，
 * 照常打开的话这些内容在画布上看不见，所以先拦下来。
 */
export function drawioStructureError(xml) {
  const { cells, nodes, attached } = drawioPage(parseDrawio(xml));
  const seen = new Set();
  for (const c of nodes) {
    if (!c.id) return '存在缺少 ID 的节点';
    if (seen.has(c.id)) return `节点 ID 重复：${c.id}`;
    seen.add(c.id);
  }
  for (const c of cells.filter((item) => item.kind === 'edge')) {
    if (c.id && seen.has(c.id)) return `单元格 ID 重复：${c.id}`;
    if (c.id) seen.add(c.id);
    if (!attached(c.source) || !attached(c.target)) return `连线 ${c.id || '（未编号）'} 指向不存在的节点`;
  }
  for (const c of cells.filter((item) => item.kind === 'annotation')) {
    if (!c.id || seen.has(c.id)) return '文字标注 ID 缺失或重复';
    seen.add(c.id);
  }
  for (const c of cells.filter((item) => item.id && !['node', 'edge', 'annotation'].includes(item.kind))) {
    if (seen.has(c.id)) return `单元格 ID 重复：${c.id}`;
    seen.add(c.id);
  }
  return '';
}

/**
 * 浏览器里把明文 drawio XML 解析成模型；base 里同 id 的节点保留业务说明、关联条目和待定标记。
 * 压缩存储抛 Error('compressed')。
 */
export function drawioToModel(xml, base = null) {
  const graph = parseDrawio(xml).getElementsByTagName('mxGraphModel')[0];
  if (!graph) throw new Error('compressed');
  const cells = readCells(graph);
  const json = (v, fallback) => {
    try { return JSON.parse(v); } catch { return fallback; }
  };
  const inDeg = new Map();
  cells.filter((c) => c.cell.getAttribute('edge') === '1').forEach((c) => {
    const t = c.cell.getAttribute('target');
    if (t) inDeg.set(t, (inDeg.get(t) || 0) + 1);
  });
  const old = new Map((base?.nodes || []).map((n) => [n.id, n]));
  const oldEdges = new Map((base?.edges || []).map((e) => [e.id, e]));
  const nodes = [];
  const edges = [];
  const annotations = [];
  let meta = {};
  cells.forEach(({ el, cell: c, label, attrs }) => {
    const id = el.getAttribute('id');
    const style = c.getAttribute('style') || '';
    const value = textOf(label, style);
    const has = (k) => Object.prototype.hasOwnProperty.call(attrs, k);
    if (id === '0') meta = attrs;
    if (c.getAttribute('vertex') === '1') {
      const g = c.getElementsByTagName('mxGeometry')[0];
      // 没有节点类型的独立文字格子是文字标注，不是流程节点；归属属性在 object 上（本分支写法）或裸 mxCell 上（develop 写法）
      if (TEXT_STYLE.test(style) && !has('onto_type')) {
        annotations.push({
          id, edge: el.getAttribute(ANNOTATION_EDGE_ATTR) || '', label: label || '', style,
          parent: c.getAttribute('parent') || '1', geometry: readXml(g),
        });
        return;
      }
      const w = Number(g?.getAttribute('width')) || 120;
      const h = Number(g?.getAttribute('height')) || 40;
      const prev = old.get(id);
      nodes.push({
        id,
        type: nodeTypeOf(attrs, style, inDeg.get(id) || 0),
        label: value,
        description: has('description') ? attrs.description : prev?.description || '',
        x: Math.round((Number(g?.getAttribute('x')) || 0) + w / 2),
        y: Math.round((Number(g?.getAttribute('y')) || 0) + h / 2),
        refs: has('refs') ? json(attrs.refs, []) : prev?.refs || [],
        pending: has('pending') ? attrs.pending === '1' : /dashed=1/.test(style) && !!prev?.pending,
        pending_note: has('pending_note') ? attrs.pending_note || null : prev?.pending_note || null,
      });
    } else if (c.getAttribute('edge') === '1' && c.getAttribute('source') && c.getAttribute('target')) {
      const prev = oldEdges.get(id);
      edges.push({
        id,
        type: edgeTypeOf(attrs, style),
        from: c.getAttribute('source'),
        to: c.getAttribute('target'),
        label: value,
        condition: has('condition') ? attrs.condition : prev?.condition || '',
        geometry: readXml(c.getElementsByTagName('mxGeometry')[0]),
      });
    }
  });
  attachAnnotations(annotations, nodes, edges);
  // 根单元格上的流程元数据与所描述的操作：XML 没带时沿用 base
  const process = { ...(base?.process || {}) };
  if (meta.process_name) process.name = meta.process_name;
  if ('process_description' in meta) process.description = meta.process_description;
  PROCESS_LISTS.forEach((k) => { if (`process_${k}` in meta) process[k] = json(meta[`process_${k}`], process[k] || []); });
  const operation = 'operation' in meta ? json(meta.operation, null) || null : base?.operation ?? null;
  return { ...(base || {}), process, operation, storage: 'standard', nodes, edges, annotations, drawio_xml: null };
}
