/* drawio 原文的结构修复（develop !613）：同号的节点改号或删掉，没连上节点的连线补建节点、改接或删掉。
 *
 * 只看第一页，和内核一样（后端 kernel/artifacts/flowchart.py）。每次修一项，改的是原文里对应的单元格，
 * 其余样式、泳道、批注、其他页不动；修完的 XML 交回服务端保存（AR-05 的 drawio_xml），和 drawio 高级编辑同一条路。
 */
import {
  TYPE_STYLE, drawioPage, drawioStructureError, drawioToModel, edgeTypeOf, isCompressedDrawio, labelHtml, nodeTypeOf, parseDrawio,
} from './drawioConvert.js';
import { nodeSize } from './flowchartModel.js';

const BOX = ['x', 'y', 'width', 'height'];
const read = (xml) => { const dom = parseDrawio(xml); return { dom, ...drawioPage(dom) }; };
const write = (dom) => new XMLSerializer().serializeToString(dom);
const geometryOf = (c) => c?.cell.getElementsByTagName('mxGeometry')[0] || null;
const num = (g, key, fallback = 0) => Number(g?.getAttribute(key)) || fallback;
const sameBox = (a, b) => !!a && !!b && BOX.every((k) => a.getAttribute(k) === b.getAttribute(k));
const spotOf = (x, g) => `${x},${num(g, 'y')}`;
const spotsOf = (cells) => new Set(cells.map((c) => spotOf(num(geometryOf(c), 'x'), geometryOf(c))));
const opposite = (endpoint) => (endpoint === 'source' ? 'target' : 'source');

function unusedId(used, prefix, serial = 1) {
  let n = serial;
  while (used.has(`${prefix}-${n}`)) n += 1;
  return `${prefix}-${n}`;
}

// 和原节点完全重叠时向右错开，跳过已经有节点的位置
function freeX(g, spots) {
  const step = Math.max(num(g, 'width'), 190) + 60;
  let x = num(g, 'x') + step;
  while (spots.has(spotOf(x, g))) x += step;
  return x;
}

function newNode(dom, { id, label, parent, x, y, width, height }) {
  const cell = dom.createElement('mxCell');
  const geometry = dom.createElement('mxGeometry');
  const set = (el, attrs) => Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, String(v)));
  set(cell, { id, value: labelHtml(label), style: TYPE_STYLE.step, vertex: 1, parent });
  set(geometry, { x, y, width, height, as: 'geometry' });
  cell.appendChild(geometry);
  return cell;
}

export const repairIssueKey = (issue) => `${issue.cellIndex}:${issue.type}:${issue.endpoint || ''}`;

export function repairIssueLabel(issue) {
  if (issue.type === 'duplicate') {
    return `节点「${issue.firstLabel || issue.id}」与「${issue.label || issue.id}」共用编号（当前修复后者）`;
  }
  const endpoint = issue.endpoint === 'source' ? '起点' : '终点';
  if (issue.edgeLabel) return `连线「${issue.edgeLabel}」${endpoint}未连接`;
  if (issue.oppositeLabel) {
    return issue.endpoint === 'source'
      ? `指向「${issue.oppositeLabel}」的连线，起点未连接`
      : `从「${issue.oppositeLabel}」出发的连线，终点未连接`;
  }
  return `连线「${issue.edgeId || '未编号'}」${endpoint}未连接`;
}

/** 面板能修的问题，按单元格顺序。 */
export function inspectDrawioRepair(xml) {
  const { cells, nodes, attached } = read(xml);
  const used = new Set(cells.map((c) => c.id));
  const firstById = new Map();
  const options = new Map();
  nodes.forEach((c) => { if (c.id && !options.has(c.id)) options.set(c.id, c.text); });
  const issues = [];
  cells.forEach((c) => {
    if (c.kind === 'node') {
      const first = firstById.get(c.id);
      if (!first) { firstById.set(c.id, c); return; }
      const g = geometryOf(c);
      issues.push({
        type: 'duplicate', cellIndex: c.index, id: c.id, label: c.text || c.id, firstLabel: first.text || c.id,
        title: `第 ${c.index + 1} 个单元格「${c.text || c.id}」与第 ${first.index + 1} 个共用 ID ${c.id}，位置 ${g ? `(${num(g, 'x')}, ${num(g, 'y')})` : '未知位置'}`,
        suggestion: unusedId(used, c.id, 2), overlap: sameBox(g, geometryOf(first)),
      });
    } else if (c.kind === 'edge') {
      const label = c.text || cells.filter((x) => x.kind === 'label' && c.id && x.parent === c.id).map((x) => x.text).filter(Boolean).join(' ');
      ['source', 'target'].filter((endpoint) => !attached(c[endpoint])).forEach((endpoint) => {
        const oppositeId = c[opposite(endpoint)];
        issues.push({
          type: 'dangling', cellIndex: c.index, endpoint, edgeId: c.id, missingId: c[endpoint], oppositeId,
          edgeLabel: label, oppositeLabel: options.get(oppositeId) || '',
          title: `连线 ${c.id || c.index + 1} 的${endpoint === 'source' ? '起点' : '终点'}「${c[endpoint] || '空'}」${c[endpoint] && used.has(c[endpoint]) ? '不是流程节点' : '不存在'}`,
          options: Array.from(options, ([value, text]) => ({
            value, label: `${text || value} (${value})${value === oppositeId ? ' · 会形成自环' : ''}`,
          })),
        });
      });
    }
  });
  return issues;
}

/** 面板列出的问题全部修完后能不能通过结构检查：在内存副本里给问题单元格赋有效值再查一遍，不碰原图。 */
export function canRepairDrawio(xml) {
  const issues = inspectDrawioRepair(xml);
  if (!issues.length) return false;
  const { dom, root, cells, nodes } = read(xml);
  const used = new Set(cells.map((c) => c.id));
  let anchor = nodes.find((c) => c.id)?.id || '';
  for (const issue of issues) {
    const c = cells[issue.cellIndex];
    if (issue.type === 'duplicate') {
      const id = unusedId(used, issue.id || 'node', 2);
      used.add(id);
      c.el.setAttribute('id', id);
    } else {
      if (!anchor) {
        if (!root) return false;
        anchor = unusedId(used, 'repair-check');
        used.add(anchor);
        root.appendChild(newNode(dom, { id: anchor, label: '', parent: '1', x: 0, y: 0, width: 100, height: 60 }));
      }
      c.cell.setAttribute(issue.endpoint, anchor);
    }
  }
  try {
    return !drawioStructureError(write(dom));
  } catch {
    return false;
  }
}

/**
 * 打开前看一眼原文：null 没问题（压缩存储、空图由模型决定怎么开）；repair 面板能修；
 * blocked 面板修不了，只能用 drawio 高级编辑。
 */
export function drawioSourceCheck(xml) {
  if (!/<mxGraphModel\b/.test(xml || '') || isCompressedDrawio(xml)) return null;
  try {
    const message = drawioStructureError(xml);
    if (!message) return null;
    return { kind: canRepairDrawio(xml) ? 'repair' : 'blocked', message };
  } catch (e) {
    return { kind: 'blocked', message: e.message };
  }
}

// 删除时一起去掉的单元格：连线带上归属它的文字标注和它上面的文字
function removedCells(cells, issue) {
  const self = cells[issue.cellIndex];
  if (issue.type !== 'dangling' || !self.id) return [self];
  return cells.filter((c) => c === self || c.annotationEdge === self.id || c.parent === self.id);
}

/** 修一项，返回改好的 XML。replacement：{mode: rename, id} / {mode: existing, nodeId} / {mode: create, label} / {mode: delete}。 */
export function applyDrawioRepair(xml, issue, replacement) {
  const { dom, root, cells, nodes } = read(xml);
  const c = cells[issue.cellIndex];
  const same = issue.type === 'duplicate'
    ? c?.kind === 'node' && c.id === issue.id
    : c?.kind === 'edge' && c.id === issue.edgeId && c[issue.endpoint] === issue.missingId;
  if (!same) throw new Error('待修复单元格已变化，请重新读取文件');
  const mode = replacement?.mode;
  const used = new Set(cells.filter((x) => x !== c).map((x) => x.id));

  if (mode === 'delete') {
    removedCells(cells, issue).forEach((x) => x.el.parentNode.removeChild(x.el));
  } else if (issue.type === 'duplicate') {
    const id = String(replacement?.id ?? '').trim();
    if (!id || used.has(id)) throw new Error('请输入当前图中未使用的节点 ID');
    c.el.setAttribute('id', id);
    const g = geometryOf(c);
    if (issue.overlap && g) g.setAttribute('x', String(freeX(g, spotsOf(nodes))));
  } else if (mode === 'existing') {
    const id = String(replacement.nodeId ?? '');
    if (!issue.options.some((o) => o.value === id)) throw new Error('请选择现有节点');
    c.cell.setAttribute(issue.endpoint, id);
  } else if (mode === 'create') {
    if (!root) throw new Error('流程图缺少根节点');
    const label = String(replacement.label ?? '').trim();
    if (!label) throw new Error('请输入新节点名称');
    const taken = new Set(cells.map((x) => x.id));
    const id = issue.missingId && !taken.has(issue.missingId) ? issue.missingId : unusedId(taken, 'node');
    const other = cells.find((x) => x.kind === 'node' && x.id && x.id === issue.oppositeId);
    const g = geometryOf(other);
    const x = num(g, 'x', 40);
    root.appendChild(newNode(dom, {
      id, label, parent: other?.parent || c.parent || '1',
      x: issue.endpoint === 'target' ? x + num(g, 'width', 160) + 100 : x - 260, y: num(g, 'y', 40), width: 160, height: 60,
    }));
    c.cell.setAttribute(issue.endpoint, id);
  } else throw new Error('请选择补建节点或连接到现有节点');
  return write(dom);
}

/** 修完一项后，其余问题上填了一半的内容跟着留下来：删除会让后面单元格的序号前移。 */
export function keepRepairDrafts(xml, issues, nextIssues, drafts, handled, replacement) {
  const removed = replacement?.mode === 'delete' ? removedCells(read(xml).cells, handled).map((c) => c.index) : [];
  const nextKeys = new Set(nextIssues.map(repairIssueKey));
  const kept = {};
  issues.forEach((issue) => {
    const key = repairIssueKey(issue);
    if (key === repairIssueKey(handled) || !(key in drafts) || removed.includes(issue.cellIndex)) return;
    const shift = removed.filter((index) => index < issue.cellIndex).length;
    const nextKey = repairIssueKey({ ...issue, cellIndex: issue.cellIndex - shift });
    if (nextKeys.has(nextKey)) kept[nextKey] = drafts[key];
  });
  return kept;
}

/** 修完后、保存前查一遍，通过时返回解析出的模型。 */
export function validateRepairedDrawio(xml) {
  const issues = inspectDrawioRepair(xml);
  if (issues.length) throw new Error(`仍有 ${issues.length} 项结构问题需要修复`);
  const message = drawioStructureError(xml);
  if (message) throw new Error(message);
  return drawioToModel(xml);
}

/**
 * 修复预览（FlowchartSvg 的模型）：同号节点各有各的编号，完全重叠的错开；连线没连上的一头画成「缺失节点」占位。
 * repairCellIndex 指回原文的单元格。
 */
export function repairPreviewGraph(xml) {
  const { cells, nodes: nodeCells, attached } = read(xml);
  const byId = new Map();
  cells.forEach((c) => { if (c.id && !byId.has(c.id)) byId.set(c.id, c); });
  const edgeCells = cells.filter((c) => c.kind === 'edge');
  const used = new Set(byId.keys());
  let serial = 0;
  const previewId = () => {
    serial += 1;
    while (used.has(`repair-preview-${serial}`)) serial += 1;
    return `repair-preview-${serial}`;
  };
  // 泳道、分组里的坐标相对父级，累加成绝对坐标（同内核 _offset）
  const offset = (c) => {
    const seen = new Set();
    let x = 0;
    let y = 0;
    for (let p = byId.get(c.parent); p && p.cell.getAttribute('vertex') === '1' && !seen.has(p); p = byId.get(p.parent)) {
      seen.add(p);
      x += num(geometryOf(p), 'x');
      y += num(geometryOf(p), 'y');
    }
    return { x, y };
  };

  const nodes = [];
  const spots = spotsOf(nodeCells);
  const shown = new Map(); // 原文 id → 预览节点（同号的只认第一个，连线都画到它身上）
  nodeCells.forEach((c) => {
    const g = geometryOf(c);
    const first = byId.get(c.id);
    const base = offset(c);
    const x = first && first !== c && sameBox(g, geometryOf(first)) ? freeX(g, spots) : num(g, 'x');
    spots.add(spotOf(x, g));
    const node = {
      id: c.id && !shown.has(c.id) ? c.id : previewId(),
      type: nodeTypeOf(c.attrs, c.style, edgeCells.filter((e) => e.target === c.id).length),
      label: c.text,
      x: Math.round(base.x + x + num(g, 'width', 120) / 2),
      y: Math.round(base.y + num(g, 'y') + num(g, 'height', 40) / 2),
      repairCellIndex: c.index,
    };
    if (c.id && !shown.has(c.id)) shown.set(c.id, node);
    nodes.push(node);
  });

  const missing = new Map();
  const beside = new Map();
  const placeholder = (edge, endpoint) => {
    const refId = edge[endpoint];
    if (refId && missing.has(refId)) return missing.get(refId);
    const near = shown.get(edge[opposite(endpoint)]);
    const spot = `${near?.id || ''}:${endpoint}`;
    const k = beside.get(spot) || 0;
    beside.set(spot, k + 1);
    const at = near || { x: 120, y: 70 };
    const node = {
      id: previewId(), type: 'step', label: `缺失节点：${refId || '空'}`, pending: true, missing: true,
      y: at.y + k * 90, repairCellIndex: edge.index,
    };
    node.x = at.x + (endpoint === 'target' ? 1 : -1) * (165 + Math.round(nodeSize(node).w / 2));
    nodes.push(node);
    if (refId) missing.set(refId, node);
    return node;
  };
  const edges = edgeCells.flatMap((e) => {
    const [from, to] = ['source', 'target'].map((endpoint) => (
      attached(e[endpoint]) ? shown.get(e[endpoint]) : placeholder(e, endpoint)));
    if (!from || !to) return []; // 连在泳道、批注上的连线不在预览里画
    return [{
      id: `repair-edge-${e.index}`, type: edgeTypeOf(e.attrs, e.style), from: from.id, to: to.id, label: e.text, repairCellIndex: e.index,
    }];
  });
  return { nodes, edges };
}

/** 点中的问题在预览里对应的节点或连线。 */
export function repairSelection(graph, issue) {
  if (!issue) return null;
  const type = issue.type === 'duplicate' ? 'node' : 'edge';
  const hit = (type === 'node' ? graph.nodes : graph.edges).find((x) => !x.missing && x.repairCellIndex === issue.cellIndex);
  return hit ? { type, id: hit.id } : null;
}
