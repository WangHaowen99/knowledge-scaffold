// Mermaid remains the syntax authority. Only losslessly representable flowcharts
// use the legacy canvas; other diagrams retain Mermaid's own renderer.
const CONFIG = {
  startOnLoad: false,
  securityLevel: 'strict',
  theme: 'base',
  themeVariables: {
    fontFamily: 'system-ui, sans-serif',
    primaryColor: '#ffffff', primaryTextColor: '#252a31',
    primaryBorderColor: '#aeb8c7', lineColor: '#8793a5',
    secondaryColor: '#f3f6fa', tertiaryColor: '#edf5ef',
  },
  flowchart: { htmlLabels: true, useMaxWidth: true, curve: 'basis' },
};

let pending = Promise.resolve();
function withMermaid(action) {
  const result = pending.then(async () => {
    const { default: mermaid } = await import('mermaid');
    mermaid.initialize(CONFIG);
    return action(mermaid);
  });
  // Mermaid has shared configuration: serialize parse/render and recover after errors.
  pending = result.catch(() => {});
  return result;
}

export function parseMmdPresentation(source) {
  return withMermaid(async (mermaid) => {
    const diagram = await mermaid.mermaidAPI.getDiagramFromText(source);
    if (/^\s*---(?:\r?\n)|%%\{\s*(?:init|config)/u.test(source)) return { kind: 'native' };
    return toLegacyGraph(diagram);
  });
}

export function renderNativeMmd(id, source) {
  return withMermaid((mermaid) => mermaid.render(id, source));
}

function labelText(value) {
  // Mermaid encodes entities while parsing. Decode them into text only, never DOM.
  const encoded = String(value ?? '').replace(/ﬂ°°/gu, '&#').replace(/ﬂ°/gu, '&').replace(/¶ß/gu, ';');
  const withBreaks = encoded.replace(/<br\s*\/?\s*>/giu, '\n');
  // Formatted/HTML labels keep the native renderer; do not silently drop formatting.
  if (/<\/?[a-z][^>]*>/iu.test(withBreaks)) return null;
  const textarea = document.createElement('textarea');
  textarea.innerHTML = withBreaks.replace(/</gu, '&lt;');
  return textarea.value;
}

const SHAPES = new Map([
  [undefined, 'action'], ['square', 'action'], ['rect', 'action'], ['round', 'action'],
  ['diamond', 'decision'], ['stadium', 'terminal'], ['cylinder', 'data'],
]);

export function toLegacyGraph(diagram) {
  const native = { kind: 'native' };
  const db = diagram.db;
  if (!/^flowchart(?:-v2)?$/u.test(diagram.type)
    || typeof db?.getVertices !== 'function' || typeof db?.getEdges !== 'function') return native;
  if (db.getSubGraphs().length || db.getClasses().size || db.getDiagramTitle?.()
    || db.getAccTitle?.() || db.getAccDescription?.()) return native;
  const vertices = [...db.getVertices().values()];
  const links = db.getEdges();
  if (!vertices.length || links.defaultStyle?.length || links.defaultInterpolate) return native;
  const direction = db.getDirection() || 'TB';
  if (!['TB', 'TD', 'BT', 'LR', 'RL'].includes(direction)) return native;
  const nodes = [];
  const edges = [];
  for (const vertex of vertices) {
    if (!SHAPES.has(vertex.type) || vertex.labelType === 'markdown' || vertex.styles?.length
      || vertex.classes?.length || vertex.link || vertex.haveCallback || vertex.icon || vertex.img
      || vertex.dir || Object.keys(vertex.props || {}).length || db.getTooltip?.(vertex.id)) return native;
    const label = labelText(vertex.text ?? vertex.id);
    if (label === null) return native;
    nodes.push({ id: vertex.id, kind: SHAPES.get(vertex.type), label });
  }
  const ids = new Set(nodes.map((node) => node.id));
  const pairs = new Set();
  for (const [index, link] of links.entries()) {
    const pair = JSON.stringify([link.start, link.end]);
    // The simple legacy edge router cannot separate parallel/self-loop paths.
    if (link.start === link.end || pairs.has(pair)) return native;
    pairs.add(pair);
    if (!ids.has(link.start) || !ids.has(link.end) || link.style?.length || link.classes?.length
      || link.interpolate || link.labelType === 'markdown' || link.animate || link.animation
      || !['arrow_point', 'arrow_open', 'double_arrow_point'].includes(link.type)
      || !['normal', 'thick', 'dotted'].includes(link.stroke)) return native;
    const label = labelText(link.text);
    if (label === null) return native;
    edges.push({ id: link.id || `edge-${index}`, from: link.start, to: link.end, label,
      arrow: link.type, stroke: link.stroke, minLength: link.length || 1 });
  }
  // MMD uses one shape for both start and end. Classify only unambiguous terminals
  // by topology, not words such as "失败" which may occur in normal steps.
  for (const node of nodes) {
    if (node.kind !== 'terminal') continue;
    const incoming = edges.some((edge) => edge.to === node.id);
    const outgoing = edges.some((edge) => edge.from === node.id);
    if (!incoming && outgoing) node.kind = 'start';
    else if (incoming && !outgoing) node.kind = 'end';
  }
  return { kind: 'legacy', graph: { nodes, edges, direction: direction === 'TD' ? 'TB' : direction } };
}
