// 系统逆向建模的三个资料子页（sources、snapshots、schema）共用的纯逻辑：
// 状态文字、时间与时长、登记表单的解析与校验、采集确认的展示、锚点拆解、ER 视图排版。

export const PACK = 'rev';
export const PACK_LABEL = '系统逆向建模';

// ---------- 采集任务（设计稿 EVD_JOB） ----------
export const JOB_STATE = {
  queued: ['排队', 'gray', 'clock'],
  running: ['运行中', 'st-run', null],
  succeeded: ['成功', 'teal', 'check'],
  partial: ['部分完成', 'amber', 'alert'],
  failed: ['失败', 'red', 'x'],
  cancelled: ['已取消', 'gray', 'ban'],
};
export const ACTIVE_JOB_STATES = ['queued', 'running'];
export const isJobActive = (job) => !!job && ACTIVE_JOB_STATES.includes(job.state);

// 采集任务表的筛选：全部 / 进行中 / 已结束（契约 EV-15 的 state=active|finished）
export const JOB_FILTERS = [['all', '全部'], ['run', '进行中'], ['done', '已结束']];
export const jobFilterQuery = (f) => (f === 'run' ? 'active' : f === 'done' ? 'finished' : undefined);
export function jobMatches(job, filter) {
  if (filter === 'run') return isJobActive(job);
  if (filter === 'done') return !isJobActive(job);
  return true;
}

// 项目流 evidence.job.updated：按 id 替换，没有就插到最前
export function mergeJob(list, job) {
  if (!job?.id) return list;
  const i = list.findIndex((j) => j.id === job.id);
  if (i < 0) return [job, ...list];
  const next = list.slice();
  next[i] = { ...list[i], ...job };
  return next;
}

export const jobOpText = (job) => [job.operation, job.scope_text].filter(Boolean).join(' · ');

// 「阶段 / 结果」列
export function jobPhase(job) {
  if (!job) return { text: '' };
  if (job.state === 'running') {
    const p = job.progress;
    const count = p ? `${p.done} / ${p.total} ${p.unit}` : '';
    return { text: [job.phase, count].filter(Boolean).join(' · '), pct: p ? p.pct ?? Math.round((p.done / Math.max(1, p.total)) * 100) : null };
  }
  if (job.state === 'queued') return { text: job.queue_reason || '排队中', muted: true };
  if (job.state === 'failed') return { text: job.error?.message || job.result?.message || '采集失败', tone: 'red' };
  if (job.state === 'succeeded' && job.result?.snapshot_id) return { text: '生成快照', snapshot: job.result.snapshot_id };
  if (job.state === 'cancelled') return { text: job.result?.message || '已取消', muted: true };
  return { text: job.result?.message || '' };
}

// 每行最后一列的按钮（契约 CollectionJob.actions）
const JOB_ACTION = {
  cancel: { label: '取消', icon: 'x' },
  browse: { label: '浏览', icon: 'search' },
  resume: { label: '续采', icon: 'play', kind: 'soft' },
  retry: { label: '重试', icon: 'refresh' },
  notify_admin: { label: '联系管理员', icon: 'msg' },
};
export const jobActions = (job) => (job?.actions || []).filter((a) => JOB_ACTION[a]).map((a) => ({ op: a, ...JOB_ACTION[a] }));

// ---------- 时间 ----------
const pad = (n) => String(n).padStart(2, '0');
// 今天的只写时分，其余写月-日 时分
export function fmtWhen(iso, now = Date.now()) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const n = new Date(now);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const same = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  return same ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}
export function fmtDay(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export const fmtHM = (ms) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

export function fmtDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} 毫秒`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${pad(s % 60)} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

// 运行中的任务按当前时间算用时；排队的没有用时
export function jobDuration(job, now = Date.now()) {
  if (!job || job.state === 'queued') return null;
  if (job.state === 'running') return job.started_at ? Math.max(0, now - Date.parse(job.started_at)) : null;
  return job.duration_ms ?? null;
}

// ---------- 快照 ----------
// 20261030-221500-9c1d → 9c1d（按钮、增量说明里用短写）
export const shortSnap = (id) => String(id || '').split('-').pop();
export const RECORD_ORDER = ['entity', 'field', 'relation', 'constraint', 'operation', 'rule', 'flow', 'profile', 'sample', 'term'];
export const snapRecords = (records) => RECORD_ORDER.filter((k) => records?.[k]).map((k) => [k, records[k]]);
export const fmtNum = (n) => Number(n || 0).toLocaleString('en-US');

// 快照表上的选择：最多两份，再点第三份时挤掉最早选的；同一份再点取消
export function toggleSnapPick(picked, id) {
  if (picked.includes(id)) return picked.filter((x) => x !== id);
  return [...picked, id].slice(-2);
}

// 选中的两份按时间排成「旧 → 新」；不是同一数据源时不能对比
export function diffPair(picked, snapshots) {
  const rows = picked.map((id) => snapshots.find((s) => s.id === id)).filter(Boolean);
  if (rows.length !== 2) return null;
  const [a, b] = rows.sort((x, y) => String(x.started_at).localeCompare(String(y.started_at)));
  return { from: a.id, to: b.id, sameSource: a.source?.id === b.source?.id };
}

// 默认对比：同一数据源最新的两份完整快照
export function defaultDiffPair(snapshots) {
  const done = snapshots.filter((s) => s.complete).sort((x, y) => String(y.started_at).localeCompare(String(x.started_at)));
  const latest = done[0];
  if (!latest) return null;
  const prev = done.find((s) => s !== latest && s.source?.id === latest.source?.id);
  return prev ? { from: prev.id, to: latest.id } : null;
}

// 进证据浏览器时选哪份快照：地址里指定的 > 锚点解析出的 > 最新完整快照
export function pickSnapshot(snapshots, { wanted, sourceId } = {}) {
  if (wanted && snapshots.some((s) => s.id === wanted)) return wanted;
  const pool = sourceId ? snapshots.filter((s) => s.source?.id === sourceId) : snapshots;
  const sorted = [...(pool.length ? pool : snapshots)].sort((x, y) => String(y.started_at).localeCompare(String(x.started_at)));
  return (sorted.find((s) => s.complete) || sorted[0])?.id || null;
}

export const DIFF_CHANGE = { added: ['新增', 'teal'], removed: ['删除', 'red'], modified: ['修改', 'amber'] };

// 统计数字后面的说明（「成功 3 · 部分 1 · 失败 1」），只写不为 0 的
export function monthText(m) {
  if (!m) return '';
  return [['succeeded', '成功'], ['partial', '部分'], ['failed', '失败'], ['cancelled', '取消']]
    .filter(([k]) => m[k]).map(([k, t]) => `${t} ${m[k]}`).join(' · ');
}
export const GATEWAY_STATE = { ok: ['正常', ''], slow: ['较慢', 'var(--amber2)'], down: ['不可用', 'var(--red)'] };

// ---------- 数据源 ----------
export const ENV = { production: ['生产', 'red'], test: ['测试', 'gray'] };
export const KIND_LABEL = { file: '文件', db: '数据库', code: '代码仓库', api: '接口文档', rea: '逆向分析', ui: '界面录制', cli: '命令行', log: '日志' };
export const KIND_ICON = { file: 'filetext', db: 'db', code: 'code', api: 'globe', rea: 'cpu', ui: 'layout', cli: 'code', log: 'activity' };
export const providerKind = (provider, providers = []) => providers.find((p) => p.id === provider?.id)?.kind
  || (/^db-/.test(provider?.id || '') ? 'db' : /^code-/.test(provider?.id || '') ? 'code' : /^api-/.test(provider?.id || '') ? 'api' : 'db');

// ---------- 上传程序的自助类提供方（F13，如程序逆向 rea-static） ----------
export const isArtifactProvider = (p) => p?.input === 'artifact';
// 数据源是不是上传程序的：有提供方信息看提供方，没有就看连接参数
export function isArtifactSource(source, providers = []) {
  const p = providers.find((x) => x.id === source?.provider?.id);
  return p ? isArtifactProvider(p) : !!source?.connection?.artifact_sha256;
}
export function artifactOf(source) {
  const c = source?.connection || {};
  return c.artifact_sha256 ? { sha256: c.artifact_sha256, filename: c.artifact_filename, size: c.artifact_size } : null;
}
export const artifactText = (a) => (a ? [a.filename, a.size != null ? fmtBytes(a.size) : null].filter(Boolean).join(' · ') : '');
const artifactConnection = (a) => ({ artifact_sha256: a.sha256, artifact_filename: a.filename, artifact_size: a.size });
// 登记人和管理员能换版本、改、删（EV-04、EV-05）
export const canManageSource = (source, user, admin) => !!admin || (!!user?.username && source?.owner === user.username);

// 连接参数（不含密钥）的一行写法
export function connText(source) {
  const c = source?.connection || {};
  if (c.artifact_sha256) return artifactText(artifactOf(source));
  if (c.host) {
    const hp = c.port ? `${c.host}:${c.port}` : c.host;
    const db = c.database ? ` / ${c.database}` : '';
    return `${hp}${db}${c.username ? ` · ${c.username}` : ''}`;
  }
  if (c.repo_url) return `${c.repo_url}${c.ref ? ` @ ${c.ref}` : ''}`;
  if (c.spec_url) return c.spec_url;
  return Object.values(c).filter(Boolean).join(' · ') || '—';
}

export function windowText(w, { long = false } = {}) {
  if (!w) return '全天';
  const days = w.days === 'daily' || !w.days ? (long ? '每天 ' : '') : `${[].concat(w.days).join('、')} `;
  return long ? `${days}${w.start} – ${w.end}` : `${days}${w.start}–${w.end}`;
}
// 「erp.* 42 表」→ 发起采集里的范围标签「erp.*（42 表）」
export function scopeTag(summary) {
  const m = String(summary || '').match(/^(.*?)\s+(\d+)\s*表$/);
  return m ? `${m[1]}（${m[2]} 表）` : String(summary || '');
}
export const scopeWindowText = (s) => (s.connection?.artifact_sha256 ? '—' : `${s.scope_summary || s.scope_allowlist?.join(' ') || '—'} · ${windowText(s.access_window)}`);

// 当前时刻是否在访问时间窗口内（窗口可以跨午夜）
export function inWindow(w, now = Date.now()) {
  if (!w) return true;
  const toMin = (t) => { const [h, m] = String(t).split(':').map(Number); return h * 60 + (m || 0); };
  const d = new Date(now);
  const cur = d.getHours() * 60 + d.getMinutes();
  const s = toMin(w.start);
  const e = toMin(w.end);
  return s <= e ? cur >= s && cur < e : cur >= s || cur < e;
}

// ---------- 登记表单里的几个一行输入：格式化与解析（解析不了返回 null） ----------
const GB = 1024 ** 3;
const MB = 1024 ** 2;
export function fmtBudget(b) {
  if (!b) return '';
  const rows = b.max_rows >= 10000 ? `${+(b.max_rows / 10000).toFixed(1)} 万行` : `${b.max_rows} 行`;
  const bytes = b.max_bytes >= GB ? `${+(b.max_bytes / GB).toFixed(1)} GB` : `${Math.round(b.max_bytes / MB)} MB`;
  const dur = b.max_duration_s >= 3600 ? `${+(b.max_duration_s / 3600).toFixed(1)} 小时` : `${Math.round(b.max_duration_s / 60)} 分钟`;
  return `≤ ${rows} · ${bytes} · ${dur}`;
}
export function parseBudget(text) {
  const t = String(text || '');
  const rows = t.match(/([\d.]+)\s*(万)?\s*行/);
  const bytes = t.match(/([\d.]+)\s*(GB|MB)/i);
  const dur = t.match(/([\d.]+)\s*(分钟|小时)/);
  if (!rows || !bytes || !dur) return null;
  return {
    max_rows: Math.round(Number(rows[1]) * (rows[2] ? 10000 : 1)),
    max_bytes: Math.round(Number(bytes[1]) * (bytes[2].toUpperCase() === 'GB' ? GB : MB)),
    max_duration_s: Math.round(Number(dur[1]) * (dur[2] === '小时' ? 3600 : 60)),
  };
}
export const fmtRate = (r) => (r ? `并发 ${r.concurrency} · 每秒 ${r.qps} 查询 · 单查询 ${r.query_timeout_s} 秒` : '');
export function parseRate(text) {
  const t = String(text || '');
  const c = t.match(/并发\s*(\d+)/);
  const q = t.match(/每秒\s*(\d+)/);
  const s = t.match(/单查询\s*(\d+)\s*秒/);
  if (!c || !q || !s) return null;
  return { concurrency: Number(c[1]), qps: Number(q[1]), query_timeout_s: Number(s[1]) };
}
export function parseWindow(text) {
  const t = String(text || '').trim();
  if (!t || t === '全天') return { ok: true, value: null };
  const m = t.match(/(\d{1,2}):(\d{2})\s*[–—~-]\s*(\d{1,2}):(\d{2})/);
  if (!m || Number(m[1]) > 23 || Number(m[3]) > 23 || Number(m[2]) > 59 || Number(m[4]) > 59) return { ok: false, value: null };
  return { ok: true, value: { days: 'daily', start: `${pad(m[1])}:${m[2]}`, end: `${pad(m[3])}:${m[4]}`, tz: 'Asia/Shanghai' } };
}
export const fmtEgress = (list) => (list || []).map((e) => (e.port ? `${e.host}:${e.port}` : e.host)).join(', ');
export function parseEgress(text) {
  const parts = String(text || '').split(/[,，\s]+/).filter(Boolean);
  if (!parts.length) return null;
  const out = [];
  for (const p of parts) {
    const m = p.match(/^([A-Za-z0-9.-]+):(\d{1,5})$/);
    if (!m || Number(m[2]) > 65535) return null;
    out.push({ host: m[1], port: Number(m[2]) });
  }
  return out;
}
// 「3306 / erp」→ 端口与库名
export function parsePortDb(text) {
  const m = String(text || '').match(/^\s*(\d{1,5})\s*\/\s*([A-Za-z0-9_$-]+)\s*$/);
  if (!m || Number(m[1]) < 1 || Number(m[1]) > 65535) return null;
  return { port: Number(m[1]), database: m[2] };
}

export const SOURCE_ID_RE = /^[a-z0-9][a-z0-9-]{1,40}$/;
// 网关只实现了 mask-v1（ADR-0086，evidence-collection.md §9）
export const MASKING = [
  ['mask-v1', '默认脱敏 mask-v1（手机号、证件号、姓名、地址、金额）'],
];

// 登记表单：新登记是空的，编辑时从 EvidenceSource 填
export function emptyForm(user) {
  return {
    provider: '', display_name: '', id: '', conn: {}, port_db: '', environment: 'production', password: '',
    scope: [], egress: '', window: '每天 22:00 – 06:00', budget: fmtBudget({ max_rows: 2000000, max_bytes: 2 * GB, max_duration_s: 1800 }),
    rate: fmtRate({ concurrency: 2, qps: 20, query_timeout_s: 30 }), masking: 'mask-v1', note: '', owner: user?.username || '', artifact: null,
  };
}
export function formFromSource(s) {
  const c = s.connection || {};
  const { port, database, ...rest } = c;
  return {
    provider: s.provider?.id || '', display_name: s.display_name, id: s.id, conn: rest,
    port_db: port || database ? `${port ?? ''} / ${database ?? ''}` : '', environment: s.environment, password: '',
    scope: [...(s.scope_allowlist || [])], egress: fmtEgress(s.egress), window: s.access_window ? windowText(s.access_window, { long: true }) : '全天',
    budget: fmtBudget(s.budget), rate: fmtRate(s.rate_limit), masking: s.masking_policy || 'mask-v1', note: s.authorization_note || '', owner: s.owner || '',
    artifact: artifactOf(s),
  };
}

// 连接参数按提供方的 cred_fields 出（端口和库名合成一格，设计稿「端口 / 库名」）
export function connFields(provider) {
  const fields = (provider?.cred_fields || []).filter((f) => !f.secret);
  const names = fields.map((f) => f.name);
  const portDb = names.includes('port') && names.includes('database');
  return { fields: fields.filter((f) => !portDb || (f.name !== 'port' && f.name !== 'database')), portDb };
}
export const secretField = (provider) => (provider?.cred_fields || []).find((f) => f.secret) || null;
// 「写入新密码」「写入新访问令牌」
export const secretInputLabel = (sf) => `写入新${sf?.label || '密码'}`;
const DEFAULT_PORT = { 'db-postgres': 5432 };
export const portDbPlaceholder = (provider) => (DEFAULT_PORT[provider?.id] ? `${DEFAULT_PORT[provider.id]} / crm` : '3306 / erp');
export const scopePlaceholder = (provider) => (provider?.kind === 'code' ? '如 src/main/java/*' : '如 erp.store*');

// 凭据只写不读（EV-01 secret_fields、EV-06）：只看密钥字段写没写过、什么时候写的，从不回显；
// 普通用户看别人登记的数据源时 cred_ref 为空，但 secret_fields 照给
export function credState(source) {
  const fields = source?.secret_fields || [];
  const done = fields.filter((f) => f.set);
  const ref = source?.cred_ref || null;
  return {
    needed: fields.length > 0, set: done.length > 0 || !!ref, ref,
    at: done.map((f) => f.written_at).filter(Boolean).sort().pop() || null,
  };
}
export const credText = (c, now) => (c?.at ? `已设置 · ${fmtWhen(c.at, now)}` : '已设置');

// 代码仓库只认 https（ADR-0086 克隆加固）；出口白名单是仓库主机的 443（§10）
const REPO_URL = /^https:\/\/(?:[^@/]+@)?([A-Za-z0-9.-]+)(?::(\d+))?(?:[/?#]|$)/i;
export function repoHost(url) {
  const m = String(url || '').trim().match(REPO_URL);
  return m ? m[1].toLowerCase() : null;
}
// 仓库地址里的主机和端口，没写端口是 443
export function repoEndpoint(url) {
  const m = String(url || '').trim().match(REPO_URL);
  return m ? { host: m[1].toLowerCase(), port: m[2] ? Number(m[2]) : 443 } : null;
}
// 出口地址的缺省：数据库是主机和端口，代码仓库是仓库地址的主机和端口
export function defaultEgress(provider, form) {
  if (provider?.kind === 'code') {
    const endpoint = repoEndpoint(form.conn?.repo_url);
    return endpoint ? fmtEgress([endpoint]) : '';
  }
  const port = parsePortDb(form.port_db)?.port;
  return form.conn?.host && port ? fmtEgress([{ host: form.conn.host, port }]) : '';
}

// 登记抽屉的步骤：上传程序的提供方只有三步，没有凭据、范围与限制、测试连接
export const STEP_TITLES = ['选择类型', '连接参数', '凭据', '范围与限制', '授权说明与负责人', '测试连接'];
export const ARTIFACT_STEP_TITLES = ['选择类型', '上传程序', '授权说明'];
export const wizardSteps = (provider) => (isArtifactProvider(provider) ? ARTIFACT_STEP_TITLES : STEP_TITLES);
const NOTE_REQUIRED = 'REA 逆向类数据源要写明软件授权范围';

/**
 * 逐步检查登记表单。返回 { errors: {字段: 说明}, done: bool[步数], current: 第一步没完成的下标 }。
 * admin=false 时凭据一步算跳过（申请里不能带密钥）；tested 表示测试连接已通过。
 * 上传程序的提供方：三步，要上传完的程序（form.artifact）和授权说明。
 */
export function validateForm(form, { provider, admin = true, editing = false, hasCred = false, tested = false } = {}) {
  const errors = {};
  if (!form.display_name.trim()) errors.display_name = '写个显示名';
  if (!SOURCE_ID_RE.test(form.id)) errors.id = '小写字母、数字和连字符，2～41 位，字母或数字开头';
  if (isArtifactProvider(provider)) {
    if (!form.artifact) errors.artifact = '先上传程序';
    if (!form.note.trim()) errors.note = NOTE_REQUIRED;
    const done = [true, !errors.display_name && !errors.id && !errors.artifact, !errors.note];
    return { errors, done, current: done.findIndex((x) => !x) };
  }
  const { fields, portDb } = connFields(provider);
  fields.forEach((f) => { if (!String(form.conn[f.name] ?? '').trim()) errors[`conn.${f.name}`] = `写上${f.label}`; });
  if (fields.some((f) => f.name === 'repo_url') && form.conn.repo_url?.trim() && !repoHost(form.conn.repo_url)) errors['conn.repo_url'] = '只支持 https 地址';
  if (portDb && !parsePortDb(form.port_db)) errors.port_db = `写成「端口 / 库名」，如 ${portDbPlaceholder(provider)}`;
  // 代码仓库不写范围就是整个仓库
  if (!form.scope.length && provider?.kind !== 'code') errors.scope = '至少写一个对象';
  if (!parseEgress(form.egress)) errors.egress = '写成「主机:端口」，多个用逗号隔开';
  if (!parseWindow(form.window).ok) errors.window = '写成「每天 22:00 – 06:00」，不限时写「全天」';
  if (!parseBudget(form.budget)) errors.budget = '写成「≤ 200 万行 · 2 GB · 30 分钟」';
  if (!parseRate(form.rate)) errors.rate = '写成「并发 2 · 每秒 20 查询 · 单查询 30 秒」';
  if (provider?.kind === 'rea' && !form.note.trim()) errors.note = NOTE_REQUIRED;
  if (!form.owner) errors.owner = '选一位负责人';
  const has = (...keys) => !keys.some((k) => Object.keys(errors).some((e) => e === k || e.startsWith(`${k}.`)));
  const done = [
    !!provider,
    !!provider && has('display_name', 'id', 'conn', 'port_db'),
    !!provider && (!admin || !secretField(provider) || (editing ? hasCred || !!form.password : !!form.password)),
    has('scope', 'egress', 'window', 'budget', 'rate'),
    has('note', 'owner') && (provider?.kind !== 'rea' || !!form.note.trim()),
    !admin || tested,
  ];
  const current = done.findIndex((x) => !x);
  return { errors, done, current };
}

// EV-02 / EV-03 的请求体（申请不带 secret）。
// 上传程序的提供方（自助登记）：connection 是上传完的程序，出口、窗口、限流、预算、脱敏都按提供方缺省，环境固定测试
export function registerBody(form, provider, { grantProjectId, withSecret = true } = {}) {
  if (isArtifactProvider(provider)) {
    return {
      id: form.id, display_name: form.display_name.trim(), provider: provider.id, environment: 'test',
      connection: form.artifact ? artifactConnection(form.artifact) : {}, authorization_note: form.note.trim() || null, owner: form.owner,
      ...(grantProjectId ? { grant_project_id: grantProjectId } : {}),
    };
  }
  const { fields, portDb } = connFields(provider);
  const connection = {};
  fields.forEach((f) => { connection[f.name] = String(form.conn[f.name] ?? '').trim(); });
  if (portDb) Object.assign(connection, parsePortDb(form.port_db));
  const sf = secretField(provider);
  return {
    id: form.id, display_name: form.display_name.trim(), provider: provider.id, environment: form.environment, connection,
    ...(withSecret && sf && form.password ? { secret: { [sf.name]: form.password } } : {}),
    scope_allowlist: form.scope, egress: parseEgress(form.egress), access_window: parseWindow(form.window).value,
    // 代码仓库不采样本、表单里没有脱敏策略，交给内核按提供方取缺省（none）
    budget: parseBudget(form.budget), rate_limit: parseRate(form.rate), masking_policy: provider?.kind === 'code' ? null : form.masking,
    authorization_note: form.note.trim() || null, owner: form.owner,
    ...(grantProjectId ? { grant_project_id: grantProjectId } : {}),
  };
}

// 自助类数据源的修改（EV-04）：显示名、程序、授权说明；换版本就是换 connection
export const artifactPatch = (form) => ({
  display_name: form.display_name.trim(), connection: artifactConnection(form.artifact), authorization_note: form.note.trim() || null,
});

// 测试连接的三项（契约 EV-08 checks[].name）
export const CHECK_LABEL = { connectivity: '连通', readonly_account: '账号只读自检', statement_allowlist: '语句白名单' };
export function checkDetail(c) {
  if (c.name === 'connectivity' && c.latency_ms != null && c.ok) return `${c.detail} · ${c.latency_ms} ms`;
  return c.detail || '';
}

// ---------- 采集确认（EV-13 summary → 弹窗的键值） ----------
export const fmtBytes = (n) => (n >= GB ? `${+(n / GB).toFixed(1)} GB` : n >= MB ? `${Math.round(n / MB)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const fmtRows = (n) => (n >= 10000 ? `约 ${+(n / 10000).toFixed(1)} 万行` : `约 ${n} 行`);
export function previewPairs(summary, now = Date.now()) {
  if (!summary) return [];
  const r = summary.rate_limit;
  // 内核给的是登记里的窗口对象（{days, start, end, tz}），旧写法是「22:00 – 06:00」
  const w = summary.window;
  const winText = w && typeof w === 'object' ? `${w.start} – ${w.end}` : w;
  const winStart = w && typeof w === 'object' ? w.start : String(w || '').split(/\s*[–-]\s*/)[0];
  const win = winText
    ? `${winText}（现在 ${fmtHM(now)}，${summary.in_window_now ? '在窗口内' : `不在窗口内，同意后排队到 ${winStart} 开始`}）`
    : '不限时间';
  const m = summary.masking;
  return [
    ['范围', summary.scope_text],
    ['访问时间窗口', win],
    ['限流', r ? `并发 ${r.concurrency} · 每秒 ${r.qps} 查询 · 单查询 ${r.query_timeout_s} 秒超时` : '—'],
    ['预计规模', summary.estimate ? `${fmtRows(summary.estimate.rows)} · ${fmtBytes(summary.estimate.bytes)}（按 discover 估算）` : '还没有 discover 结果，无法估算'],
    ['只读方式', (summary.readonly_methods || []).join(' · ') || '—'],
    ['脱敏', m?.policy && m.policy !== 'none' ? [m.policy, m.sample_rows ? `样本每表最多 ${m.sample_rows} 行` : null].filter(Boolean).join(' · ') : '—'],
  ];
}

// 发起采集的操作：只列提供方支持的（EV-10 capabilities；程序逆向只有快照）
export const OPS = [['discover', '发现'], ['snapshot', '快照'], ['sample', '补采样本']];
export const collectOps = (provider) => (provider?.capabilities ? OPS.filter(([k]) => provider.capabilities.includes(k)) : OPS);
export const canIncremental = (provider) => !provider?.capabilities || provider.capabilities.includes('incremental');

// 发起采集表单 → EV-13 / EV-14 请求体
export function collectBody({ sourceId, operation, incremental, base, budget, sampleTable }) {
  const body = { source_id: sourceId, operation };
  if (operation === 'snapshot' && incremental && base) body.incremental_base = base;
  const b = parseBudget(budget);
  if (b && operation !== 'sample') body.budget = b;
  if (operation === 'sample') body.sample = { selector: String(sampleTable || '').trim(), limit: 20 };
  return body;
}

// ---------- 锚点（契约 1.10 / 7.17：<scheme>:<源ID>/<路径>[#<片段>]） ----------
const dec = (s) => { try { return decodeURIComponent(s); } catch { return s; } };
export function parseAnchor(anchor) {
  const m = String(anchor || '').match(/^([a-z]+):([^/]+)\/([^#]*)(?:#(.*))?$/);
  if (!m) return null;
  return { scheme: m[1], source: dec(m[2]), path: m[3], fragment: m[4] ? dec(m[4]) : null };
}
// 数据库锚点：库.表[.字段]；第三段带冒号的是约束（check:xxx），不算字段
export function parseDbAnchor(anchor) {
  const a = parseAnchor(anchor);
  if (!a || a.scheme !== 'db') return null;
  const seg = a.path.split('.');
  if (seg.length < 2) return null;
  const [schema, table, third] = seg;
  const field = third && !third.includes(':') ? dec(third) : null;
  return {
    source: a.source, schema: dec(schema), table: dec(table), field,
    tableAnchor: `db:${a.source}/${schema}.${table}`,
  };
}
export const fieldAnchor = (tableAnchor, field) => `${tableAnchor}.${encodeURIComponent(field).replace(/\./g, '%2E')}`;
export const shortName = (name) => String(name || '').split('.').pop();

// 证据浏览器的置信度小标签
export const CONF_TONE = { declared: 'teal', inferred: 'indigo', observed: 'blue' };
export const CONF_TEXT = { declared: '声明', observed: '观察', inferred: '推断' };

// 字段一行的说明：类型 · 说明（主键写 PK）
export const fieldDesc = (f) => [f.type, f.note].filter(Boolean).join(' · ');

// 分层摘要里的实体数
export const treeTotal = (tree) => (tree?.domains || []).reduce((n, d) => n + (d.count || 0), 0);

// ---------- ER 视图排版（同设计稿：三栏，宽 160，表头 26、每行 18） ----------
export const ER_W = 160;
const COL_X = [20, 245, 470];
const ROW_H = 18;
const HEAD_H = 26;
const VIEW_W = 676;
const MIN_H = 545;
export const tableHeight = (t) => HEAD_H + (t.fields?.length || 0) * ROW_H + 6;

/**
 * 把 EV-28 的表和关系排成三栏：左栏放选中表所在业务域的其他表，中栏放选中表和直接相连的别处的表，右栏放其余。
 * 返回 { width, height, boxes: {anchor: {x, y, h, t, col}}, edges: [{d, confidence, on, from, to}] }。
 */
export function layoutEr({ tables = [], relations = [] } = {}, focus, { domainOf = () => null, selected } = {}) {
  const byAnchor = new Map(tables.map((t) => [t.anchor, t]));
  const tableOf = (a) => {
    const p = parseDbAnchor(a);
    return p && byAnchor.has(p.tableAnchor) ? p.tableAnchor : (byAnchor.has(a) ? a : null);
  };
  const center = byAnchor.has(focus) ? focus : tables[0]?.anchor;
  const near = new Set();
  relations.forEach((r) => {
    const a = tableOf(r.from_anchor);
    const b = tableOf(r.to_anchor);
    if (a === center && b) near.add(b);
    if (b === center && a) near.add(a);
  });
  const dom = domainOf(center);
  const cols = [[], [center], []];
  tables.forEach((t) => {
    if (t.anchor === center) return;
    if (dom && domainOf(t.anchor) === dom) cols[0].push(t.anchor);
    else if (near.has(t.anchor)) cols[1].push(t.anchor);
    else cols[2].push(t.anchor);
  });
  if (!cols[0].length && cols[2].length > 1) cols[0].push(...cols[2].splice(0, Math.ceil(cols[2].length / 2)));
  const avail = MIN_H - 40;
  const boxes = {};
  cols.forEach((list, ci) => {
    const hs = list.map((a) => tableHeight(byAnchor.get(a)));
    const sum = hs.reduce((x, y) => x + y, 0);
    const gap = list.length > 1 ? Math.max(30, Math.min(90, (avail - sum) / (list.length - 1))) : 0;
    const total = sum + gap * Math.max(0, list.length - 1);
    let y = ci === 1 ? Math.max(20, Math.round((MIN_H - total) / 2)) : 20;
    list.forEach((a, i) => {
      boxes[a] = { x: COL_X[ci], y: Math.round(y), h: hs[i], t: byAnchor.get(a), col: ci, row: i };
      y += hs[i] + gap;
    });
  });
  const height = Math.max(MIN_H, ...Object.values(boxes).map((b) => b.y + b.h + 20));
  // 连线落在哪一行：关系带了列对（外键的 attrs.columns）用第一对，锚点本身是列的用它，都没有就落在表头
  const rowY = (a, box, col) => {
    const name = col || parseDbAnchor(a)?.field;
    const i = name ? box.t.fields.findIndex((f) => f.name === name) : -1;
    return i >= 0 ? box.y + HEAD_H + ROW_H * i + 9 : box.y + 12;
  };
  const used = new Map();
  const bump = (key) => { const n = used.get(key) || 0; used.set(key, n + 1); return n * 4; };
  const hot = selected || center;
  const edges = relations.map((r) => {
    const fa = tableOf(r.from_anchor);
    const ta = tableOf(r.to_anchor);
    if (!fa || !ta || fa === ta) return null;
    const A = boxes[fa];
    const B = boxes[ta];
    const [fc, tc] = Array.isArray(r.columns?.[0]) ? r.columns[0] : [];
    let y1 = rowY(r.from_anchor, A, fc);
    let y2 = rowY(r.to_anchor, B, tc);
    let d;
    if (A.col === B.col) {
      const skip = Math.abs(A.row - B.row) - 1;
      const left = A.col === 0 && skip === 0;
      const x = left ? A.x : A.x + ER_W;
      y1 += bump(`${fa}:${left}`);
      y2 += bump(`${ta}:${left}`);
      const off = left ? -16 : 22 + 14 * Math.max(0, skip) + (A.col === 0 ? 6 : 0);
      d = `M${x} ${y1} C ${x + off} ${y1}, ${x + off} ${y2}, ${x} ${y2}`;
    } else {
      const [L, R, yl, yr] = A.col < B.col ? [A, B, y1, y2] : [B, A, y2, y1];
      const x1 = L.x + ER_W;
      const x2 = R.x;
      const mx = Math.floor((x1 + x2) / 2);
      const yr2 = yr + bump(`${R.t.anchor}:in`);
      d = `M${x2} ${yr2} C ${mx} ${yr2}, ${mx} ${yl}, ${x1} ${yl}`;
    }
    return { d, confidence: r.confidence, basis: r.basis, on: fa === hot || ta === hot, from: fa, to: ta };
  }).filter(Boolean);
  return { width: VIEW_W, height, boxes, edges };
}

// 字段右侧的小字：主键 / 外键 / 脱敏
export const fieldKey = (f) => (f.pk ? 'PK' : f.fk ? 'FK' : f.masked ? '脱敏' : '');

// ER 图例：只列这张图里出现的关系（外键是 declared；命名、取值重合推断出来的是 inferred）
const ER_LEGEND = [['declared', '外键'], ['observed', '取值重合'], ['inferred', '推断']];
export const erLegend = (relations = []) => ER_LEGEND.filter(([k]) => relations.some((r) => r.confidence === k));

// 记录详情的字段列表：EV-25 的 fields 没有主键、外键标记时，从 ER 视图（EV-28）里同一张表补上
export function withKeys(fields = [], erTable = null) {
  const er = new Map((erTable?.fields || []).map((f) => [f.name, f]));
  return fields.map((f) => ({ ...f, pk: !!(f.pk ?? er.get(f.name)?.pk), fk: !!(f.fk ?? er.get(f.name)?.fk), masked: !!(f.masked || er.get(f.name)?.masked) }));
}
// 样本里被遮蔽的列：字段标了 masked 的，加上样本自己带的 masked_columns
export const maskedColumns = (record) => new Set([
  ...(record?.fields || []).filter((f) => f.masked).map((f) => f.name),
  ...(record?.samples?.masked_columns || []),
]);
// 画像按列分组：低基数列的 top-k、数值列的统计量
export const profileColumns = (profile = []) => (profile || []).filter((p) => p.top_k?.length || p.stats);

// 证据浏览器左栏数据库记录的图标：视图、存储过程、表
export function dbRecordIcon(anchor) {
  const path = parseAnchor(anchor)?.path || '';
  if (/\.view:/.test(path)) return 'layers';
  if (/\.proc:/.test(path)) return 'zap';
  return 'table';
}

// 没指定锚点时默认选中的表：关系最多的那张（一样多时取字段多的）
export function hubTable(er) {
  const tables = er?.tables || [];
  if (!tables.length) return null;
  const deg = new Map(tables.map((t) => [t.anchor, 0]));
  (er.relations || []).forEach((r) => {
    [r.from_anchor, r.to_anchor].forEach((a) => {
      const t = parseDbAnchor(a)?.tableAnchor;
      if (deg.has(t)) deg.set(t, deg.get(t) + 1);
    });
  });
  return [...tables].sort((a, b) => (deg.get(b.anchor) - deg.get(a.anchor)) || ((b.fields?.length || 0) - (a.fields?.length || 0)))[0].anchor;
}

// 分层摘要 → 表锚点所在业务域（ER 排版按域分栏）
export function domainIndex(tree) {
  const m = new Map();
  (tree?.domains || []).forEach((d) => (d.entities || []).forEach((e) => m.set(e.anchor, d.name)));
  return m;
}

// ---------- 快照详情（EV-19 的 manifest） ----------
const DETECTED = { electron: 'Electron', javascript: 'JavaScript', dotnet: '.NET', managed: '.NET', pyinstaller: 'PyInstaller', installer: '安装目录', other: '安装目录' };
const shortHash = (h) => (h ? `${String(h).slice(0, 12)}…` : '');
/** 详情弹窗的键值：有什么写什么；程序逆向的快照多「程序」「类型」「SHA-256」「工具」，代码仓库的多「仓库」「提交」 */
export function snapshotPairs(detail) {
  const m = detail?.manifest || {};
  const repo = m.artifact?.repo_url ? m.artifact : null;
  const a = repo ? null : m.artifact;
  const skipped = detail?.inventory?.skipped || [];
  return [
    repo ? ['仓库', repo.repo_url] : null,
    repo?.ref || repo?.commit ? ['提交', [repo.ref, repo.commit ? String(repo.commit).slice(0, 12) : null].filter(Boolean).join(' · ')] : null,
    a ? ['程序', artifactText(a)] : null,
    a?.detected ? ['类型', DETECTED[a.detected] || a.detected] : null,
    a?.sha256 ? ['SHA-256', shortHash(a.sha256)] : null,
    m.tool ? ['工具', `${m.tool.name} ${m.tool.version}`] : ['连接器', detail?.provider || m.provider || '—'],
    m.scope?.length ? ['范围', m.scope.join(' ')] : null,
    detail ? ['覆盖率', `${detail.coverage_pct}%`] : null,
    m.masking_policy && m.masking_policy !== 'none' ? ['脱敏', m.masking_policy] : null,
    skipped.length ? ['跳过', skipped.map((x) => `${x.object}（${x.reason}）`).join('；')] : null,
  ].filter(Boolean);
}
// 局限：REA 给的原样列出，再加「未识别 N 处」
export const snapshotLimits = (detail) => [
  ...(detail?.manifest?.limitations || []),
  ...(detail?.manifest?.unknowns ? [`未识别 ${detail.manifest.unknowns} 处`] : []),
];

// ---------- 证据浏览器：非数据库快照（程序逆向等） ----------
// 快照属于哪类提供方：先查 EV-10，查不到按提供方 ID 前缀猜
export function snapshotKind(snap, providers = []) {
  const id = String(snap?.provider || '').split('@')[0];
  const p = providers.find((x) => x.id === id);
  if (p) return p.kind;
  const m = id.match(/^(db|code|api|rea|log)-/);
  return m ? m[1] : 'db';
}
export const isDbKind = (kind) => kind === 'db';

// 证据浏览器能打开的锚点：数据库表和字段（db:）、程序逆向（bin:）、代码仓库（code:）
export function browseTarget(anchor) {
  const db = parseDbAnchor(anchor);
  if (db) return { source: db.source, table: db.tableAnchor, field: db.field };
  const a = parseAnchor(anchor);
  return a && (a.scheme === 'bin' || a.scheme === 'code') ? { source: a.source, table: anchor, field: null } : null;
}

// bin:、code: 锚点的路径类型（mod、sym、ipc、bridge、route、ext、native、rule、file…）
export const binPathType = (anchor) => parseAnchor(anchor)?.path.split(':')[0] || null;
const BIN_ICON = { mod: 'filetext', ipc: 'send', bridge: 'link', route: 'branch', ext: 'cloud', native: 'cpu', addr: 'cpu', rule: 'scale', file: 'filetext' };
// 左栏和检索结果的图标：类和字段按记录类型，其余按路径类型
export function recordIcon({ anchor, type }) {
  if (type === 'operation' && binPathType(anchor) === 'sym') return 'code';
  if (type === 'field') return 'list';
  return BIN_ICON[binPathType(anchor)] || (type === 'operation' ? 'zap' : 'cube');
}
const KIND_TEXT = {
  module: '模块', class: '类', enum: '枚举', method: '方法', function: '函数', property: '属性', field: '字段', ipc: 'IPC 通道', bridge: '预加载接口',
  route: '路由', endpoint: '外部接口', native: '本地调用',
};
export const recordKindText = (r) => KIND_TEXT[r?.attrs?.kind] || r?.attrs?.kind || r?.type || '';
const listText = (v) => (Array.isArray(v) ? v.filter((x) => x != null && x !== '').join('、') : '');
// 记录详情的键值：位置、模块、签名、类型、表与列、通道、地址、路由、来源
export function recordPairs(r) {
  const a = r?.attrs || {};
  const loc = a.loc?.file ? `${a.loc.file}${a.loc.line ? `:${a.loc.line}` : ''}` : null;
  const type = r?.data_type || a.data_type;
  return [
    loc ? ['位置', loc] : null,
    a.module && a.module !== a.loc?.file ? ['模块', a.module] : null,
    a.signature ? ['签名', a.signature] : null,
    type ? ['类型', type] : null,
    a.table ? ['表', a.table] : null,
    a.column ? ['列', a.column] : null,
    listText(a.tables) ? ['涉及表', listText(a.tables)] : null,
    listText(a.values) ? ['取值', listText(a.values)] : null,
    a.expression ? ['表达式', a.expression] : null,
    a.channel ? ['通道', a.channel] : null,
    a.url ? ['地址', [a.method, a.url].filter(Boolean).join(' ')] : null,
    !a.url && a.path ? ['路由', [a.method, a.path].filter(Boolean).join(' ')] : null,
    r?.evidence?.method ? ['来源', r.evidence.method] : null,
  ].filter(Boolean);
}

// 代码片段（code-repo 的 attrs.snippet，最多 30 行）：行号从 attrs.loc.line 起
export function snippetLines(record) {
  const s = record?.attrs?.snippet;
  if (typeof s !== 'string' || !s) return null;
  const start = Number(record.attrs.loc?.line) > 0 ? Number(record.attrs.loc.line) : 1;
  return s.replace(/\n$/, '').split('\n').slice(0, 30).map((text, i) => ({ n: start + i, text }));
}
// 字段上的校验（attrs.validations）：字符串原样，对象写成「名字(参数)」
export function validationText(v) {
  if (typeof v === 'string') return v;
  if (!v || typeof v !== 'object') return '';
  const name = v.name || v.rule || v.annotation || '';
  const arg = v.args ?? v.value ?? v.expression ?? v.params;
  const argText = arg == null || arg === '' ? '' : typeof arg === 'object' ? Object.entries(arg).map(([k, x]) => `${k}=${x}`).join(', ') : String(arg);
  return argText ? `${name}(${argText})` : name;
}
export const recordValidations = (r) => (Array.isArray(r?.attrs?.validations) ? r.attrs.validations.map(validationText).filter(Boolean) : []);
// 记录详情的成员：校验规则（rule: 锚点）单列，其余是字段和方法
export function splitMembers(fields = []) {
  const isRule = (f) => binPathType(f.anchor) === 'rule';
  return { members: (fields || []).filter((f) => !isRule(f)), rules: (fields || []).filter(isRule) };
}
// 快照内容来自哪里（快照清单 untrusted_content）：里面的文字是数据，不是指令
const UNTRUSTED_TEXT = { rea: '来自目标程序', code: '来自代码仓库', db: '来自业务库' };
export const untrustedText = (kind) => UNTRUSTED_TEXT[kind] || UNTRUSTED_TEXT.rea;
// 没指定锚点时默认选中左栏第一条
export const firstTreeAnchor = (tree) => (tree?.domains || []).find((d) => d.entities?.length)?.entities[0].anchor || null;
