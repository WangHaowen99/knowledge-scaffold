import { ICON_TYPES } from './semantic-icons.js';

export const STUDIO_3D_NODE_LIMIT = 500;
export const STUDIO_3D_PARTICLE_LIMIT = 20;
export const STUDIO_3D_COLORS = {
  ...Object.fromEntries(ICON_TYPES.map(({ key, color }) => [key, color])),
  cluster: '#648199',
};

const NAMED_ELEMENT_KINDS = new Set(['object', 'operation', 'script', 'metric', 'rule']);

export function studioNodeName(node) {
  return NAMED_ELEMENT_KINDS.has(node?.kind) ? String(node.label || '') : '';
}

// Only element names follow selection or chain highlighting, never pointer hover.
export function studioLabelNodeIds(nodes, enabled, { selectedId = '', activeNodeIds = new Set(), hasHighlight = false } = {}) {
  if (!enabled) return new Set();
  const focused = Boolean(selectedId) || (hasHighlight && activeNodeIds.size > 0);
  return new Set(nodes.filter(({ id, kind }) => NAMED_ELEMENT_KINDS.has(kind)
    && (!focused || id === selectedId || activeNodeIds.has(id))).map(({ id }) => id));
}

// Categories are display bands, not ontology inheritance or inferred relationships.
export function studioCategoryPositions(nodes = []) {
  const groups = new Map();
  for (const node of nodes) {
    const kind = node.kind || 'other';
    if (!groups.has(kind)) groups.set(kind, []);
    groups.get(kind).push(node);
  }
  if (!groups.size) return new Map();
  const above = ['operation', 'script'].filter((kind) => groups.has(kind));
  const below = ['rule', 'metric'].filter((kind) => groups.has(kind));
  [...groups.keys()].filter((kind) => !['object', ...above, ...below].includes(kind)).sort().forEach((kind) => {
    (above.length <= below.length ? above : below).push(kind);
  });
  // Keep the object layer at the origin even when some categories are absent.
  // With at least two other layers, neither side is left needlessly empty.
  while (Math.abs(above.length - below.length) > 1) {
    const [longer, shorter] = above.length > below.length ? [above, below] : [below, above];
    shorter.push(longer.pop());
  }
  const gap = 230;
  // Give same-category links distinct heights while retaining a gap between bands.
  const thickness = gap * 0.82;
  const height = (above.length + below.length) * gap + thickness;
  const columnsFor = (count) => Math.ceil(count / (count > 2 ? Math.max(2, Math.round(Math.sqrt(count / 1.8))) : 1));
  const densestColumns = Math.max(...[...groups.values()].map((members) => columnsFor(members.length)));
  const width = Math.max(720, (densestColumns - 1) * 150, height * 1.75);
  const depth = width * 0.6;
  const centers = new Map([
    ...above.map((kind, index) => [kind, -(index + 1) * gap]),
    ['object', 0],
    ...below.map((kind, index) => [kind, (index + 1) * gap]),
  ]);
  const result = new Map();
  [...centers].forEach(([kind, centerY], bandIndex) => {
    const members = groups.get(kind);
    if (!members) return;
    members.sort((a, b) => String(a.id).localeCompare(String(b.id)));
    // A disk fills the width and depth without the collinear rows of a grid.
    // The independent height sequence avoids turning that disk into a tilted sheet.
    const seeds = members.map((node, index) => {
      const radius = Math.sqrt((index + 0.5) / members.length);
      const angle = (index + bandIndex * 0.5) * 2.39996323;
      return {
        x: Math.cos(angle) * radius,
        y: (index * 0.754877666 + 0.5) % 1 - 0.5,
        z: Math.sin(angle) * radius,
      };
    });
    const offsets = {};
    [['x', width], ['y', thickness], ['z', depth]].forEach(([axis, extent]) => {
      const values = seeds.map((point) => point[axis]);
      const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
      const centered = values.map((value) => value - mean);
      // Bound height about the category center, even for asymmetric small groups.
      const span = axis === 'y' ? Math.max(...centered.map(Math.abs)) * 2
        : Math.max(...values) - Math.min(...values);
      offsets[axis] = centered.map((value) => span > 1e-9 ? value / span * extent : 0);
    });
    members.forEach((node, index) => {
      result.set(node.id, {
        x: offsets.x[index],
        y: centerY + offsets.y[index],
        z: offsets.z[index],
      });
    });
  });
  return result;
}

// Each closed cluster still occupies one rendered node. Preserve that budget when
// expanding several clusters at once, including repeated clicks on large graphs.
export function boundedStudioClusterExpansion(clusters = {}, requestedIds = [], limit = STUDIO_3D_NODE_LIMIT) {
  let count = Object.keys(clusters).length;
  const expanded = [];
  for (const id of new Set(requestedIds)) {
    const cluster = clusters[id];
    if (!cluster) continue;
    const growth = Math.max(0, cluster.memberIds.length - 1);
    if (count + growth > limit) continue;
    expanded.push(id);
    count += growth;
  }
  return expanded;
}
