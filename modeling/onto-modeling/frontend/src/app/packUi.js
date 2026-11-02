// 包的界面（ADR-0055）的纯逻辑：产物双击打开什么、官方包编辑器怎么认、界面接口版本够不够。
// 打开方式按 AR-01 的 view.detail：包自带编辑器（官方包随平台编译 bundled / 上传包 iframe）> 平台专用组件 > 通用详情。

export const UI_API = '1.0';
export const FLOWCHART_TYPE = 'diagram.flowchart';

// 平台专用组件 → 随平台编译的官方包编辑器（旧版图示包写的是 detail.component: flowchart，契约保留兼容）
export const COMPONENT_EDITORS = { flowchart: 'diagram/flowchart' };
// 平台专用组件 → 打开平台页面（详设 5.9.5 第二档）
export const COMPONENT_PAGES = { solver_contract: { page: 'scripts', query: { tab: 'sv' } } };

// import.meta.glob 的键 → `包/编辑器`：…/packs/<包>/ui/<编辑器>/index.jsx
export function bundledKey(path) {
  const m = /packs\/([a-z][a-z0-9-]*)\/ui\/([a-z][a-z0-9-]*)\/index\.jsx$/.exec(String(path || ''));
  return m ? `${m[1]}/${m[2]}` : null;
}

function parseVersion(text) {
  const m = /^(\d+)(?:\.(\d+))?(?:\.(\d+))?$/.exec(String(text || '').trim());
  return m ? [Number(m[1]), Number(m[2] || 0), Number(m[3] || 0)] : null;
}

function cmp(a, b) {
  for (let i = 0; i < 3; i += 1) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return 0;
}

// 界面接口版本是否落在范围里：空格分隔的 >= > <= < = 比较式，全部满足才算；空串不限；写错按不满足
export function uiApiSatisfies(range, version = UI_API) {
  const spec = String(range || '').trim();
  if (!spec || spec === '*') return true;
  const current = parseVersion(version);
  if (!current) return false;
  return spec.split(/[\s,]+/).every((token) => {
    const m = /^(>=|<=|>|<|=)?(.+)$/.exec(token);
    const bound = m && parseVersion(m[2]);
    if (!bound) return false;
    const c = cmp(current, bound);
    switch (m[1] || '=') {
      case '>=': return c >= 0;
      case '>': return c > 0;
      case '<=': return c <= 0;
      case '<': return c < 0;
      default: return c === 0;
    }
  });
}

/**
 * 产物双击打开什么。type：AR-01 的一项（包没启用时为 null）；bundled：随平台编译的编辑器 `包/编辑器` 集合。
 * 返回 {kind: 'disabled'|'bundled'|'iframe'|'page'|'generic', ...}。
 */
export function openModeFor(type, { bundled = new Set() } = {}) {
  if (!type) return { kind: 'disabled' };
  const detail = type.view?.detail || {};
  const editor = detail.editor;
  if (editor && uiApiSatisfies(editor.ui_api)) {
    const key = `${editor.pack}/${editor.id}`;
    if (editor.mode === 'bundled' && bundled.has(key)) return { kind: 'bundled', key, editor };
    if (editor.mode === 'iframe') return { kind: 'iframe', editor };
  }
  const component = detail.component;
  if (component && COMPONENT_EDITORS[component] && bundled.has(COMPONENT_EDITORS[component])) {
    return { kind: 'bundled', key: COMPONENT_EDITORS[component], editor: null };
  }
  if (component && COMPONENT_PAGES[component]) return { kind: 'page', component, ...COMPONENT_PAGES[component] };
  return { kind: 'generic', layout: detail.layout || 'sections' };
}

// 产物类型前缀就是包名（AR-01 type 规则）
export const packOfType = (type) => String(type || '').split('.')[0] || '';

/**
 * flowchart 页没带 ?key= 时打开哪一张：对话里最近一张流程图卡（旧的 flowchart 卡，或按图示包声明画的产物卡）的那张图。
 * state 是对话状态机（state/conversation.js）的状态；没有返回 null（由编辑器取 AR-02 的第一张）。
 */
export function latestFlowchartArtifact(state) {
  const order = state?.order || [];
  for (let i = order.length - 1; i >= 0; i -= 1) {
    const it = state.items[order[i]];
    const card = it?.kind === 'card' ? it.card : null;
    if (!card?.artifact?.key) continue;
    if (card.card_type === 'flowchart' || (card.card_type === 'artifact' && card.artifact.type === FLOWCHART_TYPE)) return card.artifact;
  }
  return null;
}
