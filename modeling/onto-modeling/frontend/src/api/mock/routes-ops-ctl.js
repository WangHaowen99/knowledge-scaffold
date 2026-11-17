// mock：运维管控与健康（O-13～O-47，含启动自检报告 O-35）。页面：ops-ctl（O-35 也供 ops 的摘要、左栏角标用 O-13）
// 运维面路径登记成 '/ops/...'（客户端按 ops_base_url 拼，mock 只看路径）；角色用 ctx.forbid 把关：读 ops、admin，写只有 ops（O-37 立即备份 admin 也可）。
// 地址参数 ?mock=ctl:<状态>[+<状态>]：
//   ctl:ready     依赖全部就绪（左栏不出「降级」角标）
//   ctl:notready  模型网关不可用，就绪为 not_ready（左栏「未就绪」）
//   ctl:nosess    会话池是空的（「当前没有活跃会话」）
// 维护状态：缺省不在维护；?mock=maint:readonly / maint:reject 时一打开就在维护的「排空」这一步（与外壳横幅同一份）。
// 向导里进入、推进、退出维护会发用户流 maintenance.updated，横幅跟着变；但请求是否被只读拦截只看启动时的 maint: 参数（见 gaps）。
import * as D from './ops-ctl-data.js';

export function register(ctx) {
  const { on, json, err, forbid, sc, emitUser } = ctx;
  const flags = new Set(String(sc.ctl || '').split(/[+\s]+/).filter(Boolean)); // 地址里的 + 会解码成空格
  const t0 = Date.now();
  const s = {
    health: D.healthData(t0, flags.has('ready') ? 'ready' : flags.has('notready') ? 'not_ready' : 'degraded'),
    probes: 0,
    pool: D.sessionPool(t0, flags.has('nosess') ? 'empty' : null),
    maint: null,
    packs: D.opsPacks(),
    core: D.coreDefault(t0),
    repo: D.repoBackups(t0),
    startup: D.startupReport(t0),
    backups: D.backupsData(t0),
    drills: D.drills(t0),
    tasks: D.tasks(t0),
    versions: D.versions(),
    retention: D.retention(t0),
    diags: D.diagnostics(t0),
    seq: 0,
  };
  D.stampIdle(s.pool, t0);
  if (ctx.maintenance?.active) s.maint = D.maintenanceFromScenario(ctx.maintenance, s.pool, t0);
  const nid = (p) => `${p}-${(s.seq += 1).toString(36)}${Date.now().toString(36).slice(-4)}`;

  const read = (user) => forbid(user, ['ops', 'admin']);
  const write = (user) => forbid(user, ['ops']);
  const invalid = (fields, message = '参数不合法') => err(400, 'invalid_argument', message, { fields });

  // 每次请求前按时间推进：任务完成、维护排空 / 升级、熔断半开
  function tick() {
    const now = Date.now();
    D.tickTasks(s.tasks, now).forEach((t) => t.onDone?.(t, now));
    if (s.maint?.active) {
      D.refreshMaintenance(s.maint, s.pool, now);
      if (s.maint.upgraded && s.maint.target && !s.maint.upgrade_applied) {
        D.setCoreStable(s.core, s.packs, s.maint.target.version);
        s.maint.upgrade_applied = true;
      }
    } else D.tickPool(s.pool, now);
    D.tickBreakers(s.packs, now);
    return now;
  }
  const emitMaint = () => emitUser('maintenance.updated', D.publicMaintenance(s.maint));

  function startTask(type, target, initiator, { project = null, onDone, ms = D.TASK_MS } = {}) {
    const now = Date.now();
    const t = {
      id: nid('task'), type, target, project, initiator, state: 'running', progress_pct: 0,
      started_at: new Date(now).toISOString(), finished_at: null, error: null, done_at: now + ms, onDone,
    };
    s.tasks.unshift(t);
    return t;
  }
  const taskOut = ({ onDone: _o, ...t }) => D.publicTask(t);

  // ---------- 健康（O-13、O-14） ----------
  on('GET', '/ops/health', ({ user }) => read(user) || (tick(), json(200, s.health)));
  on('POST', '/ops/health/probe', async ({ user }) => {
    const deny = write(user);
    if (deny) return deny;
    await ctx.wait(500);
    s.probes += 1;
    s.health = { ...s.health, dependencies: D.probe(s.health.dependencies, s.probes) };
    return json(200, s.health.dependencies);
  });

  // ---------- 会话池（O-15～O-19） ----------
  on('GET', '/ops/sessions', ({ user }) => read(user) || json(200, D.poolView(s.pool, tick())));
  on('PUT', '/ops/sessions/config', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const fields = {};
    if (!Number.isInteger(body.max_sessions) || body.max_sessions < 1 || body.max_sessions > 64) fields.max_sessions = 'out_of_range';
    if (!Number.isInteger(body.idle_ttl_s) || body.idle_ttl_s < 60 || body.idle_ttl_s > 86400) fields.idle_ttl_s = 'out_of_range';
    if (!['evict_idle_then_queue', 'queue_only'].includes(body.overflow_policy)) fields.overflow_policy = 'invalid';
    if (Object.keys(fields).length) return invalid(fields, '会话池配置不合法');
    s.pool.config = { max_sessions: body.max_sessions, idle_ttl_s: body.idle_ttl_s, overflow_policy: body.overflow_policy };
    return json(200, { config: { ...s.pool.config } });
  });
  const sessionOf = (pid) => s.pool.items.find((x) => x.project.id === pid && x.state !== 'queued');
  on('POST', '/ops/sessions/([^/]+)/recycle', ({ user, m }) => {
    const deny = write(user);
    if (deny) return deny;
    const now = tick();
    const it = sessionOf(decodeURIComponent(m[1]));
    if (!it) return err(404, 'session_not_found', '该项目没有在线会话');
    if (it.state === 'running') return err(409, 'session_running', '运行中的会话不能回收，只能强制断开');
    s.pool.items = s.pool.items.filter((x) => x !== it);
    const v = D.poolView(s.pool, now);
    return json(200, { recycled: true, pool: { config: v.config, active: v.active, queued: v.queued } });
  });
  on('POST', '/ops/sessions/([^/]+)/disconnect', ({ user, m, body }) => {
    const deny = write(user);
    if (deny) return deny;
    if (!String(body.reason || '').trim()) return invalid({ reason: 'required' }, '请写明断开原因');
    tick();
    const it = sessionOf(decodeURIComponent(m[1]));
    if (!it) return err(404, 'session_not_found', '该项目没有在线会话');
    s.pool.items = s.pool.items.filter((x) => x !== it);
    return json(200, { disconnected: true });
  });
  on('POST', '/ops/sessions/drain', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const wait = body.max_wait_s ?? 600;
    if (!Number.isInteger(wait) || wait < 1 || wait > 86400) return invalid({ max_wait_s: 'out_of_range' });
    tick();
    return json(202, D.startDrain(s.pool, Date.now()));
  });

  // ---------- 维护模式（O-20～O-24） ----------
  on('GET', '/ops/maintenance', ({ user }) => read(user) || (tick(), json(200, D.publicMaintenance(s.maint))));
  on('POST', '/ops/maintenance', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    if (s.maint?.active) return err(409, 'maintenance_active', '已在维护模式');
    const fields = {};
    if (!['reject_new_sessions', 'read_only'].includes(body.mode)) fields.mode = 'invalid';
    if (!String(body.message || '').trim()) fields.message = 'required';
    if (body.target && body.target.kind !== 'core_upgrade') fields.target = 'unsupported';
    if (body.target && !s.core.versions.some((v) => v.version === body.target.version)) fields.target = 'version_not_found';
    if (Object.keys(fields).length) return invalid(fields, '维护设置不合法');
    s.maint = D.enterMaintenance(s.pool, body, { now: Date.now(), user: user.username });
    emitMaint();
    return json(201, D.publicMaintenance(s.maint));
  });
  on('POST', '/ops/maintenance/advance', ({ user }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    if (!s.maint?.active) return err(409, 'maintenance_not_active', '不在维护模式');
    const r = D.advanceMaintenance(s.maint, s.pool, Date.now());
    if (r.error) return err(409, 'maintenance_step_blocked', r.error.step === 'resume' ? '已经是最后一步，用退出维护模式恢复' : '当前步骤没完成，不能进入下一步', r.error);
    emitMaint();
    return json(200, D.publicMaintenance(s.maint));
  });
  on('POST', '/ops/maintenance/force-disconnect', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    if (!String(body.reason || '').trim()) return invalid({ reason: 'required' }, '请写明断开原因');
    tick();
    if (!s.maint?.active) return err(409, 'maintenance_not_active', '不在维护模式');
    D.forceDrain(s.pool);
    D.refreshMaintenance(s.maint, s.pool, Date.now());
    emitMaint();
    return json(200, D.publicMaintenance(s.maint));
  });
  on('DELETE', '/ops/maintenance', ({ user }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    if (!s.maint?.active) return err(409, 'maintenance_not_active', '不在维护模式');
    s.maint = null;
    s.pool.drain = null;
    emitMaint();
    return json(200, { active: false });
  });

  // ---------- 插件运维（O-25～O-29） ----------
  on('GET', '/ops/packs', ({ user }) => read(user) || (tick(), json(200, { items: s.packs.map(D.publicPack) })));
  const packOf = (name, version) => s.packs.find((x) => x.name === name && x.version === version);
  const notFound = () => err(404, 'pack_version_not_found', '包或版本不存在');
  on('PUT', '/ops/packs/([^/]+)/([^/]+)/rollout', ({ user, m, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const p = packOf(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
    if (!p) return notFound();
    if (!Number.isInteger(body.percent) || body.percent < 0 || body.percent > 100) return invalid({ percent: 'out_of_range' });
    if (body.by && !['project', 'tenant'].includes(body.by)) return invalid({ by: 'invalid' });
    Object.assign(p, { rollout_pct: body.percent, rollout_by: body.by || p.rollout_by });
    if (p.name === 'core' && s.core.beta?.version === p.version) s.core.beta.rollout_pct = body.percent;
    return json(200, D.publicPack(p));
  });
  on('POST', '/ops/packs/([^/]+)/rollback', ({ user, m, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const name = decodeURIComponent(m[1]);
    const p = s.packs.find((x) => x.name === name && x.channel === body.channel);
    if (!p) return notFound();
    if (!body.to_version) return invalid({ to_version: 'required' });
    if (name === 'core' && p.channel === 'stable') {
      if (!s.core.versions.some((v) => v.version === body.to_version)) return notFound();
      D.setCoreStable(s.core, s.packs, body.to_version);
      return json(200, D.publicPack(s.packs.find((x) => x.name === 'core' && x.channel === 'stable')));
    }
    Object.assign(p, { version: body.to_version, rollback_to: null, error_rate_pct: 0, breaker: 'closed' });
    return json(200, D.publicPack(p));
  });
  on('POST', '/ops/packs/([^/]+)/([^/]+)/disable', ({ user, m, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const p = packOf(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
    if (!p) return notFound();
    if (!String(body.reason || '').trim()) return invalid({ reason: 'required' }, '请写明禁用原因');
    // 禁用后不能再被新项目启用：mock 里表现为退出通道（channel=retired、放量 0）
    Object.assign(p, { channel: 'retired', rollout_pct: 0 });
    return json(200, D.publicPack(p));
  });
  on('POST', '/ops/packs/([^/]+)/([^/]+)/breaker', ({ user, m, body }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    const p = packOf(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
    if (!p) return notFound();
    if (body.action === 'open') {
      if (p.breaker === 'open') return err(409, 'breaker_state_invalid', '已经熔断', { state: p.breaker });
      Object.assign(p, { breaker: 'open', half_open_at: null });
    } else if (body.action === 'close') {
      if (p.breaker !== 'open') return err(409, 'breaker_state_invalid', '没有熔断，不需要恢复', { state: p.breaker });
      Object.assign(p, { breaker: 'half_open', half_open_at: Date.now() });
    } else return invalid({ action: 'invalid' });
    return json(200, D.publicPack(p));
  });

  // ---------- core 默认版本与包仓库备份（O-30～O-34） ----------
  const coreOut = () => ({ ...s.core, beta: s.core.beta ? { ...s.core.beta } : null, versions: s.core.versions.map((v) => ({ ...v })) });
  on('GET', '/ops/core-default', ({ user }) => read(user) || (tick(), json(200, coreOut())));
  on('POST', '/ops/core-default/promote', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const v = s.core.versions.find((x) => x.version === body.version);
    if (!v) return notFound();
    D.setCoreStable(s.core, s.packs, body.version);
    return json(200, coreOut());
  });
  function pushRepoBackup(now) {
    s.repo.unshift({ name: D.repoBackupName(now), size: 405 * 1024 ** 2, created_at: new Date(now).toISOString() });
    s.repo = s.repo.slice(0, D.REPO_KEEP);
  }
  on('POST', '/ops/core-default/rollback', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const v = s.core.versions.find((x) => x.version === body.version);
    if (!v) return notFound();
    pushRepoBackup(Date.now()); // 回滚前先备份包仓库
    D.setCoreStable(s.core, s.packs, body.version);
    return json(200, coreOut());
  });
  on('GET', '/ops/pack-repo/backups', ({ user }) => read(user) || (tick(), json(200, { items: s.repo.map((x) => ({ ...x })), keep: D.REPO_KEEP })));
  on('POST', '/ops/pack-repo/backups', ({ user }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    const t = startTask('pack_repo_backup', '包仓库 · 手动备份', user.username, { ms: 3000, onDone: (_t, now) => pushRepoBackup(now) });
    return json(202, { task_id: t.id });
  });

  // ---------- 启动自检（O-35） ----------
  on('GET', '/ops/startup-report', ({ user }) => read(user) || json(200, s.startup));

  // ---------- 备份与恢复演练（O-36～O-39） ----------
  const latestDrill = () => s.drills.slice().sort((a, b) => b.at.localeCompare(a.at))[0] || null;
  on('GET', '/ops/backups', ({ user }) => read(user) || (tick(), json(200, { ...s.backups, items: s.backups.items.map((x) => ({ ...x })), last_drill: latestDrill() })));
  on('POST', '/ops/backups', ({ user }) => {
    const deny = forbid(user, ['ops', 'admin']);
    if (deny) return deny;
    tick();
    if (s.tasks.some((x) => x.type === 'backup' && x.state === 'running')) return err(409, 'backup_in_progress', '已有备份在跑');
    const name = D.backupName(Date.now());
    const t = startTask('backup', '手动备份 · 全量范围', user.username, {
      onDone: (_t, now) => {
        s.backups.items.unshift({ name, size: 14.3 * 1024 ** 3, created_at: new Date(now).toISOString(), scope: 'full', scope_items: [...s.backups.scope] });
        s.backups.items = s.backups.items.slice(0, s.backups.schedule.keep);
      },
    });
    return json(202, { task_id: t.id, name });
  });
  on('POST', '/ops/backups/drills', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    const b = s.backups.items.find((x) => x.name === body.backup);
    if (!b) return err(404, 'not_found', '备份不存在');
    const n = body.sample_projects ?? 5;
    if (!Number.isInteger(n) || n < 1 || n > 50) return invalid({ sample_projects: 'out_of_range' });
    const t = startTask('drill', `恢复演练 · ${b.name}`, user.username, {
      ms: 6000,
      onDone: (task, now) => s.drills.push({
        id: nid('drill'), backup: b.name, at: new Date(now).toISOString(), duration_ms: now - Date.parse(task.started_at), result: 'passed',
        detail: `抽检 ${n} 个项目可打开、版本历史完整`,
      }),
    });
    return json(202, { task_id: t.id });
  });
  on('GET', '/ops/backups/drills', ({ user }) => read(user) || (tick(), json(200, { items: s.drills.slice().sort((a, b) => b.at.localeCompare(a.at)), next_cursor: null })));

  // ---------- 任务中心（O-40、O-41） ----------
  on('GET', '/ops/tasks', ({ user, query }) => {
    const deny = read(user);
    if (deny) return deny;
    tick();
    const types = (query.get('type') || '').split(',').filter(Boolean);
    const states = (query.get('state') || '').split(',').filter(Boolean);
    const items = s.tasks.filter((x) => (!types.length || types.includes(x.type)) && (!states.length || states.includes(x.state)))
      .sort((a, b) => b.started_at.localeCompare(a.started_at)).map(taskOut);
    return json(200, { items, next_cursor: null });
  });
  on('POST', '/ops/tasks/([^/]+)/(cancel|retry)', ({ user, m }) => {
    const deny = write(user);
    if (deny) return deny;
    tick();
    const t = s.tasks.find((x) => x.id === decodeURIComponent(m[1]));
    if (!t) return err(404, 'task_not_found', '任务不存在');
    const op = m[2];
    const now = Date.now();
    if (op === 'cancel') {
      if (!['queued', 'running'].includes(t.state)) return err(409, 'task_state_invalid', '任务已结束', { state: t.state });
      Object.assign(t, { state: 'cancelled', finished_at: new Date(now).toISOString(), done_at: null });
      return json(200, taskOut(t));
    }
    if (!['failed', 'partial'].includes(t.state)) return err(409, 'task_state_invalid', '任务状态不允许该操作', { state: t.state });
    Object.assign(t, {
      state: 'running', progress_pct: 0, error: null, finished_at: null,
      started_at: new Date(now).toISOString(), done_at: now + D.TASK_MS, done_state: 'succeeded',
    });
    return json(200, taskOut(t));
  });

  // ---------- 版本清单、数据保留（O-42～O-44） ----------
  on('GET', '/ops/versions', ({ user }) => {
    const deny = read(user);
    if (deny) return deny;
    tick();
    const items = s.versions.map((x) => (x.component === 'core'
      ? { ...x, version: s.core.beta ? `${s.core.stable} · ${s.core.beta.version} beta` : s.core.stable } : { ...x }));
    return json(200, { items });
  });
  on('GET', '/ops/retention', ({ user }) => read(user) || json(200, { items: s.retention.map((x) => ({ ...x, retention: { ...x.retention } })) }));
  on('PUT', '/ops/retention', ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const fields = D.retentionProblems(s.retention, body.items);
    if (Object.keys(fields).length) return invalid(fields, '保留期不合法');
    (body.items || []).forEach((p) => { s.retention.find((x) => x.id === p.id).retention = { ...p.retention }; });
    return json(200, { items: s.retention.map((x) => ({ ...x, retention: { ...x.retention } })) });
  });

  // ---------- 诊断包（O-45～O-47） ----------
  on('POST', '/ops/diagnostics', async ({ user, body }) => {
    const deny = write(user);
    if (deny) return deny;
    const include = Array.isArray(body.include) && body.include.length ? body.include : D.DIAG_INCLUDE;
    if (include.some((x) => !D.DIAG_INCLUDE.includes(x))) return invalid({ include: 'invalid' });
    await ctx.wait(600);
    const now = Date.now();
    const d = {
      id: nid('diag'), name: D.diagName(now), size: D.diagSize(include), created_at: new Date(now).toISOString(),
      created_by: user.username, expires_at: new Date(now + 7 * 24 * 3600e3).toISOString(),
    };
    s.diags.unshift(d);
    return json(201, d);
  });
  on('GET', '/ops/diagnostics', ({ user }) => read(user) || json(200, { items: s.diags.map((x) => ({ ...x })) }));
  on('GET', '/ops/diagnostics/([^/]+)/download', ({ user, m }) => {
    const deny = read(user);
    if (deny) return deny;
    const d = s.diags.find((x) => x.id === decodeURIComponent(m[1]));
    if (!d) return err(404, 'diagnostics_not_found', '诊断包不存在或已过期');
    return new Response(new Blob(['PK\u0005\u0006'.padEnd(22, '\u0000')]), {
      status: 200, headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${encodeURIComponent(d.name)}"` },
    });
  });
}
