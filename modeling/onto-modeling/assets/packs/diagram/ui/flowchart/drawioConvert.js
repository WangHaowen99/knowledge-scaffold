/* FlowchartModel ↔ drawio XML。
 *
 * 节点样式沿用提示词资产的 flowchart 约定：
 *   开始 / 结束 shape=mxgraph.flowchart.terminator | 业务步骤 process（默认）| 判断 rhombus | 数据对象 shape=card
 * drawio 高级编辑打开标准存储的图时，没有未保存修改就打开文件原文；有未保存修改（或原文读不到）时用 modelToDrawio
 * 把当前模型交给编辑器。保存时把 XML 交回服务端，由内核解析成模型。drawioToModel 只在浏览器里用（mock 与降级预览）。
 */
import { nodeSize } from './flowchartModel.js';

const TYPE_STYLE = {
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
// 样式里带 html=1，单元格文字按 HTML 显示：先转成 HTML 文本，和内核写出的一样
const labelHtml = (label) => String(label ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('\n', '<br>');
const attrsXml = (attrs) => Object.entries(attrs).map(([k, v]) => ` ${k}="${escapeXml(v)}"`).join('');

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
  (model.edges || []).forEach((e) => {
    const attrs = { id: e.id, label: labelHtml(e.label), onto_type: e.type, condition: e.condition || '' };
    cells.push(
      `        <object${attrsXml(attrs)}>\n`
      + `          <mxCell style="${escapeXml(EDGE_STYLE[e.type] || EDGE_STYLE.seq)}" edge="1" parent="1" source="${escapeXml(e.from)}" target="${escapeXml(e.to)}">\n`
      + '            <mxGeometry relative="1" as="geometry" />\n'
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

function typeOf(style, value, inDegree) {
  const s = style || '';
  if (s.includes('mxgraph.flowchart.terminator')) return inDegree === 0 || /开始|start/i.test(value) ? 'start' : 'end';
  if (s.includes('rhombus')) return 'decision';
  if (s.includes('shape=card')) return 'data';
  return 'step';
}

function edgeTypeOf(style) {
  const s = style || '';
  if (s.includes('#DC2626')) return 'exc';
  if (s.includes('#0D9488')) return 'data';
  if (s.includes('#F59E0B')) return 'cond';
  return 'seq';
}

/**
 * 浏览器里把明文 drawio XML 解析成模型；base 里同 id 的节点保留业务说明、关联条目和待定标记。
 * 压缩存储抛 Error('compressed')。
 */
export function drawioToModel(xml, base = null) {
  const dom = new DOMParser().parseFromString(xml, 'text/xml');
  if (dom.querySelector('parsererror')) throw new Error('drawio XML 解析失败');
  const graph = dom.querySelector('mxGraphModel');
  if (!graph) throw new Error('compressed');
  // 单元格可能包在用户对象（object / UserObject）里：id、文字和内核属性在外层，几何与样式在里面的 mxCell
  const cells = [...(graph.querySelector('root')?.children || [])].flatMap((el) => {
    if (el.tagName === 'mxCell') return [{ el, cell: el, label: el.getAttribute('value'), attrs: {} }];
    if (el.tagName !== 'object' && el.tagName !== 'UserObject') return [];
    const attrs = Object.fromEntries([...el.attributes].map((a) => [a.name, a.value]));
    return [{ el, cell: el.querySelector('mxCell') || el, label: attrs.label, attrs }];
  });
  const textOf = (v) => {
    const el = document.createElement('div');
    el.innerHTML = v || '';
    return (el.textContent || '').trim();
  };
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
  let meta = {};
  cells.forEach(({ el, cell: c, label, attrs }) => {
    const id = el.getAttribute('id');
    const value = textOf(label);
    const has = (k) => Object.prototype.hasOwnProperty.call(attrs, k);
    if (id === '0') meta = attrs;
    if (c.getAttribute('vertex') === '1') {
      const g = c.querySelector('mxGeometry');
      const w = Number(g?.getAttribute('width')) || 120;
      const h = Number(g?.getAttribute('height')) || 40;
      const prev = old.get(id);
      const shaped = typeOf(c.getAttribute('style'), value, inDeg.get(id) || 0);
      nodes.push({
        id,
        type: TYPE_STYLE[attrs.onto_type] && !['decision', 'data'].includes(shaped) ? attrs.onto_type : shaped,
        label: value,
        description: has('description') ? attrs.description : prev?.description || '',
        x: Math.round((Number(g?.getAttribute('x')) || 0) + w / 2),
        y: Math.round((Number(g?.getAttribute('y')) || 0) + h / 2),
        refs: has('refs') ? json(attrs.refs, []) : prev?.refs || [],
        pending: has('pending') ? attrs.pending === '1' : /dashed=1/.test(c.getAttribute('style') || '') && !!prev?.pending,
        pending_note: has('pending_note') ? attrs.pending_note || null : prev?.pending_note || null,
      });
    } else if (c.getAttribute('edge') === '1' && c.getAttribute('source') && c.getAttribute('target')) {
      const prev = oldEdges.get(id);
      edges.push({
        id,
        type: EDGE_STYLE[attrs.onto_type] ? attrs.onto_type : edgeTypeOf(c.getAttribute('style')),
        from: c.getAttribute('source'),
        to: c.getAttribute('target'),
        label: value,
        condition: has('condition') ? attrs.condition : prev?.condition || '',
      });
    }
  });
  // 根单元格上的流程元数据与所描述的操作：XML 没带时沿用 base
  const process = { ...(base?.process || {}) };
  if (meta.process_name) process.name = meta.process_name;
  if ('process_description' in meta) process.description = meta.process_description;
  PROCESS_LISTS.forEach((k) => { if (`process_${k}` in meta) process[k] = json(meta[`process_${k}`], process[k] || []); });
  const operation = 'operation' in meta ? json(meta.operation, null) || null : base?.operation ?? null;
  return { ...(base || {}), process, operation, storage: 'standard', nodes, edges, drawio_xml: null };
}
