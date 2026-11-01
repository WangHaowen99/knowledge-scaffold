// 流程（契约 FL-01 / EP-01）的前端视图计算。
// 阶段定义只有一处：core 包的 flow.yaml，前端不自带阶段表，节点、分组、命令归属都从 GET /flow 读（ADR 0025）。
// P1 的 /flow 只给 4 种状态、命令文本形式的 actions、顶层 sections 与 commands；P2 起给全量的 8 种状态与切片。
// normalizeFlow 把两种形状收成同一份视图数据，页面只认归一后的字段。

export const SECTION_TITLES = {
  required: '必经步骤',
  pre_publish: '发布前检查',
  optional: '可选步骤',
  on_demand: '有需要时',
};

// NodeState → 显示态（契约 7.8 DisplayState；pending_confirm 显示为「待确认」）
const DISPLAY_OF = { pending_confirm: 'pending' };

// 节点动作里 target 指向的页面（P2 起 refresh / confirm 带页面 id；P1 的 start 带命令文本）
const PAGE_TARGETS = new Set([
  'wb-l2', 'inbox', 'incremental', 'scenarios', 'scripts', 'knowledge', 'studio', 'studio-table', 'reports',
  'resources', 'sources', 'snapshots', 'schema', 'publish', 'versions', 'proj-settings', 'wb-materials', 'wb-blueprint', 'l4-run',
]);

export function normalizeFlow(raw) {
  if (!raw) return raw;
  const sections = Array.isArray(raw.sections) && raw.sections.length ? raw.sections : null;
  const sectionOf = {};
  (sections || []).forEach((s) => (s.nodes || []).forEach((id) => { sectionOf[id] = s.id; }));
  const nodes = (raw.nodes || []).map((n) => {
    const state = n.state || 'not_ready';
    return {
      kind: 'agent',
      placement: 'spine',
      flags: { needs_review: false, disabled: false, backfill: false },
      progress: null,
      version: '',
      per_slice: false,
      note: null,
      blocks: [],
      plugin: null,
      ...n,
      state,
      section: n.section || sectionOf[n.id] || 'required',
      display_state: n.display_state || DISPLAY_OF[state] || state,
      actions: n.actions || [],
    };
  });
  return {
    ...raw,
    nodes,
    edges: raw.edges || [],
    sections: sections || Object.keys(SECTION_TITLES).map((id) => ({ id, title: SECTION_TITLES[id], nodes: nodes.filter((n) => n.section === id).map((n) => n.id) })),
    slice_stages: raw.slice_stages || [],
    slices: raw.slices || [],
    notices: raw.notices || [],
    commands: raw.commands || {},
  };
}

// 项目流里流程增量合进快照
export function mergeFlow(flow, patch) {
  if (!flow) return flow;
  const nodes = flow.nodes.map((n) => {
    const p = (patch.nodes || []).find((x) => x.id === n.id);
    if (!p) return n;
    const merged = { ...n, ...p };
    // 增量只带 state 时，显示态跟着 state 走
    if (p.state && !p.display_state) merged.display_state = DISPLAY_OF[p.state] || p.state;
    return merged;
  });
  const slices = (flow.slices || []).map((s) => {
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
  (flow.edges || []).forEach((e) => {
    if (ids.has(e.from) && ids.has(e.to)) { next.set(e.from, e.to); hasIn.add(e.to); }
  });
  const order = [];
  let cur = spine.find((n) => !hasIn.has(n.id))?.id;
  while (cur && !order.includes(cur)) { order.push(cur); cur = next.get(cur); }
  spine.forEach((n) => { if (!order.includes(n.id)) order.push(n.id); });
  return order.map((id) => spine.find((n) => n.id === id));
}

// 建模步骤抽屉的分组：按 sections 的顺序，空组不出
export function sectionGroups(flow) {
  if (!flow) return [];
  const byId = new Map(flow.nodes.map((n) => [n.id, n]));
  return (flow.sections || [])
    .map((s) => ({ id: s.id, title: s.title || SECTION_TITLES[s.id] || s.id, nodes: (s.nodes || []).map((id) => byId.get(id)).filter(Boolean) }))
    .filter((s) => s.nodes.length);
}

/**
 * 节点动作怎么执行：
 * - navigate：target 是页面 id（P2 的「刷新」去场景走查、「去确认」去待处理）；
 * - command：target 是命令文本（P1 的 start，如 `/ontology-extract-object`），经 S-20 发到对话；
 * - skip / restore / start：调 FL-03 / FL-04 / FL-05。
 */
export function nodeActionKind(action) {
  if (!action) return null;
  const t = action.target;
  if (t && PAGE_TARGETS.has(t)) return 'navigate';
  if (t) return 'command';
  if (action.kind === 'skip' || action.kind === 'restore') return action.kind;
  return 'start';
}

// 命令文本 → 命令名：去掉斜杠与 `ontology-` 前缀，只取第一个词（`/ontology-trace 全部` → `trace`）
export function commandName(text) {
  const m = /^\/(\S+)/.exec((text || '').trim());
  if (!m) return null;
  return m[1].replace(/^ontology-/, '');
}

// 运行中的命令属于哪个节点（FL-01 的 commands：命令名 → 节点 ID）
export function commandNode(flow, text) {
  const name = commandName(text);
  if (!name || !flow?.commands) return null;
  return flow.commands[name] || null;
}

// 对话正在跑某个命令、服务端还没把节点标成进行中时，进度条先按命令归属显示「进行中」
export function overlayRunning(flow, nodeId) {
  if (!flow || !nodeId) return flow;
  const hit = flow.nodes.find((n) => n.id === nodeId);
  if (!hit || hit.state === 'running' || hit.display_state === 'running') return flow;
  return { ...flow, nodes: flow.nodes.map((n) => (n.id === nodeId ? { ...n, state: 'running', display_state: 'running' } : n)) };
}
