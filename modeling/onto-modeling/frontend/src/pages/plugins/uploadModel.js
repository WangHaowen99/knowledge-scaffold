// 上传与审核（plugin-upload，PL-13～PL-18）与管理员审核申请（AD-08～AD-10）的纯逻辑。
// 检查项、状态、错误码按后端 B3a 的实现口径（contract-changes B3a 第 10～13 条）。
import { errorText, formatBytes } from '../../api/errors.js';

// ---------- 上传 ----------

export const PACK_MAX_BYTES = 200 * 1024 * 1024;
// C-01 limits.pack_upload_bytes，没给时按契约 1.8 的 200 MiB
export const packLimit = (config) => config?.limits?.pack_upload_bytes || PACK_MAX_BYTES;

/** 选中的文件能不能传：只收 .zip，不能是空文件，不超过上限。返回提示或 null */
export function validatePackFile(file, max = PACK_MAX_BYTES) {
  if (!file) return '没有选中文件';
  const name = file.name || '';
  if (!/\.zip$/i.test(name)) return `只支持 .zip 离线包，「${name}」不是 zip`;
  if (!file.size) return `「${name}」是空文件`;
  if (file.size > max) return `「${name}」有 ${formatBytes(file.size)}，超过 ${formatBytes(max)} 上限`;
  return null;
}

const pad = (n) => String(n).padStart(2, '0');
/** 上传时间：今天只写时刻，今年写月-日，更早写日期 */
export function fmtClock(iso, now = Date.now()) {
  const t = iso ? new Date(iso) : null;
  if (!t || Number.isNaN(t.getTime())) return '';
  const n = new Date(now);
  const hm = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
  if (t.toDateString() === n.toDateString()) return hm;
  if (t.getFullYear() === n.getFullYear()) return `${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${hm}`;
  return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
}

/** 检查结果汇总：「9 通过 · 1 警告」，没有的项不写 */
export function summaryText(s) {
  const parts = [];
  if (s?.pass) parts.push(`${s.pass} 通过`);
  if (s?.warn) parts.push(`${s.warn} 警告`);
  if (s?.fail) parts.push(`${s.fail} 失败`);
  return parts.join(' · ') || '没有检查项';
}

// 逐项结果的圆点与标签（设计稿 chkRow：ok / warn / bad）
export const CHECK_RESULT = {
  pass: { cls: 'ok', icon: 'check', label: '通过', tone: 'teal' },
  warn: { cls: 'warn', icon: 'alert', label: '警告', tone: 'amber' },
  fail: { cls: 'bad', icon: 'x', label: '失败', tone: 'red' },
};
export const checkView = (c) => CHECK_RESULT[c?.result] || { cls: 'warn', icon: 'alert', label: c?.result || '未知', tone: 'amber' };

export const uploadTitle = (u) => (u?.name ? `${u.name} ${u.version || ''}`.trim() : u?.filename || '');

const PENDING = ['checking', 'passed', 'failed', 'held'];
export const isPending = (u) => PENDING.includes(u?.state);
export const isPublishable = (u) => !!u && (u.state === 'passed' || u.state === 'held') && !(u.summary?.fail > 0);

/** 上传列表一行的状态：chip 文案与色调（清单、校验和的结果在静态检查里逐项列出，行上不再单标） */
export function uploadStatus(u) {
  const fail = u?.summary?.fail || 0;
  switch (u?.state) {
    case 'checking': return { running: true, text: '检查中' };
    case 'passed': return { tone: 'blue', text: '检查完成' };
    case 'held': return { tone: 'gray', text: '暂不发布' };
    case 'failed': return { tone: 'red', icon: 'x', text: `${fail} 项失败` };
    case 'published': return { tone: 'teal', icon: 'check', text: '已发布' };
    case 'rejected': return { tone: 'gray', icon: 'undo', text: '已退回' };
    default: return { tone: 'gray', text: u?.state || '' };
  }
}

/** 一行下面的说明：「412 KB · 上传于 21:36 · 租户 · 示例租户」 */
export function uploadMeta(u, now = Date.now()) {
  const parts = [formatBytes(u.size), `上传于 ${fmtClock(u.uploaded_at, now)}`];
  if (u.tenant_label) parts.push(`租户 · ${u.tenant_label}`);
  return parts.join(' · ');
}

/** 列表顺序：待审的在前（检查中、检查完成、有失败项），已发布、已退回的在后；同组按上传时间倒序 */
export function orderUploads(items) {
  const t = (u) => Date.parse(u.uploaded_at) || 0;
  return [...(items || [])].sort((a, b) => (isPending(b) - isPending(a)) || (t(b) - t(a)));
}

/** 默认选中哪个：地址参数指定的 → 第一个能发布的 → 第一个待审的 → 第一个 */
export function pickUpload(items, wanted) {
  const list = items || [];
  if (wanted && list.some((u) => u.id === wanted)) return wanted;
  return (list.find(isPublishable) || list.find(isPending) || list[0])?.id || null;
}

/** 检查卡上切换哪几个包：待审的（最多 max 个），选中的不在里面时补上 */
export function segUploads(items, selectedId, max = 3) {
  const list = orderUploads(items);
  const out = list.filter(isPending).slice(0, max);
  const sel = list.find((u) => u.id === selectedId);
  if (sel && !out.includes(sel)) out.push(sel);
  return out.map((u) => [u.id, uploadTitle(u)]);
}

// ---------- 清单摘要（kind、贡献、生效时机） ----------

const KIND_LABEL = { capability: '能力包', knowledge: '行业包', node: '能力包', provider: '数据接入', exporter: '导出器' };
const CATEGORY_LABEL = { capability: '能力包', industry: '行业包', system: '系统知识包', provider: '数据接入', node: '能力包', exporter: '导出器' };
/** 「knowledge（行业包）」：清单带 category 时按它，否则按后端的缺省类别（knowledge → 行业包） */
export function kindText(m) {
  if (!m?.kind) return '';
  return `${m.kind}（${CATEGORY_LABEL[m.category] || KIND_LABEL[m.kind] || m.kind}）`;
}

const CONTRIB = [
  ['commands', '命令'], ['agents', '子代理'], ['skills', '技能'], ['scripts', '脚本'], ['templates', '模板'],
  ['nodes', '节点'], ['gates', '关卡'], ['hooks', '守卫'], ['pages', '页面'], ['entries', '界面入口'],
  ['artifacts', '产物类型'], ['views', '视图'], ['reports', '报告格式'], ['config', '可调设置'], ['metrics', '指标'],
];
const countOf = (v) => (Array.isArray(v) ? v.length : Number(v) || 0);
/** 贡献：「命令 1 · 模板 3 · 节点 1 · 关卡 1」，为 0 的不写 */
export function contributesText(c) {
  const parts = CONTRIB.map(([k, label]) => [label, countOf(c?.[k])]).filter(([, n]) => n > 0).map(([label, n]) => `${label} ${n}`);
  return parts.join(' · ') || '没有声明贡献';
}

/** 生效时机：纯数据现读即生效；带命令、节点、守卫的从下一条消息开始；提供方、导出器要装到平台 */
export function effectText(m) {
  if (!m) return '';
  if (m.kind === 'provider' || m.kind === 'exporter') return '装进连接器网关或旁路容器后生效（由管理员安装到平台）';
  const c = m.contributes || {};
  const runtime = ['commands', 'agents', 'skills', 'hooks', 'nodes'].some((k) => countOf(c[k]) > 0);
  if (m.kind === 'knowledge') return runtime ? '数据现读即生效；新增的步骤从下一条消息开始生效' : '数据现读即生效';
  return '下一条消息生效';
}

// ---------- 发布 ----------

/** 不能发布时的状态（能发布返回 null） */
export function publishBlock(u) {
  if (!u) return null;
  if (isPublishable(u)) return null;
  switch (u.state) {
    case 'checking': return '检查完成后才能发布';
    case 'failed': return `${u.summary?.fail || 0} 项失败，不能发布`;
    case 'published': return '已发布';
    case 'rejected': return '已退回';
    default: return '这个上传现在不能发布。';
  }
}
export const canReject = (u) => !!u && ['passed', 'failed', 'held'].includes(u.state);

export const CHANNELS = [
  ['beta', 'β', '先给部分项目试用'],
  ['stable', 'S', '所有项目可选'],
];

export const defaultPublishForm = () => ({ channel: 'beta', by: 'project', percent: 10, targets: [], autoBreaker: true });

/** 灰度表单校验：返回第一条问题或 null */
export function rolloutProblem(form) {
  if (!CHANNELS.some(([k]) => k === form?.channel)) return '选一个发布通道';
  if (!['project', 'tenant'].includes(form.by)) return '选择按项目还是按租户灰度';
  const p = form.percent;
  if (!Number.isInteger(p) || p < 0 || p > 100) return '灰度百分比要在 0～100 之间';
  const targets = form.targets || [];
  if (p === 0 && !targets.length) return `灰度范围是空的：拉一个百分比，或指定${form.by === 'tenant' ? '租户' : '项目'}`;
  if (targets.length > 50) return '指定的对象最多 50 个';
  if (new Set(targets.map((t) => t.id)).size !== targets.length) return '指定的对象有重复';
  return null;
}

/** PL-17 请求体（契约：channel、rollout{by, percent, targets?}、auto_breaker） */
export function publishBody(form) {
  const targets = (form.targets || []).map((t) => t.id);
  return {
    channel: form.channel,
    rollout: { by: form.by, percent: form.percent, ...(targets.length ? { targets } : {}) },
    auto_breaker: !!form.autoBreaker,
  };
}

/** O-01 stats.projects → 能收到灰度的项目数（不算已归档） */
export function eligibleProjects(stats) {
  const p = stats?.projects;
  if (!p || p.total == null) return null;
  return Math.max(0, p.total - (p.archived || 0));
}

/** 灰度范围说明的前半句：「约 4 个项目」；之后接「；或指定：」 */
export function rolloutEstimate(form, total) {
  const p = form.percent;
  if (form.by === 'tenant') return p >= 100 ? '全部租户' : `约 ${p}% 的租户`;
  if (p >= 100) return '全部项目';
  if (total == null) return `约 ${p}% 的项目`;
  return `约 ${Math.round((total * p) / 100)} 个项目`;
}

export function publishToast(u, form) {
  const extra = form.targets?.length ? `，另指定 ${form.targets.length} 个${form.by === 'tenant' ? '租户' : '项目'}` : '';
  return `${uploadTitle(u)} 已发布到 ${form.channel}，灰度 ${form.percent}%${extra}`;
}

// ---------- 错误 ----------

const INVALID_REASON = {
  not_zip: '不是有效的 zip 包',
  manifest_missing: '包里缺少 manifest.yaml',
  checksums_missing: '包里缺少 checksums.sha256',
  path_traversal: '包里有越界路径',
  symlink: '包里有符号链接',
};
/** PL-13～PL-18 的错误码 → 一句话（errors.js 没收的几个码在这里） */
export function uploadErrorText(e, u) {
  const d = e?.details || {};
  switch (e?.code) {
    case 'pack_upload_invalid': return INVALID_REASON[d.reason] || e.serverMessage || '插件包格式不对';
    case 'payload_too_large': return `插件包超过 ${formatBytes(d.limit_bytes || PACK_MAX_BYTES)} 上限`;
    case 'static_check_failed': return `静态检查有失败项，不能发布${d.failed?.length ? `：${d.failed.join('、')}` : ''}`;
    case 'pack_upload_state': return d.state === 'rejected' ? '这个上传已经退回了' : '这个上传已经发布了';
    case 'pack_version_exists':
      return `包仓库里已经有 ${uploadTitle(u) || '这个版本'}，内容和这次上传的不一样。版本目录不可修改，请改版本号重新打包上传。`;
    default: return errorText(e);
  }
}

// ---------- 审核记录 ----------

/**
 * PL-15 的 timeline（{at, actor, text, state}）→ 时间线的行。
 * 后端只记「上传」「静态检查通过 / 未通过」「发布到 x」「退回：原因」，这里按 state 拼成设计稿的说法，
 * 末尾补一行当前在等什么（检查中 / 待发布 / 待退回）。nameOf(actor) 给出「管理员 张工」这样的称呼。
 */
export function timelineRows(u, { nameOf = (x) => x, now = Date.now() } = {}) {
  if (!u) return [];
  const items = u.timeline || [];
  const who = (actor) => (actor && actor !== 'system' ? nameOf(actor) : '');
  const meta = (...parts) => parts.filter(Boolean).join(' · ');
  const uploadedAt = Date.parse(items.find((x) => x.state === 'checking')?.at || u.uploaded_at);
  const rows = items.map((it) => {
    const clock = fmtClock(it.at, now);
    if (it.state === 'checking') return { icon: 'check', title: `上传 ${u.filename}`, meta: meta(who(it.actor), u.tenant_label ? `租户 · ${u.tenant_label}` : '', clock) };
    if (it.state === 'passed' || it.state === 'failed') {
      const secs = Math.max(1, Math.round((Date.parse(it.at) - uploadedAt) / 1000));
      return { icon: it.state === 'failed' ? 'x' : 'check', dot: it.state === 'failed' ? 'err' : '', title: `静态检查：${summaryText(u.summary)}`, meta: meta(clock, Number.isFinite(secs) ? `用时 ${secs} 秒` : '') };
    }
    if (it.state === 'published') return { icon: 'upload', title: it.text || '发布', meta: meta(who(it.actor), clock) };
    if (it.state === 'rejected') {
      const reason = (it.text || '').replace(/^退回[：:]\s*/, '');
      return { icon: 'undo', dot: 'gray', title: '退回', meta: meta(who(it.actor), clock), note: reason && reason !== it.text ? reason : '' };
    }
    return { icon: 'dot', title: it.text || it.state, meta: meta(who(it.actor), clock) };
  });
  if (u.state === 'checking') rows.push({ spin: true, dot: 'wait', title: '静态检查中', meta: '' });
  else if (isPublishable(u)) rows.push({ icon: 'hand', dot: 'wait', title: '待发布', meta: '' });
  else if (u.state === 'failed') rows.push({ icon: 'hand', dot: 'wait', title: '待退回', meta: '' });
  return rows;
}

/** 退回原因的初稿：列出失败项 */
export function rejectDraft(u) {
  const failed = (u?.checks || []).filter((c) => c.result === 'fail').map((c) => c.label);
  return failed.length ? `静态检查有 ${failed.length} 项失败：${failed.map((x) => `「${x}」`).join('')}。请修改后重新上传。` : '';
}

export function reasonProblem(reason) {
  const t = (reason || '').trim();
  if (!t) return '请写明退回原因';
  if (t.length > 500) return '退回原因最多 500 字';
  return null;
}

// ---------- 管理员审核申请（AD-08～AD-10） ----------

export const REQUEST_KINDS = {
  pack_install: { label: '插件安装', icon: 'puzzle', cls: 'cap' },
  source_registration: { label: '数据源登记', icon: 'db', cls: 'prov' },
  knowledge_nomination: { label: '知识提名', icon: 'book', cls: 'ind' },
};
export const REQUEST_STATES = { pending: ['待处理', 'amber'], approved: ['已通过', 'teal'], rejected: ['已退回', 'gray'] };

/** 按类型、状态筛（state：pending | done | all），时间倒序 */
export function filterRequests(items, { kind = 'all', state = 'pending' } = {}) {
  return (items || [])
    .filter((r) => kind === 'all' || r.kind === kind)
    .filter((r) => state === 'all' || (state === 'pending' ? r.state === 'pending' : r.state !== 'pending'))
    .sort((a, b) => (Date.parse(b.requested_at) || 0) - (Date.parse(a.requested_at) || 0));
}

/** 各类型待处理的个数（页签上的数字） */
export function requestCounts(items) {
  const out = { all: 0, pack_install: 0, source_registration: 0, knowledge_nomination: 0, done: 0 };
  (items || []).forEach((r) => {
    if (r.state !== 'pending') { out.done += 1; return; }
    out.all += 1;
    if (r.kind in out) out[r.kind] += 1;
  });
  return out;
}

/** 申请的理由：payload 里的 note / reason，没有时用 summary */
export const requestReason = (r) => r?.payload?.note || r?.payload?.reason || '';

/** 一行的说明：「李明 · 社区诊所排队分诊 · 10-31 20:15」 */
export function requestMeta(r, { nameOf = (x) => x, now = Date.now() } = {}) {
  const project = r.payload?.project_name || r.project_id || '';
  return [nameOf(r.requested_by), project, fmtClock(r.requested_at, now)].filter(Boolean).join(' · ');
}

/** 通过前要提醒的话（数据源要先写凭据；知识提名进行业包下一个版本） */
export function approveHint(r) {
  if (r?.kind === 'source_registration') return '通过前先在数据源登记里写入凭据并测试连接；申请里不带密钥。';
  if (r?.kind === 'knowledge_nomination') return `通过后进入${r.payload?.industry_pack_label ? `「${r.payload.industry_pack_label}」` : '对应行业包'}下一个版本的待发布内容，原文已脱敏。`;
  if (r?.kind === 'pack_install') return '通过后在插件中心把这个数据接入插件安装到平台，装好后申请人就能启用。';
  return '';
}

export function requestToast(r, action) {
  const label = REQUEST_KINDS[r?.kind]?.label || '申请';
  return action === 'approve' ? `已通过${label}申请：${r.summary}` : `已退回${label}申请：${r.summary}`;
}
