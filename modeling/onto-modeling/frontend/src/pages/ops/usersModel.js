// 用户、审计与回收站（ops-users）的纯逻辑：角色、密码规则、删除用户时的移交、统计卡、
// 审计筛选与分页合并、错误留档的显示、回收站剩余天数、无主项目指定所有者的校验。
// 口径：契约 AD-01～AD-07、O-48～O-54，contract-changes B2 第 13～16、22～24 条，B3b 第 17～19 条。
import { passwordRules } from '../login/passwordRules.js';

const pad = (n) => String(n).padStart(2, '0');
const DAY = 24 * 3600 * 1000;

// ---------- 角色 ----------

export const ROLE_OPTIONS = [
  { value: 'user', label: '建模者', icon: 'user' },
  { value: 'admin', label: '管理员', icon: 'key' },
  { value: 'ops', label: '运维', icon: 'wrench' },
];
export const ROLE_TONE = { user: 'blue', admin: 'indigo', ops: 'amber' };
export const roleLabel = (role) => ROLE_OPTIONS.find((r) => r.value === role)?.label || role || '建模者';

// 改角色菜单：当前角色标「当前」，选它不发请求
export function roleMenu(current) {
  return ROLE_OPTIONS.map((r) => ({ ...r, hint: r.value === current ? '当前' : '' }));
}

// 头像底色：按用户名稳定取 4 种之一
export function avatarIndex(username) {
  return [...(username || '')].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) % 9973, 7) % 4;
}

// ---------- 时间 ----------

// 「刚刚」「3 分钟前」「14:51」「昨天 18:40」「10-28」；没有记录（B2 第 5 条：重启后为 null）给「—」
export function lastSeenText(iso, now = Date.now()) {
  if (!iso) return '—';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '—';
  const diff = now - t.getTime();
  if (diff < 60 * 1000) return '刚刚';
  if (diff < 3600 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
  const n = new Date(now);
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const hm = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
  if (sameDay(t, n)) return hm;
  if (sameDay(t, new Date(now - DAY))) return `昨天 ${hm}`;
  return `${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
}

// 审计、错误的时间列：当天只写时分秒，其余加月日
export function clockText(iso, now = Date.now()) {
  if (!iso) return '—';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '—';
  const n = new Date(now);
  const hms = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  if (t.getFullYear() === n.getFullYear() && t.getMonth() === n.getMonth() && t.getDate() === n.getDate()) return hms;
  return `${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${hms.slice(0, 5)}`;
}

// 回收站删除时间：「10-28 17:20」
export function dateTimeText(iso) {
  if (!iso) return '—';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '—';
  return `${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`;
}

// ---------- 用户管理统计卡（AD-01 stats；B2 第 13 条 new_7d、locked 可能为 null） ----------

export function userStats(stats, total) {
  const s = stats || {};
  const by = s.by_role || {};
  const accounts = s.accounts ?? total ?? null;
  const auth = s.auth || {};
  return [
    {
      key: 'accounts', label: '账号', icon: 'users', value: accounts ?? '—',
      desc: `建模者 ${by.user ?? 0} · 管理员 ${by.admin ?? 0} · 运维 ${by.ops ?? 0}`,
    },
    {
      key: 'new_7d', label: '近 7 天新注册', icon: 'plus', value: s.new_7d ?? '—',
      desc: s.registration_enabled == null ? '注册开关：读不到' : `注册开关：${s.registration_enabled ? '开启' : '关闭'}`,
    },
    {
      key: 'locked', label: '登录失败锁定', icon: 'lock', value: s.locked ?? '—',
      desc: s.locked == null ? '认证服务没有提供锁定统计' : '连续登录失败、暂时被锁定的账号',
    },
    {
      key: 'auth', label: '鉴权', icon: 'key', value: auth.alg || 'RS256',
      desc: auth.issuer_checked === false ? 'auth-server · JWKS 验签' : 'auth-server · JWKS 验签 · 校验颁发方与受众',
    },
  ];
}

// ---------- 重置密码（AD-03；规则同 A-03，与注册共用 passwordRules） ----------

export const PASSWORD_HINT = '至少 8 位，包含大写、小写、数字、符号中至少三类，不能包含用户名';
const RULE_TEXT = {
  len: '至少 8 位',
  max: '不超过 128 位',
  classes: '大写、小写、数字、符号中至少三类',
  contains_username: '不能包含用户名',
  common: '不能是常见密码',
  edge_space: '首尾不能有空格',
  same_as_old: '不能与旧密码相同',
};

// 没通过的规则 → 中文（本地校验和服务端 weak_password 的 failed_rules 共用）
export function ruleTexts(failed) {
  return (failed || []).map((k) => RULE_TEXT[k] || k);
}

export function passwordProblems(username, password) {
  if (!password) return [];
  const r = passwordRules(username || '', password);
  return Object.keys(r).filter((k) => !r[k]);
}

// 重置按钮能不能点、点了发什么
export function resetRequest({ username, password, generate }) {
  if (generate) return { ok: true, body: { generate: true, require_change: false } };
  if (!password) return { ok: false, error: '请填写新密码，或点「生成」由服务端生成' };
  const failed = passwordProblems(username, password);
  if (failed.length) return { ok: false, error: `不满足：${ruleTexts(failed).join('、')}`, failed };
  // require_change：auth-server 不支持（B2 第 14 条），界面不勾、请求里也不带 true
  return { ok: true, body: { password, require_change: false } };
}

// ---------- 删除用户（AD-04、AD-05） ----------

// 能接手项目的人：除了被删的人以外的建模者（契约 transfer_target_invalid：「移交对象不是建模者或不存在」）
export function transferCandidates(users, username) {
  return (users || []).filter((u) => u.username !== username && (u.role || 'user') === 'user');
}

export function defaultTransferTarget(users, username) {
  const list = transferCandidates(users, username);
  return (list.find((u) => u.online) || list[0])?.username || '';
}

export const userOptionLabel = (u) => `${u.display_name || u.username}（${u.username}）`;

// 「移入回收站」选项：回收站在用（O-52 retention_days 有值）才能选；
// B2 后端删除已是永久删除、回收站只剩存量（retention_days 为 null，AD-04 trash 返回 trash_unsupported）
export function trashOption(retentionDays) {
  if (typeof retentionDays === 'number') {
    return { enabled: true, text: `${retentionDays} 天内管理员或运维可以恢复，之后永久删除` };
  }
  return { enabled: false, text: '暂不支持：删除项目已改为直接永久删除，请移交给其他建模者' };
}

export function deleteSub(user, projectCount) {
  const head = `${user.username} · ${roleLabel(user.role)}`;
  if (projectCount == null) return head;
  return projectCount ? `${head} · 名下有 ${projectCount} 个项目` : `${head} · 名下没有项目`;
}

// 提交前的校验：名下没有项目时不用选去向
export function deleteRequest({ projectCount, mode, target, users, username, trashEnabled }) {
  if (!projectCount) return { ok: true, body: {} };
  if (mode === 'trash') {
    if (!trashEnabled) return { ok: false, error: '暂不支持移入回收站，请移交给其他建模者' };
    return { ok: true, body: { projects: 'trash' } };
  }
  if (!target) return { ok: false, error: '请选择接手这些项目的建模者' };
  if (target === username) return { ok: false, error: '不能移交给被删除的用户自己' };
  if (!transferCandidates(users, username).some((u) => u.username === target)) return { ok: false, error: '移交对象不是建模者或不存在' };
  return { ok: true, body: { projects: 'transfer', transfer_to: target } };
}

export function deleteDoneText(result, nameOf = (x) => x) {
  if (result?.transferred) return `已删除用户，${result.transferred} 个项目已移交给${nameOf(result.transfer_to)}`;
  if (result?.trashed) return `已删除用户，${result.trashed} 个项目已移入回收站`;
  return '已删除用户，已写审计';
}

// AD-04 返回 trash_unsupported（B2 第 15 条）：把选项改成不可选并切回移交
export const isTrashUnsupported = (err) => err?.code === 'invalid_argument' && err?.details?.fields?.projects === 'trash_unsupported';

// ---------- 审计日志（O-48、O-49） ----------

// 筛选分段（设计稿七项）；「全部」不带 type
export const AUDIT_FILTERS = [
  ['all', '全部'], ['write', '写请求'], ['mcp', 'MCP 写入'], ['commit', 'agent 提交'],
  ['plugin', '插件安装 / 切换 / 回滚'], ['evidence', '证据采集'], ['ops', '运维操作'],
];

// AuditEntry.type → [表格里的 chip 文字, 配色]（设计稿 opAud 的配色，其余类型按设计系统补）
export const AUDIT_TYPE = {
  write: ['写请求', 'gray'],
  mcp: ['MCP 写入', 'line'],
  commit: ['agent 提交', 'indigo'],
  plugin: ['插件', 'blue'],
  evidence: ['证据采集', 'teal'],
  ops: ['运维操作', 'amber'],
  auth: ['登录认证', 'gray'],
  publish: ['发布', 'blue'],
  changeset: ['变更集', 'indigo'],
  autobuild: ['自主构建', 'indigo'],
  user: ['用户管理', 'amber'],
  contract: ['对外契约', 'line'],
};

export function auditQuery(filter, q, limit) {
  const query = {};
  if (filter && filter !== 'all') query.type = filter;
  if (q && q.trim()) query.q = q.trim();
  if (limit) query.limit = limit;
  return query;
}

// 「加载更多」：按 key 去重追加；第一页（没有 cursor）整体替换
const defaultKey = (x) => x.id ?? x.entry ?? x.username;
export function mergePage(prev, page, { append, key = defaultKey }) {
  const items = append ? [...(prev?.items || [])] : [];
  const seen = new Set(items.map(key));
  (page?.items || []).forEach((x) => {
    const k = key(x);
    if (!seen.has(k)) { seen.add(k); items.push(x); }
  });
  return { items, next_cursor: page?.next_cursor || null, total: page?.total ?? prev?.total ?? null };
}

// 操作者：用户名换成显示名（AD-01 的列表）；system → 系统；agent、mcp:<名> 原样
export function actorLabel(entry, nameOf) {
  const a = entry?.actor || '';
  if (a === 'system') return '系统';
  if (!a) return '—';
  if (entry.actor_kind === 'user' || !entry.actor_kind) return nameOf?.(a) || a;
  return a;
}

// 内容列：只有摘要（路径、key）和提交 trailer，不含项目内容（契约 8.1 #29）
export function auditContent(entry) {
  const head = entry?.summary || [entry?.method, entry?.path].filter(Boolean).join(' ') || entry?.action || '';
  const trailer = entry?.trailer ? Object.entries(entry.trailer).map(([k, v]) => `${k}: ${v}`) : [];
  return [head, ...trailer].filter(Boolean).join(' · ');
}

export function exportName(format, now = Date.now()) {
  const t = new Date(now);
  return `audit-${t.getFullYear()}${pad(t.getMonth() + 1)}${pad(t.getDate())}-${pad(t.getHours())}${pad(t.getMinutes())}.${format}`;
}

// ---------- 错误留档（O-50、O-51） ----------

export const ORIGIN_LABEL = {
  session: '会话', publish: '发布', collection: '采集', assembly: '装配', gate: '关卡', kernel: '内核',
};

// B2 第 23 条：pack、node 为 null，project 可能为 null 只有 project_id
export function packNode(e) {
  const parts = [e?.pack, e?.node].filter(Boolean);
  return parts.length ? parts.join(' · ') : '—';
}

export function errorProject(e) {
  return e?.project?.name || e?.project_id || e?.context?.project_id || '—';
}

export function errorSub(e, now = Date.now()) {
  const t = e?.at ? clockText(e.at, now) : null;
  return [t, errorProject(e) !== '—' ? errorProject(e) : null, e?.pack, e?.node].filter(Boolean).join(' · ');
}

// 抽屉「执行」一行：执行记录、会话（B3b 第 17 条：exec_id 为 null）
export function execText(ctx) {
  const parts = [];
  if (ctx?.exec_id) parts.push(ctx.exec_id);
  if (ctx?.session_id) parts.push(`会话 ${ctx.session_id}`);
  if (ctx?.changeset_id) parts.push(`变更集 ${ctx.changeset_id}`);
  return parts.length ? parts.join(' · ') : '—';
}

// 「显示最近 5 条，共 100 条」
export function pagerText(shown, total) {
  return total != null ? `显示最近 ${shown} 条，共 ${total} 条` : `显示最近 ${shown} 条`;
}

// 链路跳转（O-04 在 ops-obs）
export const traceTarget = (traceId) => (traceId ? { path: '/ops-obs', query: { trace: traceId } } : null);

// ---------- 回收站（O-52～O-54；B2 第 24 条 retention_days、purge_at 可能为 null） ----------

export function daysLeft(purgeAt, now = Date.now()) {
  if (!purgeAt) return null;
  const t = Date.parse(purgeAt);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.ceil((t - now) / DAY));
}

export function purgeText(purgeAt, now = Date.now()) {
  const d = daysLeft(purgeAt, now);
  if (d == null) return '不自动清除';
  return d === 0 ? '今天' : `${d} 天 后`;
}

export function trashIntro(retentionDays) {
  return typeof retentionDays === 'number'
    ? `删除的项目先进回收站，${retentionDays} 天后自动永久删除。`
    : '这里是早先删除的项目，不会自动清除，需要时手动永久删除。';
}

export function trashEmptyText(retentionDays) {
  return typeof retentionDays === 'number' ? `被删除的项目会在这里保留 ${retentionDays} 天。` : '现在删除项目会直接永久删除，不进回收站。';
}

// O-53 恢复：重名时服务端自动改名
export function restoreDoneText(item, restored) {
  const before = item?.project?.name;
  const after = restored?.name;
  return after && before && after !== before ? `已恢复为「${after}」，回到所有者的项目列表` : '已恢复到所有者的项目列表';
}

// ---------- 无主项目与指定所有者（AD-06、AD-07，设计稿没画入口） ----------

export const OWNER_RE = /^[A-Za-z0-9_.-]{1,64}$/;

export function ownerRequest(owner, users) {
  const o = (owner || '').trim();
  if (!o) return { ok: false, error: '请选择新的所有者' };
  if (!OWNER_RE.test(o)) return { ok: false, error: '所有者用户名不合法' };
  if (users && !users.some((u) => u.username === o)) return { ok: false, error: '这个用户不存在' };
  return { ok: true, body: { owner: o } };
}

export function ownerCandidates(users) {
  return (users || []).filter((u) => u.username);
}
