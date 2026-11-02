// mock · 3.19 证据源（EV-01～EV-30）：数据源、采集与快照、证据浏览器
// EV-26 被谁引用在 routes-studio.js（Studio 证据卡先用），EV-29 锚点解析在 index.js（所有页面的出处悬停）
// 生产库采集：EV-13 预览拿 confirmation_token（10 分钟），EV-14 / EV-17 retry 不带有效 token 返回 428；
// 新发起的采集按 0.9 秒一步推进，经项目流 evidence.job.updated 推送；种子里那条运行中的任务静止不动（方便和设计稿对照）。
import * as D from './evidence-data.js';
import { entryRef } from './scenario.js';

const TOKEN_TTL = 10 * 60 * 1000;
const pad = (n) => String(n).padStart(2, '0');
const stamp = (ms) => {
  const d = new Date(ms);
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
};
const toMin = (t) => { const [h, m] = String(t).split(':').map(Number); return h * 60 + (m || 0); };
export function inWindow(w, now = Date.now()) {
  if (!w) return true;
  const d = new Date(now);
  const cur = d.getHours() * 60 + d.getMinutes();
  const s = toMin(w.start);
  const e = toMin(w.end);
  return s <= e ? cur >= s && cur < e : cur >= s || cur < e;
}
const isAdmin = (user) => user?.role === 'admin';
const tableAnchor = (name) => `db:erp-prod/erp.${name}`;

// 「库.表[.字段]」锚点 → 表名、字段名
function splitAnchor(anchor) {
  const m = String(anchor || '').match(/^db:([^/]+)\/([^.#]+)\.([^.#]+)(?:\.([^#]+))?/);
  if (!m) return null;
  return { source: m[1], schema: m[2], table: decodeURIComponent(m[3]), field: m[4] && !m[4].includes(':') ? decodeURIComponent(m[4]) : null };
}

// setTimer、now 只有单测会传（手动推进、固定时刻），开发服务器用真时钟
export function register({ on, json, err, wait, uid, F, emitProj, commandHooks, setTimer, now: nowFn }) {
  const later = setTimer || ((fn, ms) => setTimeout(fn, ms));
  const clock = nowFn || (() => Date.now());
  const srcs = D.sources();
  const prov = D.providers();
  const tokens = new Map();
  const timers = new Map();
  const projects = new Map();
  let jobSeq = 133;

  const proj = (pid) => {
    if (!projects.has(pid)) {
      const main = pid === F.MAIN_PID;
      projects.set(pid, {
        grants: main ? JSON.parse(JSON.stringify(D.GRANTS)) : {},
        jobs: main ? D.seedJobs(pid, clock()) : [],
        snapshots: main ? D.seedSnapshots() : [],
      });
    }
    return projects.get(pid);
  };
  const srcOf = (id) => srcs.find((s) => s.id === id);
  // 登记是全局的：「最近采集」要看主示例项目的任务，先把它种上
  proj(F.MAIN_PID);
  const authorized = (pid, id) => !!proj(pid).grants[id]?.authorized;
  const lastJob = (s) => {
    const done = [...projects.values()].flatMap((p) => p.jobs).filter((j) => j.source.id === s.id && j.started_at && !['queued', 'running'].includes(j.state));
    done.sort((a, b) => b.started_at.localeCompare(a.started_at));
    return done[0] ? { at: done[0].started_at, state: done[0].state } : s.last_job;
  };
  const view = (s, pid, user) => {
    const out = { ...s, last_job: lastJob(s), authorized_for_project: pid ? authorized(pid, s.id) : null };
    // 普通用户只看不敏感的登记字段
    if (!isAdmin(user)) { delete out.egress; delete out.authorization_note; }
    return out;
  };
  const emitJob = (pid, j) => emitProj(pid, 'evidence.job.updated', j);

  // ---------- EV-01～EV-12 数据源 ----------
  on('GET', '/evidence/sources', ({ query, user }) => {
    const pid = query.get('project_id');
    const filter = query.get('filter') || 'all';
    let items = srcs.map((s) => view(s, pid, user));
    if (filter === 'authorized') items = items.filter((s) => s.authorized_for_project);
    if (filter === 'production') items = items.filter((s) => s.environment === 'production');
    return json(200, { items, total: items.length, authorized_count: pid ? srcs.filter((s) => authorized(pid, s.id)).length : 0 });
  });

  const validate = (body, { creating }) => {
    const fields = {};
    if (creating && !/^[a-z0-9][a-z0-9-]{1,40}$/.test(body.id || '')) fields.id = 'pattern';
    if (creating && !body.display_name) fields.display_name = 'required';
    if (creating && !(body.scope_allowlist || []).length) fields.scope_allowlist = 'required';
    return Object.keys(fields).length ? err(400, 'invalid_argument', '登记信息不完整', { fields }) : null;
  };
  const build = (body, user) => {
    const p = prov.items.find((x) => x.id === body.provider);
    const secret = body.secret && Object.values(body.secret).some(Boolean);
    return {
      id: body.id, display_name: body.display_name, environment: body.environment || 'test',
      provider: { id: p.id, version: p.version, label: p.label }, connection: body.connection || {},
      cred_ref: secret ? `cred://${body.id}/1` : null,
      secret_fields: (p.cred_fields || []).filter((x) => x.secret).map((x) => ({ name: x.name, set: !!body.secret?.[x.name], written_at: body.secret?.[x.name] ? new Date().toISOString() : null })),
      scope_allowlist: body.scope_allowlist || [], scope_summary: (body.scope_allowlist || []).join(' '),
      egress: body.egress || [], access_window: body.access_window || null, budget: body.budget, rate_limit: body.rate_limit,
      masking_policy: body.masking_policy || 'mask-v3', authorization_note: body.authorization_note || null, owner: body.owner || user.username,
      last_job: null, authorized_for_project: null,
    };
  };
  on('POST', '/admin/evidence/sources', async ({ body, user }) => {
    if (!isAdmin(user)) return err(403, 'forbidden', '需要管理员权限');
    const bad = validate(body, { creating: true });
    if (bad) return bad;
    if (srcOf(body.id)) return err(409, 'source_id_taken', '数据源 ID 已被占用');
    const p = prov.items.find((x) => x.id === body.provider);
    if (!p?.installed) return err(409, 'provider_not_installed', '提供方未安装', { provider: body.provider });
    await wait(300);
    const s = build(body, user);
    srcs.push(s);
    if (body.grant_project_id) proj(body.grant_project_id).grants[s.id] = { authorized: true, granted_at: new Date().toISOString(), granted_by: user.username };
    return json(201, view(s, body.grant_project_id, user));
  });
  on('POST', '/evidence/source-requests', async ({ body }) => {
    if (body.secret) return err(400, 'invalid_argument', '申请里不能带密钥', { fields: { secret: 'forbidden' } });
    const bad = validate(body, { creating: true });
    if (bad) return bad;
    await wait(200);
    return json(202, { request_id: `req-${uid()}` });
  });
  on('PATCH', '/admin/evidence/sources/([^/]+)', ({ m, body, user }) => {
    if (!isAdmin(user)) return err(403, 'forbidden', '需要管理员权限');
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    const { id, provider, secret, grant_project_id, ...rest } = body;
    Object.assign(s, rest);
    if (rest.scope_allowlist) s.scope_summary = s.id === 'erp-prod' && rest.scope_allowlist.some((x) => x.startsWith('erp.')) ? 'erp.* 42 表' : rest.scope_allowlist.join(' ');
    return json(200, view(s, null, user));
  });
  on('DELETE', '/admin/evidence/sources/([^/]+)', ({ m, user }) => {
    if (!isAdmin(user)) return err(403, 'forbidden', '需要管理员权限');
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    const busy = [...projects.values()].flatMap((p) => p.jobs).find((j) => j.source.id === s.id && j.state === 'running');
    if (busy) return err(409, 'job_state_invalid', '有正在运行的采集任务', { state: 'running' });
    srcs.splice(srcs.indexOf(s), 1);
    projects.forEach((p) => { delete p.grants[s.id]; });
    return new Response(null, { status: 204 });
  });
  on('PUT', '/admin/evidence/sources/([^/]+)/secret', async ({ m, body, user }) => {
    if (!isAdmin(user)) return err(403, 'forbidden', '需要管理员权限');
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    if (!body.fields || !Object.values(body.fields).some(Boolean)) return err(400, 'invalid_argument', '没有要写入的凭据', { fields: { fields: 'required' } });
    await wait(200);
    const v = Number((s.cred_ref || '').split('/').pop()) || 0;
    s.cred_ref = `cred://${s.id}/${v + 1}`;
    const at = new Date().toISOString();
    s.secret_fields = s.secret_fields.map((x) => (body.fields[x.name] ? { ...x, set: true, written_at: at } : x));
    return json(200, { cred_ref: s.cred_ref, written_at: at });
  });
  on('DELETE', '/admin/evidence/sources/([^/]+)/secret', ({ m, user }) => {
    if (!isAdmin(user)) return err(403, 'forbidden', '需要管理员权限');
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    s.cred_ref = null;
    s.secret_fields = s.secret_fields.map((x) => ({ ...x, set: false, written_at: null }));
    return new Response(null, { status: 204 });
  });
  on('POST', '/evidence/sources/([^/]+)/test', async ({ m, body, user }) => {
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    if (!isAdmin(user) && !(body.project_id && authorized(body.project_id, s.id))) return err(403, 'source_not_authorized', '本项目没有获授权使用该数据源');
    await wait(400);
    const c = s.connection || {};
    const where = c.host ? `${c.host}${c.port ? `:${c.port}` : ''}` : (c.repo_url || c.spec_url || s.id);
    const credOk = !!s.cred_ref;
    const checks = [
      { name: 'connectivity', ok: credOk, detail: credOk ? where : '凭据还没写入，连不上', latency_ms: credOk ? 32 : null },
      { name: 'readonly_account', ok: credOk, detail: credOk ? `${c.username || '只读账号'} 没有 INSERT / UPDATE / DELETE / DDL 权限` : '没有连上，跳过' },
      { name: 'statement_allowlist', ok: true, detail: '只放行 SELECT / SHOW / DESCRIBE / EXPLAIN' },
    ];
    return json(200, { ok: checks.every((x) => x.ok), checks, ticket: { id_prefix: `tk-${uid().slice(-4).padStart(4, '7')}`, expires_in_s: 600, consumed: true } });
  });
  on('GET', '/evidence/sources/([^/]+)/access-log', ({ m, query, user }) => {
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    const pid = query.get('project_id');
    if (!isAdmin(user) && !(pid && authorized(pid, s.id))) return err(403, 'source_not_authorized', '本项目没有获授权使用该数据源');
    let items = D.accessLog(s.id, F.MAIN_PID);
    if (!isAdmin(user)) items = items.filter((x) => x.project_id === pid);
    return json(200, { items, next_cursor: null });
  });
  on('GET', '/evidence/providers', () => json(200, prov));
  on('GET', '/projects/([^/]+)/evidence/grants', ({ m }) => json(200, {
    items: srcs.map((s) => ({ source_id: s.id, authorized: authorized(m[1], s.id), granted_at: proj(m[1]).grants[s.id]?.granted_at || null, granted_by: proj(m[1]).grants[s.id]?.granted_by || null })),
  }));
  on('PUT', '/projects/([^/]+)/evidence/grants/([^/]+)', ({ m, body, user }) => {
    const s = srcOf(m[2]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    const g = { authorized: !!body.authorized, granted_at: new Date().toISOString(), granted_by: user.username };
    proj(m[1]).grants[s.id] = g;
    return json(200, { source_id: s.id, ...g });
  });
  on('GET', '/projects/([^/]+)/evidence/summary', ({ m }) => {
    const p = proj(m[1]);
    const done = (st) => p.jobs.filter((j) => j.state === st).length;
    const finished = p.jobs.filter((j) => !['queued', 'running'].includes(j.state)).length;
    const last = p.snapshots.map((s) => s.ended_at).filter(Boolean).sort().pop() || null;
    return json(200, {
      authorized_sources: srcs.filter((s) => authorized(m[1], s.id)).map(D.sourceRef),
      snapshots: { count: p.snapshots.length, sources: new Set(p.snapshots.map((s) => s.source.id)).size, referenced: p.snapshots.filter((s) => s.referenced > 0).length, last_at: last },
      referenced_anchors: p.snapshots.reduce((n, s) => n + s.referenced, 0),
      jobs_this_month: { total: finished, succeeded: done('succeeded'), partial: done('partial'), failed: done('failed'), cancelled: done('cancelled') },
      running_jobs: done('running'), queued_jobs: done('queued'),
      gateway: { status: prov.gateway.status, providers_loaded: prov.items.filter((x) => x.installed).length },
    });
  });

  // ---------- EV-13～EV-17 采集 ----------
  function preview(pid, body) {
    const s = srcOf(body.source_id);
    const base = body.incremental_base;
    const scopeText = {
      snapshot: `${s.scope_summary.replace(/\s*\d+\s*表$/, '')}，42 张表${base ? `（增量，基于 ${base.split('-').pop()}）` : ''}`,
      discover: `${s.scope_summary.replace(/\s*\d+\s*表$/, '')}（只读元数据）`,
      sample: `${body.sample?.selector || '—'}，最多 ${Math.min(20, body.sample?.limit || 20)} 行`,
    }[body.operation] || s.scope_summary;
    const estimate = { snapshot: base ? { rows: 860000, bytes: 1.2 * 1024 ** 3 } : { rows: 1960000, bytes: 1.9 * 1024 ** 3 }, discover: null, sample: { rows: 20, bytes: 4096 } }[body.operation] ?? null;
    const production = s.environment === 'production';
    const token = production ? `ct-${uid()}${uid()}` : null;
    if (token) tokens.set(token, { source_id: s.id, pid, at: clock() });
    const w = s.access_window;
    return {
      requires_confirmation: production,
      confirmation_token: token,
      summary: {
        scope_text: scopeText, tables: 42, window: w ? `${w.start} – ${w.end}` : null, in_window_now: inWindow(w, clock()),
        rate_limit: s.rate_limit, estimate,
        readonly_methods: [`只读账号 ${s.connection?.username || ''}`.trim(), '只读会话', '语句白名单'],
        masking: { policy: s.masking_policy, sample_rows: 20 }, ticket: { ttl_s: 600, single_use: true },
      },
    };
  }
  // 生产库：token 必须有效且对得上数据源
  function consent(pid, s, token) {
    if (s.environment !== 'production') return null;
    const t = token && tokens.get(token);
    if (!t || t.source_id !== s.id) return 'missing';
    if (clock() - t.at > TOKEN_TTL) return 'expired';
    tokens.delete(token);
    return null;
  }
  const PHASES = [[15, '读取表清单'], [60, '采集字段与约束'], [85, '采集数据画像'], [101, '脱敏采样']];
  function run(pid, j) {
    const p = proj(pid);
    const step = () => {
      const cur = p.jobs.find((x) => x.id === j.id);
      if (!cur || cur.state !== 'running') return;
      const done = Math.min(cur.progress.total, cur.progress.done + Math.max(1, Math.ceil(cur.progress.total / 6)));
      const pct = Math.round((done / cur.progress.total) * 100);
      Object.assign(cur, { progress: { ...cur.progress, done, pct }, phase: PHASES.find(([x]) => pct < x)[1] });
      if (done < cur.progress.total) {
        emitJob(pid, { ...cur });
        timers.set(j.id, later(step, 900));
        return;
      }
      const ended = clock();
      Object.assign(cur, { state: 'succeeded', phase: null, ended_at: new Date(ended).toISOString(), duration_ms: ended - Date.parse(cur.started_at), actions: [] });
      if (cur.operation === 'snapshot') {
        const latest = p.snapshots.find((s) => s.complete && s.source.id === cur.source.id) || p.snapshots[0];
        const id = `${stamp(Date.parse(cur.started_at))}-${uid().slice(-4).padStart(4, '0')}`;
        p.snapshots.unshift({
          ...(latest || {}), id, source: cur.source, complete: true, coverage_pct: 100, referenced: 0, previous: latest?.id || null, job_id: cur.id,
          records: { ...(latest?.records || { entity: 42, field: 618 }), entity: 43, field: 621 }, started_at: cur.started_at, ended_at: cur.ended_at,
        });
        cur.result = { snapshot_id: id, message: `生成快照 ${id}` };
        cur.actions = ['browse'];
        if (cur.resumed_from) {
          const orig = p.jobs.find((x) => x.id === cur.resumed_from);
          if (orig) { orig.actions = []; emitJob(pid, { ...orig }); }
        }
      } else if (cur.operation === 'sample') cur.result = { message: `补采 ${cur.scope_text.split(' ')[0]} 样本 ${cur.progress.total * 20} 行` };
      else cur.result = { message: '发现 42 张表、621 个字段，已更新预估规模' };
      timers.delete(j.id);
      emitJob(pid, { ...cur });
    };
    timers.set(j.id, later(step, 900));
  }
  function create(pid, s, body, { user, resumedFrom = null, startDone = 0 } = {}) {
    const p = proj(pid);
    const now = clock();
    const open = inWindow(s.access_window, now);
    const total = body.operation === 'sample' ? 1 : 42;
    const j = {
      id: `job-${String(jobSeq++).padStart(4, '0')}`, project_id: pid, source: D.sourceRef(s), operation: body.operation,
      scope_text: body.operation === 'sample' ? `${body.sample?.selector || 'replenish_std'} ${Math.min(20, body.sample?.limit || 20)} 行` : body.operation === 'snapshot' ? `42 表${body.incremental_base ? '（增量）' : ''}` : '',
      incremental_base: body.incremental_base || null, state: open ? 'running' : 'queued', phase: open ? '读取表清单' : null,
      progress: { done: startDone, total, unit: '表', pct: Math.round((startDone / total) * 100) },
      queue_reason: open ? null : `等待访问窗口 ${s.access_window.start}`,
      started_at: open ? new Date(now).toISOString() : null, ended_at: null, duration_ms: null, result: null, error: null,
      resumed_from: resumedFrom, checkpoint: false, initiated_by: user.username,
      consent: s.environment === 'production' ? { by: user.username, at: new Date(now).toISOString() } : null, actions: ['cancel'],
    };
    p.jobs.unshift(j);
    emitJob(pid, { ...j });
    if (open) run(pid, j);
    return j;
  }

  on('POST', '/projects/([^/]+)/evidence/jobs/preview', async ({ m, body }) => {
    const s = srcOf(body.source_id);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    if (!authorized(m[1], s.id)) return err(403, 'source_not_authorized', '本项目没有获授权使用该数据源');
    if (!['discover', 'snapshot', 'sample'].includes(body.operation)) return err(400, 'invalid_argument', '操作不对', { fields: { operation: 'enum' } });
    if (body.operation === 'sample' && !body.sample?.selector) return err(400, 'invalid_argument', '补采样本要写表名', { fields: { 'sample.selector': 'required' } });
    await wait(200);
    return json(200, preview(m[1], body));
  });
  on('POST', '/projects/([^/]+)/evidence/jobs', async ({ m, body, user }) => {
    const pid = m[1];
    const s = srcOf(body.source_id);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    if (!authorized(pid, s.id)) return err(403, 'source_not_authorized', '本项目没有获授权使用该数据源');
    const c = consent(pid, s, body.confirmation_token);
    if (c === 'expired') return err(410, 'confirmation_expired', '确认已过期');
    if (c === 'missing') return err(428, 'confirmation_required', '生产环境的采集要先确认', preview(pid, body));
    await wait(150);
    return json(202, create(pid, s, body, { user }));
  });
  on('GET', '/projects/([^/]+)/evidence/jobs', ({ m, query }) => {
    const st = query.get('state');
    let items = proj(m[1]).jobs;
    if (st === 'active') items = items.filter((j) => ['queued', 'running'].includes(j.state));
    else if (st === 'finished') items = items.filter((j) => !['queued', 'running'].includes(j.state));
    else if (st) items = items.filter((j) => j.state === st);
    return json(200, { items, next_cursor: null });
  });
  on('GET', '/projects/([^/]+)/evidence/jobs/([^/]+)', ({ m }) => {
    const j = proj(m[1]).jobs.find((x) => x.id === m[2]);
    return j ? json(200, j) : err(404, 'job_not_found', '采集任务不存在');
  });
  on('POST', '/projects/([^/]+)/evidence/jobs/([^/]+)/(cancel|resume|retry)', async ({ m, body, user }) => {
    const [pid, id, op] = [m[1], m[2], m[3]];
    const p = proj(pid);
    const j = p.jobs.find((x) => x.id === id);
    if (!j) return err(404, 'job_not_found', '采集任务不存在');
    const s = srcOf(j.source.id);
    if (op === 'cancel') {
      if (!['queued', 'running'].includes(j.state)) return err(409, 'job_state_invalid', '任务已结束', { state: j.state });
      clearTimeout(timers.get(id));
      timers.delete(id);
      const now = clock();
      const d = new Date(now);
      Object.assign(j, {
        state: 'cancelled', phase: null, ended_at: d.toISOString(), duration_ms: j.started_at ? now - Date.parse(j.started_at) : null,
        result: { message: `你在 ${pad(d.getHours())}:${pad(d.getMinutes())} 取消` }, actions: ['retry'],
      });
      emitJob(pid, { ...j });
      return json(200, j);
    }
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    if (op === 'resume') {
      if (j.state !== 'partial' || !j.actions.includes('resume')) return err(409, 'job_state_invalid', '只有部分完成的任务能续采', { state: j.state });
      await wait(150);
      const next = create(pid, s, { operation: j.operation, source_id: s.id }, { user, resumedFrom: j.id, startDone: j.progress?.done || 33 });
      j.actions = [];
      emitJob(pid, { ...j });
      return json(200, next);
    }
    if (!['failed', 'cancelled'].includes(j.state)) return err(409, 'job_state_invalid', '只有失败或已取消的任务能重试', { state: j.state });
    const again = { source_id: s.id, operation: j.operation, incremental_base: j.incremental_base, sample: j.operation === 'sample' ? { selector: j.scope_text.split(' ')[0], limit: 20 } : undefined };
    const c = consent(pid, s, body.confirmation_token);
    if (c === 'expired') return err(410, 'confirmation_expired', '确认已过期');
    if (c === 'missing') return err(428, 'confirmation_required', '生产环境的采集要先确认', preview(pid, again));
    await wait(150);
    return json(200, create(pid, s, again, { user }));
  });

  // ---------- EV-18～EV-22 快照 ----------
  // 对比要先登记（diff 也能被当成快照 ID 匹配）
  on('GET', '/projects/([^/]+)/evidence/snapshots/diff', ({ m, query }) => {
    const p = proj(m[1]);
    const [from, to] = [query.get('from'), query.get('to')];
    const a = p.snapshots.find((s) => s.id === from);
    const b = p.snapshots.find((s) => s.id === to);
    if (!a || !b) return err(404, 'snapshot_not_found', '快照不存在');
    if (a.source.id !== b.source.id) return err(400, 'invalid_argument', '只能对比同一数据源的两份快照', { fields: { to: 'same_source' } });
    const all = D.DIFFS[`${from}>${to}`] || [];
    const change = query.get('change');
    const n = (c) => all.filter((x) => x.change === c).length;
    return json(200, {
      from, to,
      counts: { added: n('added'), removed: n('removed'), modified: n('modified'), touching_confirmed: all.filter((x) => x.affected.length).length },
      items: change ? all.filter((x) => x.change === change) : all, next_cursor: null,
    });
  });
  on('POST', '/projects/([^/]+)/evidence/snapshots/diff/suggestions', async ({ body }) => {
    const items = (D.DIFFS[`${body.from}>${body.to}`] || []).filter((x) => x.affected.length);
    await wait(300);
    return json(200, { created: Math.min(3, items.length), inbox_item_ids: items.slice(0, 3).map((x, i) => `suggestion:${body.to.split('-').pop()}-${i + 1}`) });
  });
  on('GET', '/projects/([^/]+)/evidence/snapshots', ({ m, query }) => {
    const sid = query.get('source_id');
    const items = proj(m[1]).snapshots.filter((s) => !sid || s.source.id === sid);
    return json(200, { items, next_cursor: null });
  });
  on('GET', '/projects/([^/]+)/evidence/snapshots/([^/]+)', ({ m }) => {
    const s = proj(m[1]).snapshots.find((x) => x.id === m[2]);
    if (!s) return err(404, 'snapshot_not_found', '快照不存在');
    return json(200, { ...s, manifest: { provider: s.provider, masking_policy: s.masking_policy, scope: ['erp.*'] }, inventory: { collected: s.records.entity, skipped: s.complete ? [] : [{ object: 'erp.arrival_record 等 9 张表', reason: '预算用尽' }] } });
  });
  on('DELETE', '/projects/([^/]+)/evidence/snapshots/([^/]+)', ({ m }) => {
    const p = proj(m[1]);
    const s = p.snapshots.find((x) => x.id === m[2]);
    if (!s) return err(404, 'snapshot_not_found', '快照不存在');
    if (s.referenced > 0) return err(409, 'snapshot_referenced', '快照被已确认产物引用，不能清理', { references: s.referenced });
    p.snapshots.splice(p.snapshots.indexOf(s), 1);
    return json(200, { removed: true, blob_retention_days: 90 });
  });

  // ---------- EV-23～EV-28 证据浏览器（EV-26 在 routes-studio.js） ----------
  const snapOf = (pid, id) => proj(pid).snapshots.find((s) => s.id === id);
  const fieldsAt = (t, snapshot) => t.fields.filter((x) => (x.gen === 'old' ? !D.isNewGen(snapshot) : x.gen === 'new' ? D.isNewGen(snapshot) : true))
    .map((x) => ({ ...x, note: x.oldNote && !D.isNewGen(snapshot) ? x.oldNote : x.note }));
  const mappedRef = (key) => (key ? entryRef(key) : null);
  const entityItem = (name) => ({ anchor: tableAnchor(name), name: `erp.${name}`, label: D.TABLES[name].label, mapped_to: mappedRef(D.TABLES[name].mapped) });
  const needSnap = (pid, query) => {
    const id = query.get('snapshot');
    const s = snapOf(pid, id) || (!id ? proj(pid).snapshots.find((x) => x.complete) : null);
    return s || null;
  };

  on('GET', '/projects/([^/]+)/evidence/summary-tree', ({ m, query }) => {
    const s = needSnap(m[1], query);
    if (!s) return err(404, 'snapshot_not_found', '快照不存在');
    return json(200, {
      snapshot: { id: s.id, source_id: s.source.id, complete: s.complete },
      domains: D.DOMAINS.map(([name, count, list]) => ({ name, count, entities: list.map(entityItem) })),
    });
  });
  on('GET', '/projects/([^/]+)/evidence/search', ({ m, query }) => {
    const s = needSnap(m[1], query);
    if (!s) return err(404, 'snapshot_not_found', '快照不存在');
    const q = (query.get('q') || '').trim().toLowerCase();
    const type = query.get('type');
    const items = [];
    if (q) {
      Object.entries(D.TABLES).forEach(([name, t]) => {
        if ((!type || type === 'entity') && (name.includes(q) || t.label.toLowerCase().includes(q))) {
          items.push({ anchor: tableAnchor(name), type: 'entity', name: `erp.${name}`, label: t.label, parent_anchor: null, snippet: `${t.domain} · ${fieldsAt(t, s.id).length} 个字段`, confidence: 'declared' });
        }
        if (!type || type === 'field') {
          fieldsAt(t, s.id).forEach((x) => {
            if (x.name.includes(q) || (x.note || '').toLowerCase().includes(q)) {
              items.push({ anchor: `${tableAnchor(name)}.${x.name}`, type: 'field', name: x.name, label: x.note === 'PK' ? '主键' : x.note, parent_anchor: tableAnchor(name), snippet: `erp.${name} · ${x.type}`, confidence: x.confidence });
            }
          });
        }
      });
    }
    return json(200, { items: items.slice(0, 50), next_cursor: null, total: items.length });
  });
  on('GET', '/projects/([^/]+)/evidence/record', ({ m, query }) => {
    const anchor = query.get('anchor') || '';
    const a = splitAnchor(anchor);
    if (!a) return err(400, 'anchor_invalid', '锚点格式不合法');
    const s = needSnap(m[1], query);
    const t = D.TABLES[a.table];
    if (!s || !t || a.source !== s.source.id) return err(404, 'anchor_not_found', '锚点在所声明的快照里不存在');
    const fields = fieldsAt(t, s.id).map((x) => ({ name: x.name, type: x.type, note: x.note, confidence: x.confidence, masked: x.masked, anchor: `${tableAnchor(a.table)}.${x.name}` }));
    const snapshot = { id: s.id, source_id: s.source.id, complete: s.complete };
    if (a.field) {
      const x = fields.find((y) => y.name === a.field);
      if (!x) return err(404, 'anchor_not_found', '锚点在所声明的快照里不存在');
      return json(200, { anchor: x.anchor, type: 'field', name: `erp.${a.table}.${x.name}`, label: x.note, parent_anchor: tableAnchor(a.table), confidence: x.confidence, evidence: { provider: s.provider, method: 'information_schema' }, attrs: { data_type: x.type }, snapshot });
    }
    const extras = a.table === 'replenish_std' ? D.replenishStdExtras(s.id) : {
      ddl: `CREATE TABLE ${a.table} (\n${fields.map((x) => `  ${x.name} ${x.type}${x.note === 'PK' ? ' PRIMARY KEY' : ''}`).join(',\n')}\n);`,
      samples: { columns: fields.slice(0, 3).map((x) => x.name), rows: [], shown: 0, max: 20 },
      profile: [],
    };
    return json(200, {
      anchor: tableAnchor(a.table), type: 'entity', name: `erp.${a.table}`, label: t.label, parent_anchor: null, confidence: 'declared',
      evidence: { provider: s.provider, method: 'information_schema' }, attrs: { domain: t.domain }, fields, rows: t.rows, snapshot, ...extras,
    });
  });
  const relationsOf = (names) => D.RELATIONS
    .map(([a, b, confidence, basis]) => ({ a, b, confidence, basis, ta: a.split('.')[0], tb: b.split('.')[0] }))
    .filter((r) => names.includes(r.ta) && names.includes(r.tb));
  on('GET', '/projects/([^/]+)/evidence/lineage', ({ m, query }) => {
    const a = splitAnchor(query.get('anchor'));
    if (!a || !D.TABLES[a.table]) return err(404, 'anchor_not_found', '锚点不存在');
    const seen = new Set();
    const items = [];
    relationsOf(Object.keys(D.TABLES)).forEach((r) => {
      const other = r.ta === a.table ? r.tb : r.tb === a.table ? r.ta : null;
      if (!other || seen.has(other)) return;
      seen.add(other);
      items.push({ anchor: tableAnchor(other), label: `erp.${other}`, basis: r.basis, confidence: r.confidence });
    });
    return json(200, { items });
  });
  on('GET', '/projects/([^/]+)/evidence/er', ({ m, query }) => {
    const s = needSnap(m[1], query);
    if (!s) return err(404, 'snapshot_not_found', '快照不存在');
    const focus = splitAnchor(query.get('focus'))?.table || 'replenish_std';
    const depth = Math.max(1, Math.min(3, Number(query.get('depth') || 1)));
    let names;
    if (D.ER_GROUP.includes(focus)) names = D.ER_GROUP;
    else {
      names = [focus];
      for (let i = 0; i < depth; i += 1) {
        relationsOf(Object.keys(D.TABLES)).forEach((r) => {
          if (names.includes(r.ta) && !names.includes(r.tb)) names.push(r.tb);
          if (names.includes(r.tb) && !names.includes(r.ta)) names.push(r.ta);
        });
      }
    }
    return json(200, {
      tables: names.slice(0, 60).map((n) => ({
        anchor: tableAnchor(n), name: n, label: D.TABLES[n].label,
        fields: fieldsAt(D.TABLES[n], s.id).map((x) => ({ name: x.name, type: x.type, pk: x.pk, fk: x.fk, masked: x.masked })),
      })),
      relations: relationsOf(names).map((r) => ({ from_anchor: `db:erp-prod/erp.${r.a}`, to_anchor: `db:erp-prod/erp.${r.b}`, confidence: r.confidence, basis: r.basis })),
    });
  });

  // ---------- EV-30 ----------
  on('POST', '/evidence/sources/([^/]+)/notify-admin', ({ m, body }) => {
    const s = srcOf(m[1]);
    if (!s) return err(404, 'source_not_found', '数据源不存在');
    if (!body.project_id || !authorized(body.project_id, s.id)) return err(403, 'source_not_authorized', '本项目没有获授权使用该数据源');
    return json(202, { request_id: `req-${uid()}` });
  });

  // 「在对话里分析影响」（S-20）：按三步推进
  commandHooks.push({
    match: (b) => b.command === '/ontology-evidence-impact',
    title: (b) => `助手正在分析快照 ${String(b.args?.from || '').split('-').pop()} → ${String(b.args?.to || '').split('-').pop()} 的影响`,
    steps: ['比对两份快照的变化', '定位涉及的已确认产物', '整理建议修订，等你在对话里确认'],
  });
}

// ?mock=task:snapshots：打开采集与快照页时，「在对话里分析影响」正在跑
export const taskSeeds = {
  snapshots: () => ({
    origin: { page: 'snapshots', label: '采集与快照' }, title: `助手正在分析快照 ${D.SNAP.s9c.split('-').pop()} → ${D.SNAP.s5e.split('-').pop()} 的影响`,
    percent: 45, step_label: '定位涉及的已确认产物',
  }),
};
