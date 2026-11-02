// 包的界面（ADR-0055）的纯逻辑：产物双击打开什么、官方包编辑器怎么认、界面接口版本够不够、编辑器只读不只读。
// 打开方式按 AR-01 的 view.detail：包自带编辑器（官方包随平台编译 bundled / 上传包 iframe）> 平台专用组件 > 通用详情。
import { isLocked } from '../state/readonly.js';

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

// 版本号与兼容范围：和后端 services/packs/manifest.py 的 Version / satisfies 同一套写法（审查第 4 条，两边用同一组用例对照）
const SEMVER_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;
const SHORT_RE = /^(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$/;

// 版本 → 比较键 [主, 次, 修订, 正式版 1 / 预发布 0, 预发布各段]；写错返回 null（「1.4」即 1.4.0）
function versionKey(text) {
  const t = String(text ?? '').trim();
  const m = SEMVER_RE.exec(t);
  if (m) {
    const pre = m[4] ? m[4].split('.').map((p) => (/^\d+$/.test(p) ? [0, Number(p)] : [1, p])) : [];
    return [Number(m[1]), Number(m[2]), Number(m[3]), pre.length ? 0 : 1, pre];
  }
  const s = SHORT_RE.exec(t);
  return s ? [Number(s[1]), Number(s[2] || 0), 0, 1, []] : null;
}

function cmpPre(a, b) {
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const [ka, va] = a[i];
    const [kb, vb] = b[i];
    if (ka !== kb) return ka < kb ? -1 : 1;
    if (va !== vb) return va < vb ? -1 : 1;
  }
  return a.length === b.length ? 0 : a.length < b.length ? -1 : 1;
}

function cmp(a, b, depth = 5) {
  for (let i = 0; i < Math.min(depth, 4); i += 1) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return depth > 4 ? cmpPre(a[4], b[4]) : 0;
}

const OPS = ['>=', '<=', '>', '<', '=', '^', '~'];

// 界面接口版本是否落在范围里：空格或逗号分隔的比较式全部满足才算（>=、>、<=、<、=、^、~，可省略次版本、修订号）；
// 空串、* 不限；写错按不满足
export function uiApiSatisfies(range, version = UI_API) {
  const spec = String(range ?? '').trim();
  if (!spec || spec === '*') return true;
  const current = versionKey(version);
  if (!current) return false;
  return spec.replace(/,/g, ' ').split(/\s+/).filter(Boolean).every((token) => {
    const op = OPS.find((o) => token.startsWith(o)) || '=';
    const bound = versionKey(token.startsWith(op) ? token.slice(op.length) : token);
    if (!bound) return false;
    const c = cmp(current, bound);
    switch (op) {
      case '>=': return c >= 0;
      case '>': return c > 0;
      case '<=': return c <= 0;
      case '<': return c < 0;
      case '^': {
        const upper = bound[0] ? [bound[0] + 1, 0, 0] : [0, bound[1] + 1, 0];
        return c >= 0 && cmp(current, upper, 3) < 0;
      }
      case '~': return c >= 0 && cmp(current, [bound[0], bound[1] + 1, 0], 3) < 0;
      default: return cmp(current, bound, 4) === 0;
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

/**
 * 打开器的状态：AR-01 还没回来 → loading；读取出错 → error（给错误与重试，不能当成「插件已停用」）；
 * 读到了但没有这个类型 → disabled；其余按 openModeFor。
 */
export function openerState({ types, error, type, bundled = new Set() }) {
  if (error) return { kind: 'error', error };
  if (!types) return { kind: 'loading' };
  return openModeFor(types.find((t) => t.type === type) || null, { bundled });
}

// 产物类型前缀就是包名（AR-01 type 规则）
export const packOfType = (type) => String(type || '').split('.')[0] || '';

/**
 * 包编辑器只读不只读（窄接口 context.get 与 readonly.changed）：按写入范围锁（项目流 scope.updated / CS-06）判断，
 * 和后端 AR-05 回 423 同一口径——锁里有这份产物，或它所在的文件落在锁的路径里（助手正在跑的节点声明的范围）。
 * type：AR-01 的一项（path 形如 `ext/x/notes/{key}.md`）；一个文件多项的格式，键的 `--` 之前是文件键。
 */
export function editorReadonly(scope, type, artifact) {
  if (!artifact?.key) return false;
  const template = String(type?.path || '');
  const fileKey = type?.format === 'md-sections' ? String(artifact.key).split('--')[0] : String(artifact.key);
  const path = template.includes('{key}') ? template.replace('{key}', fileKey) : undefined;
  return isLocked(scope, { type: artifact.type, key: artifact.key, path });
}

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
