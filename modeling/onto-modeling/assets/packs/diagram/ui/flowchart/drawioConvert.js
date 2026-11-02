/* FlowchartModel ↔ drawio XML。
 *
 * 节点样式沿用提示词资产的 flowchart 约定：
 *   开始 / 结束 shape=mxgraph.flowchart.terminator | 业务步骤 process（默认）| 判断 rhombus | 数据对象 shape=card
 * drawio 高级编辑打开标准存储的图时，用 modelToDrawio 把当前（含未保存修改的）模型交给编辑器；
 * 保存时把 XML 交回服务端，由图示包解析成模型。drawioToModel 只在浏览器里用（mock 与降级预览）。
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

export function escapeXml(text) {
  return String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

export function modelToDrawio(model, name = '') {
  const cells = [];
  (model.nodes || []).forEach((n) => {
    const { w, h } = nodeSize(n);
    const style = TYPE_STYLE[n.type] || TYPE_STYLE.step;
    cells.push(
      `        <mxCell id="${escapeXml(n.id)}" value="${escapeXml(n.label)}" style="${escapeXml(n.pending ? `${style}dashed=1;` : style)}" vertex="1" parent="1">\n`
      + `          <mxGeometry x="${Math.round(n.x - w / 2)}" y="${Math.round(n.y - h / 2)}" width="${w}" height="${h}" as="geometry" />\n`
      + '        </mxCell>',
    );
  });
  (model.edges || []).forEach((e) => {
    cells.push(
      `        <mxCell id="${escapeXml(e.id)}" value="${escapeXml(e.label)}" style="${escapeXml(EDGE_STYLE[e.type] || EDGE_STYLE.seq)}" edge="1" parent="1" source="${escapeXml(e.from)}" target="${escapeXml(e.to)}">\n`
      + '          <mxGeometry relative="1" as="geometry" />\n'
      + '        </mxCell>',
    );
  });
  const title = name || model.process?.name || '流程图';
  return `<mxfile host="onto-modeling" agent="onto-modeling-flowchart">\n  <diagram name="${escapeXml(title)}">\n`
    + '    <mxGraphModel grid="1" gridSize="10" guides="1" connect="1" arrows="1" page="0">\n      <root>\n'
    + '        <mxCell id="0" />\n        <mxCell id="1" parent="0" />\n'
    + `${cells.join('\n')}${cells.length ? '\n' : ''}      </root>\n    </mxGraphModel>\n  </diagram>\n</mxfile>\n`;
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
  const cells = [...graph.querySelectorAll('mxCell')];
  const textOf = (v) => {
    const el = document.createElement('div');
    el.innerHTML = v || '';
    return (el.textContent || '').trim();
  };
  const inDeg = new Map();
  cells.filter((c) => c.getAttribute('edge') === '1').forEach((c) => {
    const t = c.getAttribute('target');
    if (t) inDeg.set(t, (inDeg.get(t) || 0) + 1);
  });
  const old = new Map((base?.nodes || []).map((n) => [n.id, n]));
  const oldEdges = new Map((base?.edges || []).map((e) => [e.id, e]));
  const nodes = [];
  const edges = [];
  cells.forEach((c) => {
    const id = c.getAttribute('id');
    const value = textOf(c.getAttribute('value'));
    if (c.getAttribute('vertex') === '1') {
      const g = c.querySelector('mxGeometry');
      const w = Number(g?.getAttribute('width')) || 120;
      const h = Number(g?.getAttribute('height')) || 40;
      const prev = old.get(id);
      nodes.push({
        id,
        type: typeOf(c.getAttribute('style'), value, inDeg.get(id) || 0),
        label: value,
        description: prev?.description || '',
        x: Math.round((Number(g?.getAttribute('x')) || 0) + w / 2),
        y: Math.round((Number(g?.getAttribute('y')) || 0) + h / 2),
        refs: prev?.refs || [],
        pending: /dashed=1/.test(c.getAttribute('style') || '') && !!prev?.pending,
        pending_note: prev?.pending_note || null,
      });
    } else if (c.getAttribute('edge') === '1' && c.getAttribute('source') && c.getAttribute('target')) {
      const prev = oldEdges.get(id);
      edges.push({
        id,
        type: edgeTypeOf(c.getAttribute('style')),
        from: c.getAttribute('source'),
        to: c.getAttribute('target'),
        label: value,
        condition: prev?.condition || '',
      });
    }
  });
  return { ...(base || {}), storage: 'standard', nodes, edges, drawio_xml: null };
}
