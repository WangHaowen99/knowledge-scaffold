// 流程图模型（契约 7.11 FlowchartModel）的纯函数：节点尺寸、统计、连线几何、适配视野、规范化与本地校验。
// 坐标约定：节点的 x、y 是节点中心（与设计稿 fcCanvas 一致），尺寸按类型和文字长度算，不存进模型。

export const NODE_TYPE_LABEL = { start: '开始', end: '结束', step: '业务步骤', decision: '判断', data: '数据对象' };
export const EDGE_TYPE_LABEL = { seq: '顺序流转', cond: '条件分支', exc: '异常流转', data: '数据流转' };

// 连线颜色与虚线（设计稿 fcCanvas 的 st 表）
export const EDGE_STYLE = {
  seq: { color: '#7C8CA6', dash: '' },
  cond: { color: '#F59E0B', dash: '' },
  exc: { color: '#DC2626', dash: '6 4' },
  data: { color: '#0D9488', dash: '2 4' },
};

const BASE_SIZE = { start: [104, 38], end: [104, 38], step: [132, 42], decision: [144, 72], data: [116, 36] };

// 估算文字宽度：中日韩字符按字号宽，其余按 0.56 倍
export function textWidth(text, fontSize = 12.5) {
  let w = 0;
  for (const ch of String(text || '')) w += /[⺀-￯]/.test(ch) ? fontSize : fontSize * 0.56;
  return w;
}

export function nodeSize(node) {
  const type = BASE_SIZE[node?.type] ? node.type : 'step';
  const [bw, bh] = BASE_SIZE[type];
  const fs = type === 'data' ? 12 : 12.5;
  const tw = textWidth(node?.label, fs);
  // 判断是菱形，文字只能占中间一半宽
  const need = type === 'decision' ? tw * 1.8 + 24 : tw + 28;
  return { w: Math.max(bw, Math.round(need)), h: bh };
}

// 「8 个节点、1 个判断」：数据对象不算节点
export function summarize(model) {
  const nodes = (model?.nodes || []).filter((n) => n.type !== 'data');
  const decisions = nodes.filter((n) => n.type === 'decision').length;
  return { nodes: nodes.length, decisions, text: `${nodes.length} 个节点、${decisions} 个判断` };
}

const asList = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);

// 补齐缺省字段，保证编辑器拿到的形状总是完整的
export function normalizeModel(model) {
  const m = model || {};
  const ids = new Set();
  const uniq = (prefix, id) => {
    let v = id || '';
    let i = 1;
    while (!v || ids.has(v)) { v = `${prefix}${i}`; i += 1; }
    ids.add(v);
    return v;
  };
  const p = m.process || {};
  const nodes = (m.nodes || []).map((n) => ({
    id: uniq('n', n.id),
    type: BASE_SIZE[n.type] ? n.type : 'step',
    label: n.label || '',
    description: n.description || '',
    x: Number.isFinite(n.x) ? n.x : 0,
    y: Number.isFinite(n.y) ? n.y : 0,
    refs: Array.isArray(n.refs) ? n.refs : [],
    pending: !!n.pending,
    pending_note: n.pending_note || null,
  }));
  const nodeIds = new Set(nodes.map((n) => n.id));
  const edges = (m.edges || [])
    .filter((e) => nodeIds.has(e.from) && nodeIds.has(e.to))
    .map((e) => ({
      id: uniq('e', e.id),
      type: EDGE_STYLE[e.type] ? e.type : 'seq',
      from: e.from,
      to: e.to,
      label: e.label || '',
      condition: e.condition || '',
    }));
  return {
    storage: m.storage === 'drawio_compressed' ? 'drawio_compressed' : 'standard',
    process: {
      name: p.name || '',
      description: p.description || '',
      roles: asList(p.roles),
      inputs: asList(p.inputs),
      exceptions: asList(p.exceptions),
      handoffs: asList(p.handoffs),
    },
    nodes,
    edges,
    drawio_xml: m.drawio_xml ?? null,
    operation: m.operation || null,
  };
}

const SIDE_VEC = { l: [-1, 0], r: [1, 0], t: [0, -1], b: [0, 1] };
const SLOT = 16;

function sidePoint(n, side, offset = 0) {
  const { w, h } = nodeSize(n);
  const [vx, vy] = SIDE_VEC[side];
  // 偏移沿边的方向；菱形的顶点只有一个点，偏移收小
  const k = n.type === 'decision' ? 0.4 : 1;
  return vx ? [n.x + (vx * w) / 2, n.y + offset * k] : [n.x + offset * k, n.y + (vy * h) / 2];
}

/**
 * 连线端点：按两节点中心的主方向取边的中点；同一边上有多条线时沿边错开（每条 16）。
 * 返回 Map(edgeId → {x1, y1, x2, y2, mx, my})。
 */
export function edgeGeometry(nodes, edges) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const ends = new Map(); // `${nodeId}:${side}` → [{edgeId, end, other}]
  const sides = new Map();
  edges.forEach((e) => {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) return;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const horiz = Math.abs(dx) > Math.abs(dy);
    const sf = horiz ? (dx > 0 ? 'r' : 'l') : (dy > 0 ? 'b' : 't');
    const st = horiz ? (dx > 0 ? 'l' : 'r') : (dy > 0 ? 't' : 'b');
    sides.set(e.id, [sf, st]);
    [[a, sf, 'from', b], [b, st, 'to', a]].forEach(([n, side, end, other]) => {
      const k = `${n.id}:${side}`;
      if (!ends.has(k)) ends.set(k, []);
      ends.get(k).push({ edgeId: e.id, end, other });
    });
  });
  const offsets = new Map();
  ends.forEach((list, k) => {
    const side = k.slice(k.lastIndexOf(':') + 1);
    const along = side === 'l' || side === 'r' ? 'y' : 'x';
    list.sort((p, q) => p.other[along] - q.other[along]);
    list.forEach((it, i) => offsets.set(`${it.edgeId}:${it.end}`, (i - (list.length - 1) / 2) * SLOT));
  });
  const out = new Map();
  edges.forEach((e) => {
    const s = sides.get(e.id);
    if (!s) return;
    const [x1, y1] = sidePoint(byId.get(e.from), s[0], offsets.get(`${e.id}:from`) || 0);
    const [x2, y2] = sidePoint(byId.get(e.to), s[1], offsets.get(`${e.id}:to`) || 0);
    out.set(e.id, { x1, y1, x2, y2, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 });
  });
  return out;
}

export function bounds(nodes) {
  if (!nodes.length) return { x: 0, y: 0, w: 0, h: 0 };
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  nodes.forEach((n) => {
    const { w, h } = nodeSize(n);
    x0 = Math.min(x0, n.x - w / 2);
    y0 = Math.min(y0, n.y - h / 2);
    x1 = Math.max(x1, n.x + w / 2);
    y1 = Math.max(y1, n.y + h / 2);
  });
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

// 取景留白（流程单位）：上方给图例、右侧给缩放按钮留位置，与设计稿 FC_DEV 的 viewBox 一致
const MARGIN = { left: 108, right: 128, top: 107, bottom: 63 };

export function fitBox(nodes, margin = MARGIN) {
  const b = bounds(nodes);
  return { x: b.x - margin.left, y: b.y - margin.top, w: b.w + margin.left + margin.right, h: b.h + margin.top + margin.bottom };
}

// 视野（React Flow viewport）：和设计稿一样按 fitBox（流程单位的留白）整体缩放居中；
// 图缩得很小时流程单位的留白放不下左上角的图例，改为按屏幕像素留白（上方至少 LEGEND_CLEAR）。
const LEGEND_CLEAR = 78;
const SCREEN_MARGIN = { left: 91, right: 107, top: LEGEND_CLEAR + 6, bottom: 53 };

export function fitViewport(nodes, width, height, { minZoom = 0.3, maxZoom = 1.5 } = {}) {
  if (!width || !height || !nodes.length) return { x: 0, y: 0, zoom: 1 };
  const clamp = (z) => Math.max(minZoom, Math.min(maxZoom, z));
  const box = fitBox(nodes);
  const z1 = clamp(Math.min(width / box.w, height / box.h));
  if (MARGIN.top * z1 >= LEGEND_CLEAR) {
    return { x: (width - box.w * z1) / 2 - box.x * z1, y: (height - box.h * z1) / 2 - box.y * z1, zoom: z1 };
  }
  const m = SCREEN_MARGIN;
  const b = bounds(nodes);
  const aw = Math.max(40, width - m.left - m.right);
  const ah = Math.max(40, height - m.top - m.bottom);
  const zoom = clamp(Math.min(aw / (b.w || 1), ah / (b.h || 1)));
  return { x: m.left + (aw - b.w * zoom) / 2 - b.x * zoom, y: m.top + (ah - b.h * zoom) / 2 - b.y * zoom, zoom };
}

// 新连线的类型：从判断出来的是条件分支，连到数据对象的是数据流转
export function edgeTypeFor(from, to) {
  if (from?.type === 'data' || to?.type === 'data') return 'data';
  if (from?.type === 'decision') return 'cond';
  return 'seq';
}

// 新节点放在已有节点下方（没有节点时放在左上）
export function nextNodePosition(nodes) {
  if (!nodes.length) return { x: 150, y: 60 };
  const steps = nodes.filter((n) => n.type !== 'data');
  const list = steps.length ? steps : nodes;
  const bottom = list.reduce((a, n) => (n.y > a.y ? n : a), list[0]);
  return { x: bottom.x, y: bottom.y + 120 };
}

// 列表字段（参与角色、流程输入……）在文本框里每行一项
export const listToText = (list) => (list || []).join('\n');
export const textToList = (text) => String(text || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);

const nameOf = (n) => `「${n.label || NODE_TYPE_LABEL[n.type]}」`;

/**
 * 确定性检查（AR-08 的口径，mock 和保存前的提示共用）：
 * 错误：没有开始或结束、节点没有名称、判断少于两个出口、节点孤立；提醒：口径待定的节点、条件分支没写条件。
 * finding.field 写 `nodes/<id>` 或 `edges/<id>`，界面据此定位。
 */
export function checkFlowchart(model) {
  const m = normalizeModel(model);
  const errors = [];
  const warnings = [];
  const add = (list, code, message, field) => list.push({ code, level: list === errors ? 'error' : 'warning', message, field });
  if (!m.nodes.some((n) => n.type === 'start')) add(errors, 'E-FC-START', '缺少开始节点', null);
  if (!m.nodes.some((n) => n.type === 'end')) add(errors, 'E-FC-END', '缺少结束节点', null);
  m.nodes.forEach((n) => {
    if (!n.label.trim()) add(errors, 'E-FC-LABEL', `有一个${NODE_TYPE_LABEL[n.type]}没有名称`, `nodes/${n.id}`);
    const out = m.edges.filter((e) => e.from === n.id && e.type !== 'data');
    const touched = m.edges.some((e) => e.from === n.id || e.to === n.id);
    if (!touched && m.nodes.length > 1) add(errors, 'E-FC-ISOLATED', `${nameOf(n)}没有连线`, `nodes/${n.id}`);
    if (n.type === 'decision' && out.length < 2) add(errors, 'E-FC-BRANCH', `判断${nameOf(n)}少于两个出口`, `nodes/${n.id}`);
    if (n.pending) add(warnings, 'W-FC-PENDING', `${nameOf(n)}口径待定`, `nodes/${n.id}`);
  });
  m.edges.forEach((e) => {
    if (e.type === 'cond' && !e.label.trim() && !e.condition.trim()) add(warnings, 'W-FC-COND', '有条件分支没写条件', `edges/${e.id}`);
  });
  return { ok: errors.length === 0, errors, warnings };
}

// 校验结果的一句话提示（设计稿：流程图校验通过：0 错误，1 提醒（「取标准限值」口径待定））
export function checkSummary({ errors = [], warnings = [] }) {
  const head = errors.length ? '流程图校验没通过' : '流程图校验通过';
  const tail = warnings.length ? `（${warnings.slice(0, 2).map((w) => w.message).join('；')}${warnings.length > 2 ? ' 等' : ''}）` : '';
  return `${head}：${errors.length} 错误，${warnings.length} 提醒${tail}`;
}
