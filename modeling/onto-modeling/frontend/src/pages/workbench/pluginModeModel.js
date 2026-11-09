// 工作台插件开发模式（ADR-0087、契约 F17）的纯逻辑：对话类型与切换、插件三步、草稿卡的按钮与文字、文件抽屉的分组
import { PACK_CATEGORIES, categoryOf } from '../plugins/catalogModel.js';

export const isPluginConv = (c) => c?.kind === 'plugin';
const isChatConv = (c) => !!c && (c.kind || 'chat') === 'chat';
const BUSY = ['running', 'connecting', 'queued'];

/** 一类对话里最近更新的那条：S-01 已按最近更新排，updated_at 缺的按列表顺序 */
export function latestConversation(items, pred) {
  let best = null;
  (items || []).forEach((c) => {
    if (!pred(c)) return;
    if (!best || (Date.parse(c.updated_at) || 0) > (Date.parse(best.updated_at) || 0)) best = c;
  });
  return best;
}

/**
 * 打开工作台选哪条对话：地址指定的 > l4-run 页的运行对话 > 引擎在线的非插件对话 > 最近的非插件对话。
 * 插件开发只从模式菜单或链接（?c=）进。
 */
export function pickConversation(items, { preferredId, liveId, preferAutobuild } = {}) {
  const list = items || [];
  const byId = (id) => (id ? list.find((c) => c.id === id) : null);
  const live = byId(liveId);
  return byId(preferredId)
    || (preferAutobuild ? list.find((c) => c.kind === 'autobuild') : null)
    || (live && !isPluginConv(live) ? live : null)
    || latestConversation(list, (c) => !isPluginConv(c))
    || list[0] || null;
}

/**
 * 模式菜单选「插件开发」：这条对话在跑、排队，或自主构建在跑时不切；否则进最近一条插件对话，没有就新建。
 * @returns {{action: 'busy', reason: 'session'|'autobuild'} | {action: 'open', id: string} | {action: 'create'}}
 */
export function planEnterPlugin({ items, sessionState, autobuildRunning }) {
  if (autobuildRunning) return { action: 'busy', reason: 'autobuild' };
  if (BUSY.includes(sessionState)) return { action: 'busy', reason: 'session' };
  const c = latestConversation(items, isPluginConv);
  return c ? { action: 'open', id: c.id } : { action: 'create' };
}

/** 离开插件开发（选陪伴建模、自主构建，或「切回建模试用」）：回最近一条建模对话，没有就新建；这条在跑时不切 */
export function planLeavePlugin({ items, sessionState }) {
  if (BUSY.includes(sessionState)) return { action: 'busy', reason: 'session' };
  const c = latestConversation(items, isChatConv);
  return c ? { action: 'open', id: c.id } : { action: 'create' };
}

export const BUSY_TEXT = { session: '这一轮跑完再切', autobuild: '自主构建在跑，停下后再切' };

/** 会话切换菜单：插件对话里只列插件对话，建模对话里不列插件对话 */
export const menuConversations = (items, plugin) => (items || []).filter((c) => isPluginConv(c) === !!plugin);

// ---------- 插件三步 ----------
const failCount = (pre) => pre?.summary?.fail ?? (pre?.items || []).filter((i) => i.result === 'fail').length;

/**
 * 顶栏「写草稿 — 检查 — 安装」：st 取 ok 完成、fail 没过、pending 进行中、'' 没开始；x 是右侧小标。
 * @param {object|null} draft DraftView（PD-02），没草稿为 null
 */
export function pluginSteps(draft) {
  const steps = [{ id: 'draft', label: '写草稿', st: '' }, { id: 'check', label: '检查', st: '' }, { id: 'install', label: '安装', st: '' }];
  if (!draft) return steps;
  const [write, check, install] = steps;
  write.st = 'ok';
  const pre = draft.precheck?.state;
  if (pre === 'failed') Object.assign(check, { st: 'fail', x: `${failCount(draft.precheck)} 项` });
  else if (pre === 'passed') check.st = 'ok';
  const ins = draft.install?.state;
  if (ins === 'installed') Object.assign(install, { st: 'ok', x: draft.install.enabled_version || null });
  else if (ins === 'outdated') Object.assign(install, { st: 'pending', x: '待更新' });
  else if (pre === 'passed') install.st = 'pending';
  return steps;
}

// ---------- 草稿卡 ----------
/**
 * 草稿卡底部出哪组按钮。只有这份草稿最新的一张卡出按钮。
 * @param {object|null} draft DraftView
 * @param {{latest: boolean, installFailed?: object[]}} o installFailed 是安装时静态检查没过（422）的失败项
 * @returns {'snapshot'|'fix'|'install'|'installed'|'update'}
 *   snapshot 旧卡或还没取到状态，只显示快照；fix 预检没过；install 过了没装；installed 已装且最新；update 装了但草稿改过
 */
export function draftCardMode(draft, { latest, installFailed } = {}) {
  if (!latest || !draft) return 'snapshot';
  if (installFailed?.length || draft.precheck?.state === 'failed') return 'fix';
  if (draft.install?.state === 'installed') return 'installed';
  if (draft.install?.state === 'outdated') return 'update';
  return 'install';
}

export const categoryLabel = (draft) => PACK_CATEGORIES[categoryOf(draft) || 'capability']?.[0] || '插件';

function shortTime(iso, now = Date.now()) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  const hm = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
  return new Date(now).toDateString() === t.toDateString() ? hm : `${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${hm}`;
}

/**
 * 草稿卡头部：眉题、名称、名称旁的标、副标题。
 * @param {object|null} draft DraftView；旧卡、没取到时按卡片快照（payload 的 name、version、changed_files）
 * @param {object} card plugin_draft 卡的 payload
 * @param {string} mode draftCardMode 的结果
 */
export function draftCardHead(draft, card, mode, now) {
  const cat = categoryLabel(draft);
  const title = draft?.label || card?.name || draft?.name || '';
  if (mode === 'snapshot') {
    const n = (card?.changed_files || []).length;
    return { eyebrow: `${cat} · 草稿`, title, chip: card?.version ? { tone: 'line', text: card.version } : null, sub: n ? `改了 ${n} 个文件` : '' };
  }
  const ins = draft.install || {};
  const files = `${draft.file_count ?? (draft.files || []).length} 个文件`;
  const at = shortTime(draft.updated_at, now);
  const updated = at ? `${files} · ${at} 更新` : files;
  if (mode === 'installed') {
    return { eyebrow: cat, title, chip: { tone: 'teal', icon: 'check', text: `已装 ${ins.enabled_version}` }, sub: updated };
  }
  // 没装或改过：标的是现在点安装会用的版本号（已在包仓库里的自动升补丁号）
  const ver = ins.next_version || draft.version;
  const chip = ver ? { tone: 'line', text: ver } : null;
  if (mode === 'update' || ins.state === 'outdated') {
    return { eyebrow: `${cat} · 草稿`, title, chip, sub: `已装 ${ins.enabled_version} · 改了 ${(ins.changes || []).length} 个文件` };
  }
  return { eyebrow: `${cat} · 草稿`, title, chip, sub: updated };
}

/** 流程行节点后面的话：「排在「校验」后面，没过挡住发布」 */
export function nodePlacementText(n) {
  const where = n.after_title ? `排在「${n.after_title}」后面` : n.before_title ? `排在「${n.before_title}」前面` : '';
  return [where, n.blocks_publish ? '没过挡住发布' : ''].filter(Boolean).join('，');
}

const KNOWLEDGE_UNITS = [['concepts', '个概念'], ['checks', '条检查'], ['standards', '条标准'], ['files', '个文件']];
/** 行业包、系统知识包的「知识」行：非零的条数 */
export const knowledgeTags = (k) => (k ? KNOWLEDGE_UNITS.filter(([key]) => k[key] > 0).map(([key, unit]) => `${k[key]} ${unit}`) : []);

/** 预检标：通过 N 项 / N 项没过 */
export function precheckChip(pre) {
  if (!pre) return null;
  if (pre.state === 'failed') return { tone: 'red', icon: 'x', text: `${failCount(pre)} 项没过` };
  return { tone: 'teal', icon: 'check', text: `${pre.summary?.pass ?? (pre.items || []).filter((i) => i.result === 'pass').length} 项通过` };
}

/** 预检项分成没过的（逐条列出）和其余的（收起） */
export function splitPrecheck(items) {
  const failed = (items || []).filter((i) => i.result === 'fail');
  return { failed, rest: (items || []).filter((i) => i.result !== 'fail') };
}

/** 「让助手修复」发给这条对话的话 */
export function fixMessage(items) {
  const parts = (items || []).map((i) => (i.detail ? `${i.label}：${i.detail}` : i.label));
  return `预检没过：${parts.join('；')}。请修好，再跑一遍本地检查。`;
}

/** 时间线里每份草稿最新的一张卡：名称 → 卡片键 */
export function latestDraftCards(state) {
  const out = {};
  (state?.order || []).forEach((k) => {
    const it = state.items[k];
    const c = it?.kind === 'card' ? it.card : null;
    if (c && isDraftCard(c) && c.name) out[c.name] = k;
  });
  return out;
}

/** plugin_draft 卡：契约 F17 写的是 payload.kind，其余卡片按 card_type 分（EC-40），两种都认 */
export const isDraftCard = (c) => c?.card_type === 'plugin_draft' || c?.kind === 'plugin_draft';

// ---------- 文件抽屉 ----------
const ROOT_FIRST = ['manifest.yaml'];
const ROOT_LAST = ['checksums.sha256'];
const rootRank = (name) => (ROOT_FIRST.includes(name) ? 0 : ROOT_LAST.includes(name) ? 2 : 1);

/** 文件按目录分组：根下文件在前（清单最前、校验和最后），其余目录按名称 */
export function groupFiles(files) {
  const groups = new Map();
  (files || []).forEach((f) => {
    const i = f.path.lastIndexOf('/');
    const dir = i >= 0 ? f.path.slice(0, i + 1) : '';
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push({ ...f, name: f.path.slice(i + 1) });
  });
  const root = (groups.get('') || []).map((f, i) => [f, i]).sort((a, b) => rootRank(a[0].name) - rootRank(b[0].name) || a[1] - b[1]).map(([f]) => f);
  const dirs = [...groups.keys()].filter((d) => d).sort();
  return [...(root.length ? [{ dir: '', files: root }] : []), ...dirs.map((dir) => ({ dir, files: groups.get(dir) }))];
}

/** 抽屉打开时选哪个文件：看改动时选有 diff 的那个，否则流程声明、清单、第一个 */
export function defaultFile(draft, changes) {
  const paths = (draft?.files || []).map((f) => f.path);
  if (changes) {
    const d = draft?.install?.diff?.path;
    return d || draft?.install?.changes?.[0]?.path || paths[0] || null;
  }
  return ['flow.yaml', 'manifest.yaml'].find((p) => paths.includes(p)) || paths[0] || null;
}

/** 网页上能不能改：校验和由平台写，二进制和超长的没有内容 */
export const canEditFile = (path, file) => !!file && path !== 'checksums.sha256' && !path.endsWith('/checksums.sha256') && !file.binary && file.content != null;

const DIFF_KIND = { '+': 'add', '-': 'del' };
/** install.diff 的行 → 代码区的行：kind 取 add / del / ''，mark 是行号位置的符号 */
export const diffRows = (diff) => (diff?.lines || []).map((l) => ({ kind: DIFF_KIND[l.op] || '', mark: l.op === '+' ? '+' : l.op === '-' ? '−' : '', text: l.text }));

export const CHANGE_LABEL = { added: '新增', modified: '改动', deleted: '删除' };

// ---------- 从插件开发切回建模试用 ----------
// sessionStorage 记「从哪条插件对话来」，建模对话的建议条据此多一个「回到插件开发」；进插件开发后清掉
const RETURN_KEY = (pid) => `onto.pluginReturn:${pid}`;
export const pluginReturn = {
  get(store, pid) { try { return store?.getItem(RETURN_KEY(pid)) || null; } catch { return null; } },
  set(store, pid, cid) { try { store?.setItem(RETURN_KEY(pid), cid); } catch { /* 存不了就不显示回去的入口 */ } },
  clear(store, pid) { try { store?.removeItem(RETURN_KEY(pid)); } catch { /* 同上 */ } },
};

/** 「回到插件开发」回哪条：记下的那条还在、还是插件对话才算 */
export function returnTarget(items, recorded) {
  const c = recorded ? (items || []).find((x) => x.id === recorded) : null;
  return isPluginConv(c) ? c.id : null;
}

// 起步卡片：点了把一句起头填进输入框（不直接发）
export const START_OPTIONS = [
  { id: 'gate', icon: 'gate', tone: 'node', title: '发布前加一道检查', text: '做一个发布前的检查，没过就挡住发布：' },
  { id: 'step', icon: 'flow', tone: 'cap', title: '加一个建模步骤', text: '加一个建模步骤，排进进度条：' },
  { id: 'cmd', icon: 'cmd', tone: 'cap', title: '加一个命令', text: '加一个对话里随时能用的命令：' },
  { id: 'industry', icon: 'book', tone: 'ind', title: '整理行业知识', text: '把下面的术语和规则整理成行业包：' },
];
