// 流程图缩略：按层从左到右排，判断节点后的分支上下分开（同设计稿 WB_FLOWTHUMB 的版式）
export function layoutThumb(nodes, edges) {
  const out = new Map(edges.map((e) => [e.from, []]));
  edges.forEach((e) => out.get(e.from).push(e.to));
  const indeg = new Map(nodes.map((n) => [n.id, 0]));
  edges.forEach((e) => indeg.set(e.to, (indeg.get(e.to) || 0) + 1));
  const level = new Map();
  const queue = nodes.filter((n) => !indeg.get(n.id)).map((n) => n.id);
  queue.forEach((id) => level.set(id, 0));
  while (queue.length) {
    const id = queue.shift();
    (out.get(id) || []).forEach((to) => {
      if (!level.has(to) || level.get(to) < level.get(id) + 1) {
        level.set(to, level.get(id) + 1);
        queue.push(to);
      }
    });
  }
  const byLevel = [];
  nodes.forEach((n) => {
    const l = level.get(n.id) ?? 0;
    (byLevel[l] = byLevel[l] || []).push(n);
  });
  const width = (n) => (n.type === 'start' || n.type === 'end' ? 80 : n.type === 'decision' ? 120 : Math.max(98, Math.round(n.label.length * 15.7 + 8)));
  const pos = new Map();
  let x = 16;
  byLevel.forEach((group, l) => {
    const w = Math.max(...group.map(width));
    group.forEach((n, i) => {
      const h = n.type === 'decision' ? 56 : n.type === 'start' || n.type === 'end' ? 36 : group.length > 1 ? 32 : 36;
      const cy = group.length > 1 ? (i === 0 ? 34 : 116) : 75;
      pos.set(n.id, { x, w: width(n), h, cy, n });
    });
    const fanOut = byLevel[l + 1]?.length > 1;
    x += w + (l === 0 ? 70 : fanOut ? 50 : 58);
  });
  return { pos, width: x - 50 + 12 };
}
