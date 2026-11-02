// 自动布局：主流程用 dagre 自上而下排，数据对象挂在它连着的步骤右侧（与设计稿的版式一致）
import dagre from '@dagrejs/dagre';
import { nodeSize } from './flowchartModel.js';

export function autoLayout(model) {
  const flowNodes = model.nodes.filter((n) => n.type !== 'data');
  const dataNodes = model.nodes.filter((n) => n.type === 'data');
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'TB', ranksep: 64, nodesep: 90, marginx: 0, marginy: 0 });
  flowNodes.forEach((n) => {
    const { w, h } = nodeSize(n);
    g.setNode(n.id, { width: w, height: h });
  });
  const ids = new Set(flowNodes.map((n) => n.id));
  model.edges.forEach((e) => { if (ids.has(e.from) && ids.has(e.to)) g.setEdge(e.from, e.to); });
  dagre.layout(g);
  const pos = {};
  flowNodes.forEach((n) => {
    const p = g.node(n.id);
    pos[n.id] = { x: Math.round(p.x + 150), y: Math.round(p.y + 60) };
  });
  // 数据对象：放在第一个连着的步骤右边，同一步骤挂多个时往下错开
  const used = new Map();
  dataNodes.forEach((d) => {
    const link = model.edges.find((e) => (e.from === d.id && ids.has(e.to)) || (e.to === d.id && ids.has(e.from)));
    const host = link ? model.nodes.find((n) => n.id === (link.from === d.id ? link.to : link.from)) : null;
    if (!host) { pos[d.id] = { x: d.x, y: d.y }; return; }
    const k = used.get(host.id) || 0;
    used.set(host.id, k + 1);
    const hw = nodeSize(host).w;
    const dw = nodeSize(d).w;
    pos[d.id] = { x: pos[host.id].x + hw / 2 + 126 + dw / 2, y: pos[host.id].y + k * 56 };
  });
  return pos;
}
