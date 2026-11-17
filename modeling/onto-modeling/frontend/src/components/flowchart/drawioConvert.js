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
const ANNOTATION_EDGE_ATTR = 'data-flowchart-annotation-edge';
const NODE_KINDS = new Set(['start', 'end', 'action', 'decision', 'object', 'data']);

export class FlowchartValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'FlowchartValidationError';
  }
}

export function validateFlowchartDocument(doc) {
  for (const [index, diagram] of (doc.diagrams || []).entries()) {
    const prefix = `第 ${index + 1} 张流程图`;
    const ids = new Set();
    const nodeIds = new Set();
    for (const node of diagram.nodes || []) {
      if (!node.id || typeof node.id !== 'string') {
        throw new FlowchartValidationError(`${prefix}存在缺少 ID 的节点`);
      }
      if (ids.has(node.id)) {
        throw new FlowchartValidationError(`${prefix}的节点 ID 重复：${node.id}`);
      }
      if (!NODE_KINDS.has(node.kind)) {
        throw new FlowchartValidationError(`${prefix}的节点类型无效：${node.kind || '空'}`);
      }
      ids.add(node.id);
      nodeIds.add(node.id);
    }
    for (const edge of diagram.edges || []) {
      if (!edge.id || typeof edge.id !== 'string') {
        throw new FlowchartValidationError(`${prefix}存在缺少 ID 的连线`);
      }
      if (ids.has(edge.id)) {
        throw new FlowchartValidationError(`${prefix}的单元格 ID 重复：${edge.id}`);
      }
      if (!nodeIds.has(edge.from) || !nodeIds.has(edge.to)) {
        throw new FlowchartValidationError(`${prefix}的连线 ${edge.id} 指向不存在的节点`);
      }
      ids.add(edge.id);
    }
    for (const annotation of diagram.annotations || []) {
      if (!annotation.id || typeof annotation.id !== 'string' || ids.has(annotation.id)) {
        throw new FlowchartValidationError(`${prefix}的文字标注 ID 缺失或重复`);
      }
      ids.add(annotation.id);
    }
  }
  return doc;
}

function styleToKind(style) {
  const s = style || '';
  if (s.includes('mxgraph.flowchart.terminator')) return 'terminator';
  if (s.includes('rhombus')) return 'decision';
  if (s.includes('shape=card')) return 'data';
  if (s.includes('backgroundOutline=1')) return 'object';
  return 'action';
}

function readXmlElement(element) {
  if (!element) return null;
  return {
    tag: element.tagName,
    attributes: Object.fromEntries(Array.from(element.attributes).map((attr) => [attr.name, attr.value])),
    children: Array.from(element.childNodes).filter((child) => child.nodeType === 1).map(readXmlElement),
  };
}

function pointToSegmentDistance(point, start, end) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const fraction = dx || dy
    ? Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy)))
    : 0;
  return Math.hypot(point.x - start.x - fraction * dx, point.y - start.y - fraction * dy);
}

function edgeDistanceToText(edge, textCell, nodeBounds) {
  const source = nodeBounds.get(edge.from);
  const target = nodeBounds.get(edge.to);
  const geometry = textCell.getElementsByTagName('mxGeometry')[0];
  if (!source || !target || !geometry) return Infinity;
  const center = {
    x: (Number(geometry.getAttribute('x')) || 0) + (Number(geometry.getAttribute('width')) || 0) / 2,
    y: (Number(geometry.getAttribute('y')) || 0) + (Number(geometry.getAttribute('height')) || 0) / 2,
  };
  const waypoints = edge.meta.geometry?.children
    ?.flatMap((child) => child.tag === 'Array' && child.attributes.as === 'points'
      ? child.children.filter((point) => point.tag === 'mxPoint').map((point) => ({
          x: Number(point.attributes.x), y: Number(point.attributes.y),
        })) : []) || [];
  const points = [source, ...waypoints, target];
  return Math.min(...points.slice(1).map((point, index) =>
    pointToSegmentDistance(center, points[index], point)));
}

function textAnnotation(cell) {
  return {
    id: cell.getAttribute('id'),
    edgeId: cell.getAttribute(ANNOTATION_EDGE_ATTR) || '',
    style: cell.getAttribute('style') || '',
    parent: cell.getAttribute('parent') || '1',
    geometry: readXmlElement(cell.getElementsByTagName('mxGeometry')[0]),
  };
}

export function parseDrawioToDocument(xml, fileName = '流程图') {
  const dom = new DOMParser().parseFromString(xml, 'text/xml');
  if (dom.getElementsByTagName('parsererror').length) throw new Error('drawio XML 解析失败');
  const diagramNodes = Array.from(dom.getElementsByTagName('diagram'));
  if (!diagramNodes.length) return { diagrams: [] };
  const models = diagramNodes.length
    ? diagramNodes.map((d) => ({
        name: d.getAttribute('name') || '',
        description: d.getAttribute(DESCRIPTION_ATTR) || '',
        processMeta: d.getAttribute(PROCESS_META_ATTR) || '',
        model: d.getElementsByTagName('mxGraphModel')[0],
      }))
    : [];

  const diagrams = models.map(({ name, description, processMeta, model }, index) => {
    if (!model) throw new Error('compressed'); // 压缩存储：交给 drawio 编辑器
    const cells = Array.from(model.getElementsByTagName('mxCell'));
    // 外层对象保存链接、提示和自定义数据；标准编辑器重建 XML 时无法保留它们。
    if (cells.some((cell) => ['object', 'UserObject'].includes(cell.parentNode?.tagName))) {
      throw new Error('unsupported wrapped cell');
    }
    const nodes = [];
    const edges = [];
    const positions = {};
    const nodeBounds = new Map();
    const annotations = [];
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
        if (/(?:^|;)text(?:;|$)/.test(style) && !cell.hasAttribute(NODE_KIND_ATTR)) {
          annotations.push({ ...textAnnotation(cell), value });
          continue;
        }
        let kind = cell.getAttribute(NODE_KIND_ATTR) || styleToKind(style);
        if (kind === 'terminator') kind = (inDegree.get(id) || 0) === 0 ? 'start' : 'end';
        const geo = cell.getElementsByTagName('mxGeometry')[0];
        if (geo) {
          const x = Number(geo.getAttribute('x')) || 0;
          const y = Number(geo.getAttribute('y')) || 0;
          const width = Number(geo.getAttribute('width')) || 0;
          const height = Number(geo.getAttribute('height')) || 0;
          positions[id] = {
            x, y,
          };
          nodeBounds.set(id, { x: x + width / 2, y: y + height / 2 });
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
        edges.push({
          id: id || `edge-${edges.length + 1}`,
          from,
          to,
          kind: cell.getAttribute(EDGE_KIND_ATTR)
            || (/^(是|否|Y|N|yes|no)$/i.test(value) ? 'condition' : 'sequence'),
          label: cell.hasAttribute(LABEL_ATTR) ? cell.getAttribute(LABEL_ATTR) : value,
          conditionText: cell.getAttribute(CONDITION_ATTR) || '',
          meta: {
            style: cell.getAttribute('style') || EDGE_STYLE,
            geometry: readXmlElement(cell.getElementsByTagName('mxGeometry')[0]),
          },
        });
      }
    }
    for (const annotation of annotations) {
      const cell = cells.find((candidate) => candidate.getAttribute('id') === annotation.id);
      const linkedEdge = annotation.edgeId
        ? edges.find((edge) => edge.id === annotation.edgeId) : null;
      if (linkedEdge) {
        if (!linkedEdge.label) linkedEdge.label = annotation.value;
        linkedEdge.meta.textAnnotationId = annotation.id;
        continue;
      }
      const candidates = edges
        .filter((edge) => !edge.label && !edge.conditionText && (outDegree.get(edge.from) || 0) > 1)
        .map((edge) => ({ edge, distance: edgeDistanceToText(edge, cell, nodeBounds) }))
        .sort((left, right) => left.distance - right.distance);
      if (candidates[0]?.distance <= 60
        && (candidates.length === 1 || candidates[1].distance - candidates[0].distance >= 20)) {
        candidates[0].edge.label = annotation.value;
        candidates[0].edge.kind = 'condition';
        annotation.edgeId = candidates[0].edge.id;
        candidates[0].edge.meta.textAnnotationId = annotation.id;
      }
    }
    if (annotations.some((annotation) => !annotation.edgeId)) {
      throw new Error('unsupported text annotation');
    }
    return {
      id: `diagram-${index + 1}`,
      title: name || fileName.replace(/\.(drawio|dio)$/i, ''),
      ...(description ? { description } : {}),
      ...(processMeta ? { processMeta: JSON.parse(processMeta) } : {}),
      nodes,
      edges,
      annotations,
      layout: { direction: 'TB', positions },
    };
  });
  return validateFlowchartDocument({ diagrams });
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

function serializeXmlElement(element) {
  if (!element) return '<mxGeometry relative="1" as="geometry" />';
  const attributes = Object.entries(element.attributes || {})
    .map(([key, value]) => ` ${key}="${escapeXml(value)}"`).join('');
  const children = (element.children || []).map(serializeXmlElement).join('');
  return children
    ? `<${element.tag}${attributes}>${children}</${element.tag}>`
    : `<${element.tag}${attributes} />`;
}

export function serializeDocumentToDrawio(doc) {
  validateFlowchartDocument(doc);
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
      const separateLabel = (diagram.annotations || []).some((annotation) =>
        annotation.edgeId === edge.id && annotation.id === edge.meta?.textAnnotationId);
      cells.push(
        `        <mxCell id="${escapeXml(edge.id)}" value="${escapeXml(separateLabel ? '' : label)}" style="${escapeXml(style)}" edge="1" parent="1" source="${escapeXml(edge.from)}" target="${escapeXml(edge.to)}" ${LABEL_ATTR}="${escapeXml(edge.label || '')}"${edge.conditionText ? ` ${CONDITION_ATTR}="${escapeXml(edge.conditionText)}"` : ''}${edge.kind ? ` ${EDGE_KIND_ATTR}="${escapeXml(edge.kind)}"` : ''}>\n` +
          `          ${serializeXmlElement(edge.meta?.geometry)}\n` +
          '        </mxCell>',
      );
    }
    for (const annotation of diagram.annotations || []) {
      const edge = annotation.edgeId
        ? diagram.edges.find((candidate) => candidate.id === annotation.edgeId) : null;
      if (annotation.edgeId && (!edge || !edge.label)) continue;
      cells.push(
        `        <mxCell id="${escapeXml(annotation.id)}" value="${escapeXml(edge ? edge.label : annotation.value)}" style="${escapeXml(annotation.style)}" vertex="1" parent="${escapeXml(annotation.parent)}"${edge ? ` ${ANNOTATION_EDGE_ATTR}="${escapeXml(edge.id)}"` : ''}>\n` +
          `          ${serializeXmlElement(annotation.geometry)}\n` +
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
