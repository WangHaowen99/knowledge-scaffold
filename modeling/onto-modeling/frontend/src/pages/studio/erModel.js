// Studio 的 ER 视图（包选用的内核视图 er_view，PL-12 view_choices）：对象是实体，对象之间的关系声明是连线。
// 布局用 dagre（从左到右），这里只算坐标，画法在 StudioErView.jsx。
import dagre from '@dagrejs/dagre';

export const ER_BOX_W = 196;
const HEAD_H = 30;
const ROW_H = 19;
const MAX_ROWS = 8;
const PAD = 24;

// 包选用了 ER 视图才有这个选项；依赖证据源的两种（schema_browser、evidence_sidebar）本期不出
export const hasErView = (extensions) => (extensions?.view_choices || []).some((v) => v.id === 'er_view');

// ST-02 的图 + 各对象的属性（ST-04 fields）→ 实体与关系
export function erData(graph, fieldsOf = {}) {
  const objects = (graph?.nodes || []).filter((n) => n.kind === 'object');
  const ids = new Set(objects.map((n) => n.id));
  const seen = new Set();
  const relations = (graph?.edges || []).filter((e) => e.kind === 'declared' && ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
    .filter((e) => {
      const k = `${e.source}>${e.target}>${e.label || ''}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .map((e) => ({ id: e.id, from: e.source, to: e.target, label: e.label || '' }));
  const entities = objects.map((n) => {
    const fields = fieldsOf[n.id] || [];
    const shown = fields.slice(0, MAX_ROWS).map((f) => ({ name: f.name, column: f.column || '' }));
    return { id: n.id, name: n.label, fields: shown, more: Math.max(0, fields.length - MAX_ROWS) };
  });
  return { entities, relations };
}

export const boxHeight = (e) => HEAD_H + (e.fields.length + (e.more ? 1 : 0)) * ROW_H + (e.fields.length || e.more ? 8 : 0);

export function erLayout({ entities = [], relations = [] } = {}) {
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setGraph({ rankdir: 'LR', nodesep: 28, ranksep: 90, edgesep: 14, marginx: PAD, marginy: PAD });
  g.setDefaultEdgeLabel(() => ({}));
  entities.forEach((e) => g.setNode(e.id, { width: ER_BOX_W, height: boxHeight(e) }));
  relations.forEach((r) => g.setEdge(r.from, r.to, { label: r.label, width: Math.min(140, r.label.length * 12 + 8), height: 16, labelpos: 'c' }, r.id));
  dagre.layout(g);
  const boxes = entities.map((e) => {
    const n = g.node(e.id);
    return { ...e, x: Math.round(n.x - n.width / 2), y: Math.round(n.y - n.height / 2), h: n.height };
  });
  const edges = relations.map((r) => {
    const ed = g.edge({ v: r.from, w: r.to, name: r.id });
    const pts = ed?.points || [];
    return {
      ...r,
      d: pts.map((p, i) => `${i ? 'L' : 'M'}${Math.round(p.x)} ${Math.round(p.y)}`).join(' '),
      lx: Math.round(ed?.x ?? 0),
      ly: Math.round(ed?.y ?? 0),
    };
  });
  const info = g.graph();
  return { width: Math.max(320, Math.ceil(info.width || 0)), height: Math.max(200, Math.ceil(info.height || 0)), boxes, edges };
}

export const ER_GEOMETRY = { HEAD_H, ROW_H };
