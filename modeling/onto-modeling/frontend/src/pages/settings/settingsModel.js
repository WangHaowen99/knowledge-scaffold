// 项目设置页的纯逻辑：分区与地址参数、表单与校验、自主构建策略、能力与插件的行、MCP 的展示
import { isEvidenceMcp } from '../../state/evidence.js';

// ---------- 分区 ----------
// 地址参数 section 取这里的 id
export const SECTIONS = [
  { id: 'basic', title: '基本信息', icon: 'filetext' },
  { id: 'engine', title: '引擎与模型', icon: 'cpu' },
  { id: 'build-mode', title: '构建方式', icon: 'rocket' },
  { id: 'autobuild', title: '自主构建策略', icon: 'sliders' },
  { id: 'plugins', title: '能力与插件', icon: 'puzzle' },
  { id: 'mcp-servers', title: '助手可用的 MCP', icon: 'plug' },
  { id: 'mcp', title: '对外 MCP 接口', icon: 'link' },
  { id: 'danger', title: '危险操作', icon: 'alert' },
];
const ALIASES = { build: 'build-mode', packs: 'plugins', mcpin: 'mcp-servers', mcpout: 'mcp' };

// 返回 { nav: 左侧导航高亮哪一项, anchor: 滚到哪个元素（data-sec） }；不认识的参数返回 null
export function sectionTarget(param) {
  if (!param) return null;
  const id = ALIASES[param] || param;
  return SECTIONS.some((s) => s.id === id) ? { nav: id, anchor: id } : null;
}

// 滚动时左侧导航跟着走：顶部越过分区标题 80px 以内算进入该分区；滚到底时算最后一个
export function activeSection(tops, scrollTop, atBottom = false) {
  if (!tops.length) return null;
  if (atBottom) return tops[tops.length - 1][0];
  let cur = tops[0][0];
  tops.forEach(([id, top]) => { if (top - 80 <= scrollTop) cur = id; });
  return cur;
}

// ---------- 基本信息 ----------
const pad = (n) => String(n).padStart(2, '0');
export const ymd = (t) => { const d = new Date(t); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
export const md = (t) => { const d = new Date(t); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
export const mdhm = (t) => { const d = new Date(t); return `${md(t)} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };

const CREATED_FROM = { wizard: '来自新建向导', import: '来自导入', clone: '克隆而来', example: '示例项目' };
export function createdText(meta) {
  if (!meta?.created_at) return '—';
  return [ymd(meta.created_at), CREATED_FROM[meta.created_from]].filter(Boolean).join(' · ');
}

// P-05 只带改了的字段
export function basicPatch(meta, form) {
  const out = {};
  const name = (form.name || '').trim();
  const goal = (form.goal || '').trim();
  if (name !== (meta?.name || '')) out.name = name;
  if (goal !== (meta?.goal || '').trim()) out.goal = goal;
  return out;
}

// P-03 的结果 → 名称提示（null 表示没问题）
export function nameProblem(check, name) {
  const n = (name || '').trim();
  if (!n) return '请输入项目名称';
  if (n.length > 200) return '项目名称不能超过 200 个字符';
  if (check?.reason === 'conflict') {
    const alt = check.conflict?.candidates?.[0];
    return `你已经有一个叫「${n}」的项目${alt ? `，可以改成「${alt}」` : '，换个名字或加上日期'}`;
  }
  return null;
}

// ---------- 引擎与模型 ----------
const RESUME_TEXT = { native: '功能最完整', replay: '轻量' };
export function engineCards(engines, current) {
  const items = engines?.items || [];
  return items.map((e) => ({
    id: e.id,
    label: e.label,
    icon: e.channel === 'beta' ? 'cpu' : 'terminal',
    badge: e.channel !== 'beta' && e.id === engines.default ? ['推荐', 'blue'] : null,
    desc: RESUME_TEXT[e.resume] || '',
    resume: e.resume,
    installed: e.installed !== false,
    reason: e.installed === false ? (e.unavailable_reason || '当前部署未安装') : null,
    on: e.id === current,
  }));
}

// 项目里显示的模型名（新建向导第 4 步、项目设置「引擎与模型」同一行）：A-09 的个人配置，其次管理员配置
export const modelDisplayName = (cfg) => cfg?.model || cfg?.admin_summary?.model || '使用我的模型配置';

export function modelSourceText(source, cfg) {
  if (source === 'personal') return `使用我的模型配置${cfg?.model ? ` · ${cfg.model}` : ''}`;
  if (source === 'admin') return `使用管理员配置${cfg?.admin_summary?.model ? ` · ${cfg.admin_summary.model}` : ''}`;
  return '使用部署默认配置';
}

// ---------- 自主构建策略 ----------
export const RECOMMENDED = { confidence_threshold: 0.8, below_threshold: 'conservative_assumption', max_fix_rounds: 2 };
const POLICY_FIELDS = ['scope', 'production_collection', 'advanced'];

// 可编辑的那部分（深拷贝），AB-02 按它提交
export function policyForm(policy) {
  const out = {};
  POLICY_FIELDS.forEach((k) => { out[k] = policy?.[k] == null ? null : JSON.parse(JSON.stringify(policy[k])); });
  return out;
}
export function policyDirty(policy, form) {
  return JSON.stringify(policyForm(policy)) !== JSON.stringify(policyForm(form));
}
export function policyBody(form, revision) {
  return { ...policyForm(form), base_revision: revision };
}

// 生产库授权的时间窗口：「22:00 – 06:00」
const HM = /^([01]\d|2[0-3]):[0-5]\d$/;
export const windowText = (w) => (w ? `${w.start} – ${w.end}` : '');
export function parseWindow(text) {
  const t = (text || '').trim();
  if (!t) return { value: null };
  const parts = t.split(/\s*[–—~-]\s*/);
  if (parts.length !== 2 || !HM.test(parts[0]) || !HM.test(parts[1])) return { error: '写成「22:00 – 06:00」这样的起止时间' };
  return { value: { start: parts[0], end: parts[1] } };
}
export const scopeText = (arr) => (arr || []).join('、');
export const parseScope = (text) => (text || '').split(/[、,，;；\s]+/).map((x) => x.trim()).filter(Boolean);

const BELOW_TEXT = { conservative_assumption: '拿不准时先按最稳妥的做法往下做', suspend: '拿不准时先跳过，留给你来定' };
const shortSource = (s) => (s || '').replace(/^\d+\s*/, '');
export function advancedSummary(adv) {
  if (!adv) return '';
  return [
    `阈值 ${Number(adv.confidence_threshold).toFixed(2)}`,
    BELOW_TEXT[adv.below_threshold] || '',
    adv.source_priority?.length ? `冲突时${shortSource(adv.source_priority[0])}优先` : '',
    `修复最多 ${adv.max_fix_rounds} 轮`,
  ].filter(Boolean).join(' · ');
}
export function isRecommended(adv) {
  if (!adv) return true;
  return Object.entries(RECOMMENDED).every(([k, v]) => adv[k] === v) && !adv.must_suspend_kinds?.length;
}

// 阈值滑块：0.5～1.0，步长 0.05
export const thresholdPos = (v) => Math.max(0, Math.min(100, Math.round(((v - 0.5) / 0.5) * 10000) / 100));
export function thresholdAt(pct) {
  const v = 0.5 + (Math.max(0, Math.min(100, pct)) / 100) * 0.5;
  return Math.round(v * 20) / 20;
}

export function moveItem(list, from, to) {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const out = [...list];
  const [x] = out.splice(from, 1);
  out.splice(to, 0, x);
  return out;
}

// 「上次运行 #3 · 11-01 · 1 小时 52 分 · 待复核」
export function lastRunText(last) {
  if (!last) return '还没有运行过自主构建';
  const min = Math.round((last.duration_ms || 0) / 60000);
  const dur = min >= 60 ? `${Math.floor(min / 60)} 小时${min % 60 ? ` ${min % 60} 分` : ''}` : `${min} 分`;
  return [`上次运行 #${last.run_no}`, last.ended_at ? md(last.ended_at) : null, dur, last.reviewed ? '已复核' : '待复核'].filter(Boolean).join(' · ');
}

const RUN_STATE = { queued: '排队中', running: '运行中', pausing: '正在暂停', paused: '已暂停', stopping: '正在停下', converting: '正在转为陪伴' };
export const runStateText = (run) => (run ? `运行 #${run.run_no} ${RUN_STATE[run.state] || '运行中'}` : '');

// 开始前的检查：返回不能开始的原因（null 表示可以）；还有没有要做的由开始前预览（AB-03）判定
export function startBlocker({ archived, active }) {
  if (archived) return '项目已归档，不能开始自主构建';
  if (active) return `${runStateText(active)}，结束后才能开始下一次`;
  return null;
}

// 保存前：把生产库授权里正在编辑的时间窗口、采集范围文字并进表单；返回 { form, problems }
export function finalizePolicy(form, drafts = {}) {
  const problems = {};
  const rows = (form?.production_collection || []).map((row) => {
    const d = drafts[row.source_id];
    if (!d) return row;
    const w = d.window != null ? parseWindow(d.window) : { value: row.window };
    if (w.error && row.allowed) problems[`window:${row.source_id}`] = w.error;
    return { ...row, window: w.error ? row.window : w.value, scope: d.scope != null ? parseScope(d.scope) : row.scope };
  });
  return { form: { ...form, production_collection: rows }, problems };
}

// ---------- 能力与插件 ----------
export const CATEGORY_LABEL = { capability: '能力包', industry: '行业包', system: '系统知识包', provider: '多源连接器', node: '能力包', exporter: '导出器' };

// 「界面」列：左栏页面 / 资料页子页 / 内核 / 没有页面时「—」
export function uiCell(ui) {
  if (!ui || ui.kind === 'none') return { kind: 'none', text: '—' };
  if (ui.kind === 'kernel') return { kind: 'kernel', text: (ui.note || '内核').replace(/^内核[:：]\s*/, '内核：') };
  if (ui.kind === 'page') return { kind: 'page', text: `左栏页面：${ui.pages.map((p) => p.title).join('、')}` };
  if (ui.kind === 'subpages') {
    const parent = ui.subpages[0]?.parent === 'resources' ? '资料页' : '页面';
    return { kind: 'subpages', text: `${parent} ${ui.subpages.length} 个子页`, pop: ui.subpages.map((s) => s.title).join('、') };
  }
  return { kind: 'none', text: '—' };
}

const PENDING_POP = '下一条消息前切换';
export function statusChip(p) {
  if (p.status === 'required') return { tone: 'blue', icon: 'lock', text: '必装' };
  if (p.status === 'disabled') return { tone: 'gray', icon: 'ban', text: '已停用' };
  if (p.status === 'pending' && p.pending) {
    const when = p.pending.effective === 'next_autobuild' ? '下次自主构建生效' : p.pending.effective === 'next_safe_point' ? '安全点后生效' : '下一条消息生效';
    const what = p.pending.kind === 'disable' ? '停用 ·' : p.pending.kind === 'enable' ? '启用 ·' : p.pending.to;
    return { tone: 'amber', icon: 'clock', text: `${what} ${when}`, pop: PENDING_POP };
  }
  return { tone: 'teal', icon: 'check', text: '已启用' };
}

// 行内按钮：有升级时「升级」+ 更多（停用）；有回滚时「回滚」「停用」；否则「停用」；必装的没有按钮（状态列已标「必装」）
export function rowActions(p) {
  const a = p.actions || [];
  if (p.status === 'required') return { buttons: [], more: [] };
  if (a.includes('cancel_pending')) return { buttons: [{ op: 'cancel_pending', label: '撤销切换' }], more: [] };
  if (a.includes('enable')) return { buttons: [{ op: 'enable', label: '启用', kind: 'soft' }], more: [] };
  const buttons = [];
  const more = [];
  if (a.includes('upgrade')) buttons.push({ op: 'upgrade', label: '升级', kind: 'soft', icon: 'arrow' });
  if (a.includes('rollback')) buttons.push({ op: 'rollback', label: '回滚', icon: 'undo' });
  if (a.includes('disable')) (a.includes('upgrade') ? more : buttons).push({ op: 'disable', label: '停用', kind: 'ghost' });
  return { buttons, more };
}

export function pendingSummary(changes, items) {
  if (!changes?.length) return '';
  const label = (n) => items?.find((p) => p.name === n)?.label || n;
  const one = (c) => {
    if (c.kind === 'enable') return `启用${label(c.pack)} ${c.to}`;
    if (c.kind === 'disable') return `停用${label(c.pack)}`;
    return `${label(c.pack)} ${c.from} → ${c.to}`;
  };
  return `${changes.length} 个待生效的切换：${changes.map(one).join('、')}`;
}

// 切换后的提示
export function switchToast(op, p, res) {
  const later = res?.effective === 'immediate' ? '' : '，下一条消息生效';
  const page = p.ui?.kind === 'page' ? p.ui.pages.map((x) => `「${x.title}」`).join('') : '';
  switch (op) {
    case 'enable': return `已启用「${p.label}」${later}`;
    case 'upgrade': return `已升级到${p.label} ${res?.pack?.pending?.to || p.latest_available}${later}`;
    case 'rollback': return `已回滚到 ${res?.pack?.pending?.to || p.rollback_to}${later}`;
    case 'disable': return `已停用「${p.label}」${page ? `，左栏的${page}已移除` : ''}${later}`;
    case 'cancel_pending': return `已撤销，保持 ${p.pending?.from || p.locked_version}`;
    default: return '已保存';
  }
}

// ---------- MCP ----------
// 证据源本期不开放：EV-01 读不到时，内置的 evidence 服务器、包贡献的 evidence.* 工具不出现
export const visibleMcp = (items, evidenceOn) => (evidenceOn ? items || [] : (items || []).filter((x) => !isEvidenceMcp(x)));

// 「evidence.search / show / where-used / lineage / diff」；没有工具时「—」
export function toolsText(tools) {
  if (!tools?.length) return '—';
  const prefix = tools[0].includes('.') ? tools[0].slice(0, tools[0].indexOf('.') + 1) : '';
  if (prefix && tools.every((t) => t.startsWith(prefix))) return `${tools[0]}${tools.slice(1).map((t) => ` / ${t.slice(prefix.length)}`).join('')}`;
  if (tools.length <= 2) return tools.join(' / ');
  return `${tools[0]} 等 ${tools.length} 个`;
}
export const SERVER_STATUS = { running: ['运行中', 'teal'], stopped: ['未运行', 'gray'], error: ['出错', 'red'] };
export const TRANSPORT_LABEL = { stdio: 'stdio', http: 'HTTP' };

const ENTITY_ORDER = ['object', 'operation', 'rule', 'metric', 'script'];
const VERB_ORDER = ['list', 'get', 'change_validate', 'change_apply', 'delete'];
// 对外工具按对象 / 操作 / 规则分组，每组按 读 → 校验 → 写入 → 删除 排
export function toolList(toolsets) {
  const all = [...new Set((toolsets || []).flatMap((t) => t.tools || []))];
  const key = (name) => {
    const i = name.indexOf('_');
    const ent = ENTITY_ORDER.indexOf(name.slice(0, i));
    const verb = VERB_ORDER.indexOf(name.slice(i + 1));
    return [ent < 0 ? 99 : ent, verb < 0 ? 99 : verb];
  };
  return all.sort((a, b) => { const [x1, y1] = key(a); const [x2, y2] = key(b); return x1 - x2 || y1 - y2 || a.localeCompare(b); });
}
// 「flow.get、evidence.*（6）」
export function packToolsText(packTools) {
  const groups = new Map();
  (packTools || []).forEach((t) => {
    const p = t.name.includes('.') ? t.name.slice(0, t.name.indexOf('.')) : t.name;
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(t.name);
  });
  return [...groups.entries()].map(([p, list]) => (list.length > 1 ? `${p}.*（${list.length}）` : list[0])).join('、');
}

export const scopeLabel = (s) => (s.startsWith('ontology.change.') ? s.slice('ontology.'.length) : s);
export const scopeTone = (s) => (s === 'ontology.change.apply' ? 'amber' : 'line');

export const TTL_OPTIONS = [['1', '1 天', 86400], ['30', '30 天', 2592000], ['0', '永不过期', null]];
export function tokenProblems(form) {
  const out = {};
  const name = (form.name || '').trim();
  if (!name) out.name = '请填写名称';
  else if (name.length > 120) out.name = '不超过 120 个字';
  if (!form.scopes?.length) out.scopes = '至少选一个';
  return out;
}
export function tokenBody(form) {
  const ttl = TTL_OPTIONS.find((t) => t[0] === form.ttl) || TTL_OPTIONS[1];
  return { name: form.name.trim(), scopes: form.scopes, expires_in_s: ttl[2] };
}
// 新建后默认看哪一份配置：能写就看 Change，只读看 Read
export const defaultConfigTab = (scopes) => ((scopes || []).some((s) => s.startsWith('ontology.change.')) ? 'c' : 'r');

// MCP 地址：主地址取第一个端点（read 工具集）
export const primaryEndpoint = (mcp) => (mcp?.endpoints || [])[0]?.url || '';
