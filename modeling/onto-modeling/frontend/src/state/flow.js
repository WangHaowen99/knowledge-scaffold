// 流程（契约 FL-01 / EP-01）的前端视图计算。
// 阶段定义只有一处：core 包的 flow.yaml，前端不自带阶段表，节点、分组、命令归属都从 GET /flow 读（ADR 0025）。
// P1 的 /flow 只给 4 种状态、命令文本形式的 actions、顶层 sections 与 commands；P2 起给全量的 8 种状态与建模的步骤。
// normalizeFlow 把两种形状收成同一份视图数据，页面只认归一后的字段。


export const SECTION_TITLES = {
  required: '必经步骤',
  pre_publish: '发布前检查',
  optional: '可选步骤',
  on_demand: '有需要时',
};

// NodeState → 显示态（契约 7.8 DisplayState；pending_confirm 显示为「待确认」）
const DISPLAY_OF = { pending_confirm: 'pending' };

export function normalizeFlow(raw) {
  if (!raw) return raw;
  const sections = Array.isArray(raw.sections) && raw.sections.length ? raw.sections : null;
  const sectionOf = {};
  (sections || []).forEach((s) => (s.nodes || []).forEach((id) => { sectionOf[id] = s.id; }));
  const nodes = (raw.nodes || []).map((n) => {
    const state = n.state || 'not_ready';
    // 节点什么时候要做（need）、上不上导航栏（nav）、挂在哪个必需节点下（under）：老响应没有时按 optional、placement 推；
    // 条件节点出现在流程里就是条件成立了，上导航栏
    const need = n.need || (n.optional ? 'optional' : 'required');
    const mainline = need === 'required' && ['spine', 'step', 'gate'].includes(n.placement || 'spine');
    return {
      kind: 'agent',
      placement: 'spine',
      need,
      nav: (mainline || isWhen(need)) && !n.flags?.disabled,
      under: null,
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

// 节点集合变了（如启用、停用了节点包）时 flow_hash 跟着变；增量只能合已有节点，这时要重取整份流程（FL-01）
export const flowNeedsReload = (flow, patch) => !!flow && !!patch?.flow_hash && patch.flow_hash !== flow.flow_hash;

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

// 顶栏轨迹的样式：已完成的几种显示态（需复核、自主完成、通过）都打勾；可补做（导入后越过的资料分析等，ADR-0084）不算待办，淡显；
// 过期是做过、上游改了要重跑，和没做过的空圈分开（原来画成空圈，看着像流程往回退）
const TRACK_CLASS = {
  confirmed: 'ok', needs_review: 'ok', auto: 'ok', pass: 'ok', running: 'running', pending: 'pending', blocked: 'blocked', backfill: 'skip',
  stale: 'stale',
};
export const trackClass = (displayState) => TRACK_CLASS[displayState] || '';

export const isWhen = (need) => !!need && typeof need === 'object' && !!need.when;

// 导航栏：上导航栏（nav）的节点按流程顺序（后端按 after、before 排好的节点顺序）；条件节点（条件成立时才在流程里）
// 排在它挂的节点（under）后面。已停用的包的节点不上
export function trackNodes(flow) {
  if (!flow) return [];
  const nav = flow.nodes.filter((n) => n.nav && !n.flags?.disabled);
  const ids = new Set(nav.map((n) => n.id));
  const hangs = (n) => isWhen(n.need) && !!n.under && ids.has(n.under) && n.under !== n.id;
  const out = [];
  nav.filter((n) => !hangs(n)).forEach((n) => {
    out.push(n);
    nav.filter((x) => hangs(x) && x.under === n.id).forEach((x) => out.push(x));
  });
  return out;
}

// 动作的去处：斜杠命令发到对话（S-20）；页面编号（「发布」的 publish）跳页；别的文字（「核验资料分析报告」那句指令）当普通消息发
export function actionKind(action) {
  const target = String(action?.target || '').trim();
  if (target.startsWith('/')) return 'command';
  if (!action?.verify && /^[a-z][a-z0-9-]*$/.test(target)) return 'page';
  return 'message';
}

// 节点下拉里这个节点自己的按钮（FL-01 actions，动作的 when 后端已经筛过）：主命令在前、变体在后。
// 过期时后端在最前面多给一条 refresh（叫节点名，发只查改动条目的命令），它顶替主命令；跳过、恢复、旧的「去确认」不在这里
// 脚本节点由内核在沙箱里跑（FL-05）：节点名是主按钮（kind node，target 是节点 ID）；包另外声明的同名命令不再列
// （原来点它是助手按命令自己读文件判断，没跑脚本）。人工节点照旧按声明的动作（发布去页面）；面板节点在插件页里操作
const KERNEL_KINDS = ['script'];

export function nodeButtons(node) {
  const acts = (node?.actions || []).filter((a) => ['start', 'refresh'].includes(a.kind) && String(a.target || '').trim());
  if (KERNEL_KINDS.includes(node?.kind) && !node.panel) {
    const reason = acts.find((a) => a.kind === 'refresh')?.reason || '';
    const rest = acts.filter((a) => a.kind === 'start' && a.label !== node.title);
    return [{ label: node.title, target: node.id, kind: 'node', primary: true, reason },
      ...rest.map((a) => ({ label: a.label, target: String(a.target).trim(), kind: actionKind(a), primary: false, reason: a.reason || '' }))];
  }
  const refresh = acts.find((a) => a.kind === 'refresh');
  const starts = acts.filter((a) => a.kind === 'start');
  const primary = refresh || starts.find((a) => !a.hook && a.label === node.title) || starts[0];
  if (!primary) return [];
  const rest = starts.filter((a) => a !== primary && !(a.label === primary.label && !a.hook));
  return [primary, ...rest].map((a) => ({
    label: a.label, target: String(a.target).trim(), kind: actionKind(a), primary: a === primary, reason: a.reason || '',
  }));
}

// 可选节点「点名字就开始」：面板节点（求解适配，没有对话入口）去声明了这个节点的插件页；有斜杠命令的发它的主命令
// （过期时发 refresh 那条）；其余（脚本、人工节点）调 FL-05
export function optionalStart(node, pages = []) {
  if (node.panel) {
    const page = (pages || []).find((p) => (p.data?.nodes || []).includes(node.id));
    return page ? { kind: 'page', page: page.id, params: { node: node.id } } : null;
  }
  const [first] = nodeButtons(node);
  if (first?.kind === 'command') return { kind: 'command', label: node.title, target: first.target };
  return { kind: 'node', id: node.id };
}

/**
 * 导航栏节点的下拉（FL-01）：
 * - buttons：这个节点的按钮（nodeButtons）；
 * - optional：挂在它下面（under）的可选节点，每行名字、状态、一个小按钮（没跳过时「跳过」FL-03，跳过了「恢复」FL-04）；
 *   面板节点只给状态，没有小按钮；
 * - endRound：条件节点多一项「这一轮不处理」（FL-03）；
 * - direct：只有一个跳页面的按钮、别的都没有（发布）时不做下拉，点节点名直接去；
 * - empty：什么都没有，不做下拉。
 */
export function navMenu(flow, node, pages = []) {
  const buttons = nodeButtons(node);
  const optional = (flow?.nodes || [])
    .filter((n) => n.need === 'optional' && !n.nav && n.under === node.id && !n.flags?.disabled)
    .map((n) => ({
      id: n.id,
      title: n.title,
      display_state: n.display_state,
      op: n.panel ? null : n.state === 'skipped' ? 'restore' : 'skip',
      start: optionalStart(n, pages),
    }));
  const endRound = isWhen(node.need);
  const lone = buttons.length === 1 && buttons[0].kind === 'page' && !optional.length && !endRound;
  return {
    buttons, optional, endRound,
    direct: lone ? buttons[0] : null,
    empty: !buttons.length && !optional.length && !endRound,
  };
}

// 命令文本 → S-20 的 {command, args, text}：`键=值` 进 args，其余词是 text；
// 不以斜杠开头的（如「核验报告」的一句指令）当普通消息发，只给 text
export function commandFromTarget(target) {
  const parts = String(target || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length || !parts[0].startsWith('/')) return { text: String(target || '').trim() };
  const args = {};
  const words = [];
  parts.slice(1).forEach((w) => {
    const m = /^([A-Za-z_][A-Za-z0-9_-]*)=(.+)$/.exec(w);
    if (m) args[m[1]] = m[2];
    else words.push(w);
  });
  return { command: parts[0], ...(Object.keys(args).length ? { args } : {}), text: words.join(' ') };
}

// 全部命令（S-18）的条目 → S-20 请求体：发条目的完整命令文本（target，「/ontology-research 追加」），带上按钮名；
// 对话里的名字由服务端按名字表取。老响应没有 target 时发命令名
// FL-05 跑完脚本步骤的提示：结果和上次一样时步骤条、报告都不变，不提示就看不出跑过（复测 R1）
// resp 是 FL-05 的响应 {node, flow_hash, run}：结论在 node.last_run.summary（脚本输出第一行）
export function scriptRunText(resp) {
  const node = resp?.node;
  const summary = resp?.run && String(node?.last_run?.summary || '').trim();
  return summary ? `${node.title || ''}：${summary}` : '';
}

export function catalogCommand(item) {
  // 脚本步骤（S-18 kind node）同导航栏：由内核在沙箱里跑（FL-05），调用方按 node_id 走 startNode
  if (item?.kind === 'node') return { node_id: item.node_id || item.target, display_label: item.label || '' };
  return { ...commandFromTarget(item?.target || item?.command), display_label: item?.label || '' };
}

// 建议条的 send 动作 → S-20 请求体：建模的下一步给 command + args、text 为空串（contract-changes B4d 第 14 条），
// 不把空 text 当正文；core 的 _send 给的是整段命令文本（如「/ontology-research phase=blueprint」），拆成 command / args / text
export function suggestionCommand(action) {
  if (!action?.command) return null;
  const text = String(action.text || '').trim();
  if (!action.args && text.split(/\s+/)[0] === action.command) return commandFromTarget(text);
  return { command: action.command, ...(action.args ? { args: action.args } : {}), text };
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

// 自主构建的单元（契约里的 slice_id、slice）：现在一个节点一个单元，编号带包名前缀（core.object）；老运行按段，
// F8 之后只有一段「全部」（id all），之前是 s1、s2……。「全部」界面上不提，要有个叫法时叫「建模」；真的分段照原名显示
export const FULL_SLICE_ID = 'all';
export const isNodeUnit = (id) => !!id && id.includes('.');
// 这是老运行里真的一段时给段名（用在「这一段」「分段」的说法上），全部、节点都给空
export const sliceName = (id, name) => (id === FULL_SLICE_ID || isNodeUnit(id) ? '' : name || '');
export const unitTitle = (id, name) => (id === FULL_SLICE_ID ? '建模' : name || '');
// 一组行里有两段以上真的分段（老运行）时才出「分段」的列和说法
export const multiSlice = (ids) => (ids || []).filter((id) => sliceName(id, id)).length > 1;
