// 个人设置（设计稿 me）的纯逻辑：分区参数、账号卡、改密规则（A-06）、偏好（A-08）、我的模型配置（A-09～A-11）。
// mock（routes-me.js）也用这里的改密规则与偏好合并，页面和 mock 说的是同一套口径。
import { passwordRules } from '../login/passwordRules.js';

// ---------- 分区 ----------
export const SECTIONS = [
  { key: 'acct', title: '账号', icon: 'user' },
  { key: 'look', title: '外观', icon: 'sun' },
  { key: 'noti', title: '通知', icon: 'bell' },
  { key: 'model', title: '我的模型配置', icon: 'key' },
];
// ?section=acct|look|noti|model；也认几个顺手的别名，认不出的落到账号
const SECTION_ALIAS = { account: 'acct', password: 'acct', appearance: 'look', theme: 'look', notifications: 'noti', notification: 'noti', models: 'model' };
export function parseSection(raw) {
  const k = String(raw || '').trim().toLowerCase();
  if (SECTIONS.some((s) => s.key === k)) return k;
  return SECTION_ALIAS[k] || 'acct';
}

// ---------- 账号 ----------
export const isLocalMode = (user, config) => user?.auth_source === 'local' || config?.auth?.enabled === false;

export function fmtTokensShort(n) {
  if (n == null) return '—';
  if (n >= 1e6) return `${Number((n / 1e6).toFixed(1))}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}
const SYMBOL = { CNY: '¥', USD: '$' };
export function fmtMoney(m) {
  if (!m || m.amount == null) return '—';
  const v = Number(m.amount).toFixed(m.amount < 100 ? 2 : 0).replace(/\.00$/, '');
  return SYMBOL[m.currency] ? `${SYMBOL[m.currency]}${v}` : `${v} ${m.currency || ''}`.trim();
}
// 本月用量：大字是输入 + 输出 tokens 合计，悬浮看拆分和费用
export function usageView(usage) {
  if (!usage) return { text: '—', title: '' };
  const total = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  const n = (x) => (x == null ? '—' : x.toLocaleString('en-US'));
  return {
    text: fmtTokensShort(total),
    title: `本月 tokens：输入 ${n(usage.input_tokens)} · 输出 ${n(usage.output_tokens)}；费用 ${fmtMoney(usage.cost)}`,
  };
}
const pad = (x) => String(x).padStart(2, '0');
export function fmtDateTime(iso) {
  const t = new Date(iso);
  if (!iso || Number.isNaN(t.getTime())) return '';
  return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`;
}
// 上次登录：B2 第 5 条，服务重启后为 null，这时不显示
export function lastLoginText(last) {
  if (!last?.at) return '';
  const at = fmtDateTime(last.at);
  if (!at) return '';
  return `上次登录 ${at}${last.ip ? ` · ${last.ip}` : ''}`;
}
export function profileChips(user, local) {
  const role = user?.role_label || { user: '建模者', admin: '管理员', ops: '运维' }[user?.role] || '建模者';
  return [
    { text: role, tone: 'blue', icon: 'user' },
    local ? { text: '本地模式', tone: 'gray', icon: 'server' } : { text: user?.auth_source || 'auth-server', tone: 'gray', icon: 'shield' },
  ];
}
export const avatarText = (user) => [...(user?.display_name || user?.username || '我')][0];

// ---------- 改密（A-06，规则同 A-03，另加不能与旧密码相同） ----------
// 设计稿那排实时打勾的 chip；其余规则（上限、首尾空格、与旧密码相同）不满足时在 chip 下面单独说
export const RULE_CHIPS = [['len', '8 位以上'], ['classes', '三类字符'], ['contains_username', '不含用户名'], ['common', '不是常见密码']];
export const RULE_TEXT = {
  len: '至少 8 位',
  max: '不能超过 128 位',
  classes: '大写、小写、数字、符号中至少三类',
  contains_username: '不能包含用户名',
  common: '这是常见密码，换一个',
  edge_space: '首尾不能有空格',
  same_as_old: '不能与旧密码相同',
};
const RULE_ORDER = ['len', 'max', 'classes', 'contains_username', 'common', 'edge_space', 'same_as_old'];

// 逐条判定：true 表示满足；键与 weak_password 的 failed_rules 同名
export function changePasswordRules(username, oldPassword, next) {
  return { ...passwordRules(username || '', next || ''), same_as_old: !oldPassword || next !== oldPassword };
}
export function failedRules(username, oldPassword, next) {
  const r = changePasswordRules(username, oldPassword, next);
  return RULE_ORDER.filter((k) => !r[k]);
}

// 表单状态：本地判定并上服务端退回的 failed_rules（用户改了新密码就清掉）
export function passwordFormView({ old = '', next = '', confirm = '' }, username, serverFailed = []) {
  const failed = next ? [...new Set([...failedRules(username, old, next), ...serverFailed])] : [];
  const chips = RULE_CHIPS.map(([k, t]) => ({ key: k, text: t, state: !next ? 'idle' : failed.includes(k) ? 'fail' : 'ok' }));
  const chipKeys = RULE_CHIPS.map(([k]) => k);
  const extra = failed.filter((k) => !chipKeys.includes(k)).map((k) => RULE_TEXT[k]);
  const mismatch = !!confirm && confirm !== next;
  const canSubmit = !!old && !!next && !!confirm && !failed.length && !mismatch;
  return { chips, extra, mismatch, failed, canSubmit };
}

// 改密失败落到哪个字段、说什么
export function passwordError(err) {
  const code = err?.code;
  if (code === 'old_password_incorrect') return { field: 'old', text: '当前密码不正确' };
  if (code === 'weak_password') return { field: 'next', text: '新密码不满足规则', failed: err.details?.failed_rules || [] };
  if (code === 'forbidden' && err.details?.reason === 'local_mode') return { field: null, text: '本地模式没有账号密码，不能修改' };
  if (code === 'user_admin_unavailable') return { field: null, text: '账号服务没有配置管理令牌，暂时不能修改密码，请联系管理员' };
  return null;
}

// ---------- 偏好（A-08） ----------
export const DEFAULT_PREFS = {
  theme: 'light',
  notifications: {
    system: true,
    l2_step_done: true, l2_needs_input: true, l2_error: true,
    l4_finished: true, l4_new_suspension: false, l4_error_or_stopped: true,
  },
};
// 服务端少给字段、给了不认识的取值时按默认补齐；多出来的字段原样保留（PUT 是整体替换，不能丢）
export function normalizePrefs(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const n = r.notifications && typeof r.notifications === 'object' ? r.notifications : {};
  const notifications = { ...n };
  Object.entries(DEFAULT_PREFS.notifications).forEach(([k, v]) => { notifications[k] = typeof n[k] === 'boolean' ? n[k] : v; });
  return { ...r, theme: r.theme === 'dark' ? 'dark' : 'light', notifications };
}
// 改一项，返回新对象：path 如 'theme'、'notifications.l2_error'
export function withPref(prefs, path, value) {
  const [a, b] = path.split('.');
  const base = normalizePrefs(prefs);
  if (!b) return { ...base, [a]: value };
  return { ...base, [a]: { ...base[a], [b]: value } };
}

export const NOTI_GROUPS = [
  {
    title: '陪伴建模（L2）',
    items: [
      ['l2_step_done', '一步跑完', '例如「操作提取完成，等你确认生效」'],
      ['l2_needs_input', '需要你回答问题或确认操作', '决策卡、确认卡、走查分支选择'],
      ['l2_error', '构建出错', '附带友好提示；原始错误进运维页'],
    ],
  },
  {
    title: '自主构建（L4）',
    items: [
      ['l4_finished', '运行完成', '附带报告摘要：完成几片、几个自动决策待复核'],
      ['l4_new_suspension', '新增挂起问题', 'L4 不会停下来，问题先记着，你可以晚点处理'],
      ['l4_error_or_stopped', '运行出错或在安全点停止', '预算用完、连续失败、手动暂停'],
    ],
  },
];
// 通知类型（契约 4.4）→ 偏好开关；表里没有的类型只看总开关。外壳收到用户流 EU-01、页面不在前台时用它决定弹不弹系统通知
export const KIND_PREF = {
  step_done: 'l2_step_done',
  needs_input: 'l2_needs_input',
  session_error: 'l2_error',
  autobuild_finished: 'l4_finished',
  autobuild_suspension: 'l4_new_suspension',
  autobuild_stopped: 'l4_error_or_stopped',
};
export function systemNotifyAllowed(prefs, kind, permission) {
  if (permission !== 'granted') return false;
  const n = normalizePrefs(prefs).notifications;
  if (!n.system) return false;
  const k = KIND_PREF[kind];
  return k ? !!n[k] : true;
}

// 浏览器通知权限 → 总开关旁边的说明；granted 时不说
export function permissionNote(permission, systemOn) {
  if (permission === 'unsupported') return { tone: 'gray', text: '这个浏览器不支持系统通知，只能靠标签页标题闪烁提醒你。', action: null };
  if (permission === 'denied') return { tone: 'amber', text: '浏览器已禁止本站发通知。要在浏览器的网站设置里允许通知，再回来打开。', action: null };
  if (permission === 'default' && systemOn) return { tone: 'amber', text: '这个浏览器还没允许本站发通知，系统通知暂时弹不出来。', action: '允许通知' };
  return null;
}

// 预览卡里的示例（合成场景）
export const NOTI_PREVIEW = [
  { when: '现在', title: 'L4 运行完成 · 门店补货单编制', body: '5 片里 4 片完成、1 片部分完成；17 个自动决策待复核', pref: 'l4_finished' },
  { when: '3 分钟前', title: '需要你决定 · 超量与审批提示', body: '标准上限从哪里来？4 个选项，推荐 A', pref: 'l2_needs_input' },
];

// ---------- 我的模型配置（A-09～A-11） ----------
const ENGINE_FALLBACK = { claude: { label: '稳定引擎', channel: 'stable' }, jiuwen: { label: '内测引擎', channel: 'beta' } };
// 两个引擎各自对接的接口类型：填个人网关时要知道是哪一种
const PROTOCOL = { claude: 'Anthropic 兼容接口', jiuwen: 'OpenAI 兼容接口' };

// 一张卡一个引擎：以 A-09 的 engines 为准，按 C-06 的顺序排，名字只用 C-06 的 label
export function engineCards(settings, engines) {
  const items = engines?.items || [];
  const ids = Object.keys(settings?.engines || {});
  const order = [...items.map((e) => e.id).filter((id) => ids.includes(id)), ...ids.filter((id) => !items.some((e) => e.id === id))];
  return order.map((id) => {
    const e = items.find((x) => x.id === id) || { id, ...(ENGINE_FALLBACK[id] || { label: '引擎', channel: 'beta' }), installed: true };
    const stable = e.channel === 'stable';
    const title = PROTOCOL[id] ? `${e.label} · ${PROTOCOL[id]}` : e.label;
    const sub = e.installed === false ? (e.unavailable_reason || '当前部署未安装') : stable ? '功能最完整' : '内测中';
    return { id, title, sub, icon: stable ? 'cap' : 'node', cfg: settings.engines[id] };
  });
}

export function sourceChip(source) {
  if (source === 'personal') return { text: '个人 Key', tone: 'blue', icon: 'key' };
  if (source === 'deployment') return { text: '部署默认', tone: 'gray', icon: null };
  return { text: '管理员配置', tone: 'gray', icon: null };
}

export function adminText(cfg) {
  const s = cfg?.admin_summary;
  if (!s) return '管理员还没有配置，会按部署默认配置调用';
  return `管理员配置：${s.gateway_host || '部署默认网关'} · ${s.model || '网关默认模型'}`;
}
export function sourceOptions(cfg) {
  return [
    { key: 'deployment', title: '使用部署默认配置', desc: '部署机 .env 里的网关和模型' },
    { key: 'admin', title: '使用管理员配置', desc: adminText(cfg) },
    { key: 'personal', title: '使用个人 Key', desc: '用你自己的 Key 计费，你的项目都按它发消息' },
  ];
}
const fallbackOf = (cfg) => (cfg?.admin_summary ? '管理员配置' : '部署默认');
export function placeholders(cfg) {
  return {
    api_key: cfg?.api_key_set ? 'sk-•••• •••• ••••' : '粘贴你的 API Key',
    base_url: `留空沿用${fallbackOf(cfg)}的网关地址`,
    model: `留空沿用${fallbackOf(cfg)}的模型 · ${cfg?.admin_summary?.model || '网关默认模型'}`,
  };
}

// 上下文参数是运行时实际值（B2 第 6 条）：个人 Key 只换网关、Key 和模型，上下文仍按管理员配置
export function contextText(ctx) {
  if (!ctx || ctx.window == null) return '';
  const parts = [`上下文 ${fmtTokensShort(ctx.window)}`];
  if (ctx.max_output != null) parts.push(`输出上限 ${fmtTokensShort(ctx.max_output)}`);
  if (ctx.compact_at_pct != null) parts.push(`用到 ${ctx.compact_at_pct}% 时压缩`);
  return parts.join(' · ');
}

export function modelForm(cfg) {
  return { source: cfg?.source || 'admin', api_key: '', base_url: cfg?.base_url || '', model: cfg?.model || '' };
}
export function modelDirty(cfg, form) {
  const f0 = modelForm(cfg);
  if (form.source !== f0.source) return true;
  if (form.source !== 'personal') return false;
  return !!form.api_key.trim() || form.base_url.trim() !== f0.base_url || form.model.trim() !== f0.model;
}
export function modelProblems(cfg, form) {
  const out = {};
  if (form.source !== 'personal') return out;
  if (!cfg?.api_key_set && !form.api_key.trim()) out.api_key = '第一次使用个人 Key，要先填 API Key';
  const url = form.base_url.trim();
  if (url && !/^https?:\/\/[^\s/]+/i.test(url)) out.base_url = '网关地址要以 http:// 或 https:// 开头';
  return out;
}
// 表单 → A-10 请求体：Key 留空不传（保持不变）；网关、模型只在改过时传（不传表示沿用已存值，传空串表示清掉）；
// 切回管理员配置或部署默认时只传 source（服务端会清掉个人配置，B2 第 7 条）
export function modelRequest(cfg, form) {
  if (form.source !== 'personal') return { source: form.source };
  const f0 = modelForm(cfg);
  const body = { source: 'personal' };
  const key = form.api_key.trim();
  if (key) body.api_key = key;
  if (form.base_url.trim() !== f0.base_url) body.base_url = form.base_url.trim();
  if (form.model.trim() !== f0.model) body.model = form.model.trim();
  return body;
}
export const canClear = (cfg) => !!cfg && (cfg.source === 'personal' || !!cfg.api_key_set || !!cfg.base_url || !!cfg.model);
export function clearedToast(cfg) {
  return cfg?.source === 'deployment' ? '已清除个人配置，改用部署默认配置' : '已清除个人配置，改用管理员配置';
}
