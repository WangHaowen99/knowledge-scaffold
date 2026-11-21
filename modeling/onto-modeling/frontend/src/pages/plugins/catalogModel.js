// 插件中心的纯逻辑（设计稿 pages-60-plugins.js 的 PL_CAT、plFoot、plUi、plCard、启用 / 更新弹窗）：
// 筛选与分组、卡脚按 project_status 映射、兼容检查结果到弹窗文字、提示语。页面只负责画。

// 分类：名称、图标、说明（设计稿 PL_CAT）；exporter 契约里有、设计稿没画，有包时才出现。
// 节点包是能力包的特例（用户 2026-11-02 定），归进能力包，不单列
export const PACK_CATEGORIES = {
  capability: ['能力包', 'cube', '决定怎么建模'],
  industry: ['行业包', 'book', '行业知识资产，只有数据'],
  system: ['系统知识包', 'server', '商用系统的表字段字典'],
  provider: ['多源连接器', 'db', '连接器网关里运行，管理员安装'],
  exporter: ['导出器', 'download', '把知识包导出成其他格式'],
};
// 有包时才出现在左栏的分类：导出器设计稿没画；多源连接器在部署了连接器网关时才有（转换器）
export const OPTIONAL_CATEGORIES = ['exporter', 'provider'];
// 卡片图标底色（设计稿 .pl-ico.cap / .ind / .sys / .prov / .exp）
export const PACK_ICON_CLASS = { capability: 'cap', industry: 'ind', system: 'sys', provider: 'prov', exporter: 'exp' };
// 旧数据里的 node 分类按能力包算
export const categoryOf = (p) => (p?.category === 'node' ? 'capability' : p?.category);
// 详情页英雄区的大图标：能力包按包名（与新建向导第 3 步一致），其余按分类
const PACK_ICON = { core: 'cube', trace: 'flask', codegen: 'code', diagram: 'branch' };
const PACK_LABEL = { core: '基础建模', codegen: '代码生成与求解', 'deterministic-execution': '确定性执行' };
export const packIcon = (p) => PACK_ICON[p?.name] || PACK_CATEGORIES[categoryOf(p)]?.[1] || 'puzzle';

// 纯数据包（行业包、系统知识包）现读即生效，更新前先给看更新弹窗
export const DATA_CATEGORIES = ['industry', 'system'];
export const isDataPack = (p) => DATA_CATEGORIES.includes(p?.category);

const INSTALLED = ['required', 'enabled', 'update', 'pending'];
export const isInstalled = (p) => INSTALLED.includes(p?.project_status);

// ---------- 筛选与分组 ----------
// cat：all、分类键、inst（本项目已启用，含待生效）、upd（可更新）；q 按名称、ID、说明（设计稿 data-name）
export function matchCard(p, { cat = 'all', q = '' } = {}) {
  const byCat = cat === 'all' || categoryOf(p) === cat || (cat === 'inst' && isInstalled(p)) || (cat === 'upd' && p.project_status === 'update');
  const s = q.trim().toLowerCase();
  return byCat && (!s || `${p.label} ${p.name} ${p.description || ''}`.toLowerCase().includes(s));
}

/** 按分类分组（设计稿顺序），没有匹配项的分组不出现 */
export function sectionsOf(items, filter) {
  return Object.entries(PACK_CATEGORIES)
    .map(([key, [title, , desc]]) => {
      const all = items.filter((p) => categoryOf(p) === key);
      return { key, title, desc, total: all.length, items: all.filter((p) => matchCard(p, filter)) };
    })
    .filter((s) => s.items.length);
}

/** 左栏分类参数：?cat= 认分类键与 inst、upd，其余回到全部 */
export function catOf(query) {
  const c = query?.cat;
  return c && (PACK_CATEGORIES[c] || c === 'inst' || c === 'upd') ? c : 'all';
}

// 开发 skill（PL-27）按分类下载；全部、已启用、可更新不是分类，导出器没有 skill
const DEV_SKILL_CATEGORIES = ['capability', 'industry', 'system', 'provider'];
export const devSkillOf = (cat) => (DEV_SKILL_CATEGORIES.includes(cat)
  ? { path: `/packs/dev-skills/${cat}`, filename: `onto-plugin-dev-${cat}.zip` }
  : null);

// ---------- 卡片 ----------
// 只给租户包、私有包打标；官方包不标（几乎所有包都是官方）
export function publisherChip(p) {
  const pub = p.publisher || { kind: 'official', label: '官方' };
  if (pub.kind === 'private') return { text: pub.label || '仅自己可见', tone: 'gray' };
  if (pub.kind !== 'tenant') return null;
  const label = pub.label || '';
  return { text: !label ? '租户' : /^租户/.test(label) ? label : `租户 · ${label}`, tone: 'indigo' };
}

const stripNone = (note) => String(note || '').replace(/^无独立页面\s*[·，,]\s*/, '').replace(/^无独立页面$/, '');

/** 界面标记（设计稿 plUi）：内核页面 / 无独立页面 / 带页面：x / 带页面：资料页 n 个子页 */
export function uiChip(ui) {
  if (!ui) return null;
  if (ui.kind === 'kernel') return { text: '内核页面', tone: 'gray', icon: 'layout', pop: String(ui.note || '').replace(/^内核[^：:]*[：:]\s*/, '') || null };
  if (ui.kind === 'none') return { text: '无独立页面', tone: 'gray', icon: null, pop: stripNone(ui.note) || null };
  if (ui.kind === 'subpages') {
    const parent = ui.subpages?.[0]?.parent === 'resources' ? '资料页' : '页面';
    return { text: `带页面：${parent} ${ui.subpages?.length || 0} 个子页`, tone: 'blue', icon: 'folder', pop: ui.note || null };
  }
  if (ui.kind === 'page') {
    const titles = (ui.pages || []).map((x) => x.title);
    return { text: `带页面：${titles.join('、')}`, tone: 'blue', icon: 'layout', pop: `启用后项目左栏「插件」分组出现${titles.map((t) => `「${t}」`).join('')}` };
  }
  return null;
}

const BATCH = { first: ['首版', 'blue'], second: ['第二批', 'gray'], third: ['第三批', 'gray'] };

/**
 * 卡片中间那排标记：租户发布方、带页面的能力包、批次（提供方）。
 * 分类不标（卡片已按分类分区）；官方不标；不带页面不标；可更新看卡脚按钮；后端的元信息 meta 不显示（口径 5）
 */
export function cardTags(p) {
  const tags = [];
  const pub = publisherChip(p);
  if (pub) tags.push(pub);
  const ui = categoryOf(p) === 'capability' ? uiChip(p.ui) : null;
  if (ui && ui.text !== '无独立页面') tags.push(ui);
  const dependencies = Object.entries(p?.requires?.depends || {}).filter(([name]) => name !== 'core');
  if (dependencies.length) tags.push({
    text: `依赖：${dependencies.map(([name]) => PACK_LABEL[name] || name).join('、')}`,
    tone: 'gray',
  });
  const batch = p.category === 'provider' ? BATCH[p.batch] : null;
  if (batch) tags.push({ text: batch[0], tone: batch[1] });
  return { tags };
}

/**
 * 多源连接器的信任（ADR-0104，契约 PackCard.trust）：随镜像交付的（builtin）不标；
 * 上传的标已信任 / 未信任，信任了但网关里没装上标「未装上」（网关不可用时 installed 为 null，按已信任算）
 */
export function trustChip(p) {
  const t = p?.trust;
  if (!t || t.state === 'builtin') return null;
  if (t.state === 'untrusted') return { text: '未信任', tone: 'gray', icon: 'lock' };
  if (t.installed === false) return { text: '未装上', tone: 'amber', icon: 'alert' };
  return { text: '已信任', tone: 'teal', icon: 'shieldok' };
}

/** 卡片上的「包名@版本」：已启用的显示锁定版本 */
export const versionOf = (p) => (isInstalled(p) && p.locked_version ? p.locked_version : p.version || p.latest);

/**
 * 卡脚（设计稿 plFoot）：left 是 chip 或按钮，note 是中间的说明，right 是「详情」或按钮。
 * o.pendingKind 来自 PL-05（待生效的是停用时换说法）。
 */
export function cardFoot(p, o = {}) {
  const chip = (text, tone, icon) => ({ type: 'chip', text, tone, icon: icon || null });
  const detail = { type: 'detail' };
  // 上传的多源连接器：卡脚显示信任状态，信任与撤销在详情页（管理员）
  const trust = trustChip(p);
  if (trust) return { left: chip(trust.text, trust.tone, trust.icon), right: detail };
  switch (p.project_status) {
    case 'required': return { left: chip('必装 · 已启用', 'blue', 'lock'), right: detail };
    case 'enabled': return { left: chip('已启用', 'blue', 'check'), right: detail };
    case 'update':
      return { left: chip(`已启用 ${versionOf(p)}`, 'blue', 'check'), right: { type: 'button', label: `更新到 ${p.latest}`, kind: 'soft', icon: 'refresh', action: 'update' } };
    case 'pending':
      return { left: o.pendingKind === 'disable' ? chip('停用 · 下一条消息生效', 'gray', 'clock') : chip('已启用 · 下一条消息生效', 'blue', 'clock'), right: detail };
    case 'available': return { left: { type: 'button', label: '启用到本项目', kind: 'primary', icon: 'plus', action: 'enable' }, right: detail };
    case 'offline': return { left: chip('离线部署不可用', 'gray', 'ban'), right: { type: 'button', label: '启用', disabled: true, title: '离线部署不可用' } };
    case 'not_applicable': return { left: chip('本项目不适用', 'gray'), note: p.usage_note || null, noteClass: 'xs faint ell grow', right: detail };
    case 'platform_installed': return { left: chip('平台已安装', 'teal', 'check'), note: p.usage_note || null, noteClass: 'xs muted ell grow', right: detail };
    default: return { note: '先建一个项目才能启用', noteClass: 'xs faint ell grow', right: detail };
  }
}

// ---------- 推荐 ----------
/** 推荐一项的按钮：启用 / 更新；本页处理过之后换成 chip */
export function recView(rec, item) {
  if (rec.action === 'enable') return item && isInstalled(item) ? { done: '已启用' } : { label: '启用' };
  return item && item.project_status !== 'update' && isInstalled(item) ? { done: '已更新' } : { label: '更新' };
}

// ---------- 启用、更新弹窗 ----------
/** 兼容检查的行：通过的「页面接口」在不带页面的包上不列（这时只有 5 行） */
export function compatRows(checks, ui) {
  const withPages = ui?.kind === 'page' || ui?.kind === 'subpages';
  return (checks || []).filter((c) => c.id !== 'page_api' || !c.ok || withPages);
}

/**
 * 启用弹窗的兼容检查（R13）：全部通过时折成一行「兼容检查通过（n 项）」，点开看明细；
 * 有不通过的默认展开，只列不通过的那几行
 */
export function compatView(checks, ui) {
  const rows = compatRows(checks, ui);
  const failed = rows.filter((c) => !c.ok);
  return failed.length
    ? { ok: false, summary: `兼容检查不通过（${failed.length} 项）`, rows: failed, expanded: true }
    : { ok: true, summary: `兼容检查通过（${rows.length} 项）`, rows, expanded: false };
}

/** 「会带来什么」：summary_md 是「- 」开头的几行时画成列表，否则是一段话；没有时按数量拼 */
export function bringsView(brings) {
  const md = String(brings?.summary_md || '').trim();
  if (md) {
    const lines = md.split('\n').map((x) => x.trim()).filter(Boolean);
    if (lines.every((x) => /^[-*]\s+/.test(x))) return { list: lines.map((x) => x.replace(/^[-*]\s+/, '')) };
    return { text: md };
  }
  const parts = [];
  if (brings?.commands) parts.push(`${brings.commands} 个命令`);
  if (brings?.agents) parts.push(`${brings.agents} 个子代理`);
  if (brings?.skills) parts.push(`${brings.skills} 个 skill`);
  if (brings?.nodes?.length) parts.push(`${brings.nodes.length} 个流程节点（${brings.nodes.map((n) => n.title || n.id).join('、')}）`);
  return { text: parts.length ? `${parts.join('、')}。` : '没有新增命令和流程节点。' };
}

/** 「界面」那一行的说明（设计稿：无独立页面用 uiNote，子页用说明，带页面写「启用后左栏出现」） */
export function uiEffectText(effect, ui) {
  if (effect?.text) return effect.text;
  if (ui?.kind === 'page') return `启用后左栏出现${(ui.pages || []).map((x) => `「${x.title}」`).join('')}`;
  if (ui?.kind === 'subpages') return ui.note || '';
  return stripNone(ui?.note) || '能力体现在助手对话和 Studio 里';
}

export function dependencyInstruction(pack, compatibility) {
  if (compatibility?.ok || pack?.name !== 'deterministic-execution') return '';
  const failed = (compatibility?.checks || []).find((check) => check.id === 'depends' && !check.ok);
  return failed ? '请先启用“代码生成与求解”，再启用“确定性执行”。' : '';
}

/** 什么时候生效（契约 PL-03 effective），一行 */
export function effectiveNote(effective) {
  if (effective === 'immediate') return '现读即生效';
  if (effective === 'next_safe_point') return '下一个安全点后生效';
  return '下一条消息生效';
}

/** 更新弹窗「对已有产物的影响」 */
export function impactText(impact) {
  if (impact?.note) return impact.note;
  const n = impact?.needs_review_after_upgrade || 0;
  const a = impact?.affected_artifacts || 0;
  const head = n ? `节点完成判定变了，${n} 个已确认的产物会标为需复核。` : '节点完成判定没有变化，已确认的产物不会被标为需复核。';
  return a ? `${head}用到这个包的 ${a} 个产物会在下次校验时按新内容检查。` : head;
}

/** 暂时不能更新 / 启用：原因（blocked_reason，没有时取没通过的检查）和每项的说明 */
export function blockedView(compat) {
  const failed = (compat?.checks || []).filter((c) => !c.ok);
  return {
    reason: compat?.blocked_reason || failed.map((c) => c.label).join('、') || '兼容检查没有通过',
    details: failed.map((c) => c.detail).filter(Boolean),
  };
}

/** 点「更新到 x」之后走哪条路：兼容检查不过 → 暂时不能更新；纯数据包 → 更新弹窗；其余直接 PL-07 */
export function updateRoute(p, compat) {
  if (!compat?.ok) return 'blocked';
  return isDataPack(p) || compat.effective === 'immediate' ? 'confirm' : 'direct';
}

const later = (res) => (res?.effective === 'immediate' ? '，现读即生效' : '，下一条消息生效');
export function enableToast(p, res) {
  return `已启用 ${p.label}${res?.version_label ? `，保存为版本 ${res.version_label}` : ''}`;
}
export function updateToast(p, to, res, viaDialog) {
  return viaDialog ? `已更新到 ${p.name} ${to}${later(res)}` : `已更新${p.label}到 ${to}${later(res)}`;
}
