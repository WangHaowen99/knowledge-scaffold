/* drawio XML ↔ FlowchartEditor 文档格式（modeling 流程图编辑器契约）双向转换。
 *
 * 节点样式遵循提示词资产的 flowchart 约定：
 *   开始/结束 shape=mxgraph.flowchart.terminator | 业务活动 process（默认）
 *   判断 rhombus | 子流程 process;backgroundOutline=1 | 子流程判断 rhombus;double=1
 *   动作 shape=card | 递归 shape=hexagon
 * 未识别的样式保留在 node.meta.style，序列化时原样回写（不破坏 agent 产物）。
 * 压缩存储（<diagram> 内非明文 mxGraphModel）不支持，调用方应回退 drawio 编辑器。
 */

const KIND_STYLE = {
  start: 'rounded=1;whiteSpace=wrap;html=1;shape=mxgraph.flowchart.terminator;',
  end: 'rounded=1;whiteSpace=wrap;html=1;shape=mxgraph.flowchart.terminator;',
  action: 'rounded=0;whiteSpace=wrap;html=1;',
  decision: 'rhombus;whiteSpace=wrap;html=1;',
  data: 'shape=card;whiteSpace=wrap;html=1;',
  object: 'rounded=0;whiteSpace=wrap;html=1;backgroundOutline=1;',
};

const EDGE_STYLE = 'edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;';
const DESCRIPTION_ATTR = 'data-flowchart-description';
const PROCESS_META_ATTR = 'data-flowchart-process-meta';
const CONDITION_ATTR = 'data-flowchart-condition';
const LABEL_ATTR = 'data-flowchart-label';
const DETAIL_ATTR = 'data-flowchart-detail';
const NODE_KIND_ATTR = 'data-flowchart-node-kind';
const EDGE_KIND_ATTR = 'data-flowchart-edge-kind';

function styleToKind(style) {
  const s = style || '';
  if (s.includes('mxgraph.flowchart.terminator')) return 'terminator';
  if (s.includes('rhombus')) return 'decision';
  if (s.includes('shape=card')) return 'data';
  if (s.includes('backgroundOutline=1')) return 'object';
  return 'action';
}

export function parseDrawioToDocument(xml, fileName = '流程图') {
  const dom = new DOMParser().parseFromString(xml, 'text/xml');
  if (dom.querySelector('parsererror')) throw new Error('drawio XML 解析失败');
  const diagramNodes = [...dom.querySelectorAll('diagram')];
  if (!diagramNodes.length) return { diagrams: [] };
  const models = diagramNodes.length
    ? diagramNodes.map((d) => ({
        name: d.getAttribute('name') || '',
        description: d.getAttribute(DESCRIPTION_ATTR) || '',
        processMeta: d.getAttribute(PROCESS_META_ATTR) || '',
        model: d.querySelector('mxGraphModel'),
      }))
    : [];

  const diagrams = models.map(({ name, description, processMeta, model }, index) => {
    if (!model) throw new Error('compressed'); // 压缩存储：交给 drawio 编辑器
    const cells = [...model.querySelectorAll('mxCell')];
    const nodes = [];
    const edges = [];
    const positions = {};
    const inDegree = new Map();
    const outDegree = new Map();

    for (const cell of cells) {
      if (cell.getAttribute('edge') === '1') {
        const from = cell.getAttribute('source');
        const to = cell.getAttribute('target');
        if (!from || !to) continue;
        outDegree.set(from, (outDegree.get(from) || 0) + 1);
        inDegree.set(to, (inDegree.get(to) || 0) + 1);
      }
    }

    for (const cell of cells) {
      const id = cell.getAttribute('id');
      const value = cell.getAttribute('value') || '';
      if (cell.getAttribute('vertex') === '1') {
        const style = cell.getAttribute('style') || '';
        let kind = cell.getAttribute(NODE_KIND_ATTR) || styleToKind(style);
        if (kind === 'terminator') kind = (inDegree.get(id) || 0) === 0 ? 'start' : 'end';
        const geo = cell.querySelector('mxGeometry');
        if (geo) {
          positions[id] = {
            x: Number(geo.getAttribute('x')) || 0,
            y: Number(geo.getAttribute('y')) || 0,
          };
        }
        nodes.push({
          id, kind, label: value,
          meta: {
            style, kind,
            width: Number(geo.getAttribute('width')),
            height: Number(geo.getAttribute('height')),
          },
          ...(cell.hasAttribute(DETAIL_ATTR) ? { detail: cell.getAttribute(DETAIL_ATTR) } : {}),
        });
      } else if (cell.getAttribute('edge') === '1') {
        const from = cell.getAttribute('source');
        const to = cell.getAttribute('target');
        if (!from || !to) continue;
        edges.push({
          id: id || `edge-${edges.length + 1}`,
          from,
          to,
          kind: cell.getAttribute(EDGE_KIND_ATTR)
            || (/^(是|否|Y|N|yes|no)$/i.test(value) ? 'condition' : 'sequence'),
          label: cell.hasAttribute(LABEL_ATTR) ? cell.getAttribute(LABEL_ATTR) : value,
          conditionText: cell.getAttribute(CONDITION_ATTR) || '',
          meta: { style: cell.getAttribute('style') || EDGE_STYLE },
        });
      }
    }
    return {
      id: `diagram-${index + 1}`,
      title: name || fileName.replace(/\.(drawio|dio)$/i, ''),
      ...(description ? { description } : {}),
      ...(processMeta ? { processMeta: JSON.parse(processMeta) } : {}),
      nodes,
      edges,
      layout: { direction: 'TB', positions },
    };
  });
  return { diagrams };
}

function escapeXml(text) {
  return String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll('\r', '&#13;')
    .replaceAll('\n', '&#10;')
    .replaceAll('\t', '&#9;');
}

export function serializeDocumentToDrawio(doc) {
  const pages = (doc.diagrams || []).map((diagram) => {
    const positions = diagram.layout?.positions || {};
    const cells = [];
    for (const node of diagram.nodes) {
      const style = node.meta?.kind === node.kind && node.meta?.style
        ? node.meta.style : KIND_STYLE[node.kind] || KIND_STYLE.action;
      const pos = positions[node.id] || { x: 40, y: 40 };
      const width = node.meta?.width ?? 160;
      const height = node.meta?.height ?? 60;
      cells.push(
        `        <mxCell id="${escapeXml(node.id)}" value="${escapeXml(node.label)}" style="${escapeXml(style)}" vertex="1" parent="1" ${NODE_KIND_ATTR}="${escapeXml(node.kind)}"${node.detail ? ` ${DETAIL_ATTR}="${escapeXml(node.detail)}"` : ''}>\n` +
          `          <mxGeometry x="${Math.round(pos.x)}" y="${Math.round(pos.y)}" width="${escapeXml(width)}" height="${escapeXml(height)}" as="geometry" />\n` +
          '        </mxCell>',
      );
    }
    for (const edge of diagram.edges) {
      const label = edge.label || edge.conditionText || '';
      const style = edge.meta?.style || EDGE_STYLE;
      cells.push(
        `        <mxCell id="${escapeXml(edge.id)}" value="${escapeXml(label)}" style="${escapeXml(style)}" edge="1" parent="1" source="${escapeXml(edge.from)}" target="${escapeXml(edge.to)}" ${LABEL_ATTR}="${escapeXml(edge.label || '')}"${edge.conditionText ? ` ${CONDITION_ATTR}="${escapeXml(edge.conditionText)}"` : ''}${edge.kind ? ` ${EDGE_KIND_ATTR}="${escapeXml(edge.kind)}"` : ''}>\n` +
          '          <mxGeometry relative="1" as="geometry" />\n' +
          '        </mxCell>',
      );
    }
    return (
      `  <diagram name="${escapeXml(diagram.title)}"${diagram.description ? ` ${DESCRIPTION_ATTR}="${escapeXml(diagram.description)}"` : ''}${diagram.processMeta ? ` ${PROCESS_META_ATTR}="${escapeXml(JSON.stringify(diagram.processMeta))}"` : ''}>\n` +
      '    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1169" pageHeight="826" math="0" shadow="0">\n' +
      '      <root>\n' +
      '        <mxCell id="0" />\n' +
      '        <mxCell id="1" parent="0" />\n' +
      `${cells.join('\n')}\n` +
      '      </root>\n' +
      '    </mxGraphModel>\n' +
      '  </diagram>'
    );
  });
  return `<mxfile host="onto-modeling" modified="${new Date().toISOString()}" agent="onto-modeling-flowchart" version="24.0.0">\n${pages.join('\n')}\n</mxfile>\n`;
}
