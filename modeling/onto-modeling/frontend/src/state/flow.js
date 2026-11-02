// 流程（契约 FL-01 / EP-01）的前端视图计算

// 项目流里流程增量合进快照
export function mergeFlow(flow, patch) {
  if (!flow) return flow;
  const nodes = flow.nodes.map((n) => {
    const p = (patch.nodes || []).find((x) => x.id === n.id);
    return p ? { ...n, ...p } : n;
  });
  const slices = flow.slices.map((s) => {
    const p = (patch.slices || []).find((x) => x.id === s.id);
    return p ? { ...s, ...p, stages: { ...s.stages, ...(p.stages || {}) } } : s;
  });
  return { ...flow, flow_hash: patch.flow_hash || flow.flow_hash, nodes, slices };
}

// 主线节点按边的顺序排（只放 spine / step / gate）
export function trackNodes(flow) {
  if (!flow) return [];
  const spine = flow.nodes.filter((n) => ['spine', 'step', 'gate'].includes(n.placement));
  const ids = new Set(spine.map((n) => n.id));
  const next = new Map();
  const hasIn = new Set();
  flow.edges.forEach((e) => {
    if (ids.has(e.from) && ids.has(e.to)) { next.set(e.from, e.to); hasIn.add(e.to); }
  });
  const order = [];
  let cur = spine.find((n) => !hasIn.has(n.id))?.id;
  while (cur && !order.includes(cur)) { order.push(cur); cur = next.get(cur); }
  spine.forEach((n) => { if (!order.includes(n.id)) order.push(n.id); });
  return order.map((id) => spine.find((n) => n.id === id));
}
