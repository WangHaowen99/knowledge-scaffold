// 运维管控与健康（ops-ctl）：页面纯逻辑与 mock（O-13～O-47）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import * as D from '../src/api/mock/ops-ctl-data.js';
import { register } from '../src/api/mock/routes-ops-ctl.js';
import {
  coreRows, currentStep, defaultMaintMessage, depView, diagDefaults, drillView, diagNote, drainSummary, errorRateView, exitText, fmtBytes,
  fmtBytesPair, fmtDuration, fmtMs, fmtStamp, fmtWhen, idleText, liveView, maintActions, maintBody, maintErrorText, maintProblem,
  maintSteps, maintTitle, nameOf, packActions, poolBody, poolDirty, poolForm, readyView, reloadsAfter, retentionBody, retentionForm,
  retentionText, rolloutProblem, scheduleText, sectionOf, sessionActions, sessionSub, startupSub, taskAction, taskActionToast, validatePool,
  validateRetention, visibleInventory, visiblePacks, visibleRetention, visibleTasks,
} from '../src/pages/ops/ctlModel.js';

const local = (...a) => new Date(...a).toISOString();

// ---------- 格式化 ----------
test('字节、时长、耗时的写法与设计稿一致', () => {
  assert.equal(fmtBytes(412 * 1024 ** 2), '412 MB');
  assert.equal(fmtBytes(14.2 * 1024 ** 3), '14.2 GB');
  assert.equal(fmtBytes(1.8 * 1024 ** 4), '1.8 TB');
  assert.equal(fmtBytes(32 * 1024 ** 3), '32 GB');
  assert.equal(fmtBytes(512), '512 B');
  assert.equal(fmtBytes(null), '—');
  assert.equal(fmtBytesPair(21.4 * 1024 ** 3, 32 * 1024 ** 3), '21.4 / 32 GB');
  assert.equal(fmtDuration(4 * 60e3 + 10e3), '4 分 10 秒');
  assert.equal(fmtDuration(9 * 60e3 + 2e3), '9 分 02 秒');
  assert.equal(fmtDuration(2 * 3600e3 + 40 * 60e3), '2 小时 40 分');
  assert.equal(fmtDuration(3 * 3600e3), '3 小时');
  assert.equal(fmtDuration(45e3), '45 秒');
  assert.equal(fmtDuration(26 * 3600e3), '1 天 2 小时');
  assert.equal(fmtDuration(null), '—');
  assert.equal(fmtMs(1820), '1,820 ms');
  assert.equal(fmtMs(3), '3 ms');
});

test('时间：今天只写时刻、昨天写「昨天」、更早写日期；自检时间带秒', () => {
  const now = new Date(2026, 9, 31, 16, 0).getTime();
  assert.equal(fmtWhen(local(2026, 9, 31, 15, 30), now), '15:30');
  assert.equal(fmtWhen(local(2026, 9, 30, 22, 10), now), '昨天 22:10');
  assert.equal(fmtWhen(local(2026, 9, 29, 16, 2), now), '10-29 16:02');
  assert.equal(fmtStamp(local(2026, 9, 31, 8, 2, 11)), '10-31 08:02:11');
  assert.equal(nameOf({ zhangsan: '张工' }, 'zhangsan'), '张工');
  assert.equal(nameOf({}, 'system'), '系统');
  assert.equal(nameOf({}, 'liming'), 'liming');
});

test('分区参数：认标准名和几个别名，其余为 null', () => {
  assert.equal(sectionOf('startup'), 'startup');
  assert.equal(sectionOf('startup-report'), 'startup');
  assert.equal(sectionOf('Maint'), 'maintenance');
  assert.equal(sectionOf('pack-repo'), 'core');
  assert.equal(sectionOf('nope'), null);
  assert.equal(sectionOf(undefined), null);
});

// ---------- 健康 ----------
test('依赖状态到显示：慢与失败标红耗时，警告不标；名称换成界面叫法', () => {
  const slow = depView({ name: 'connector_gateway', probe: 'x', latency_ms: 1820, status: 'slow', last_issue: '10:42 超时 1 次' });
  assert.deepEqual([slow.name, slow.latency, slow.late, slow.chip.label, slow.chip.tone, slow.chip.dot], ['连接器网关', '1,820 ms', true, '慢', 'amber', true]);
  const warn = depView({ name: 'disk', latency_ms: 4, status: 'warn', last_issue: null });
  assert.deepEqual([warn.late, warn.chip.label, warn.issue], [false, '警告', '—']);
  assert.equal(depView({ name: 'auth_server', status: 'ready' }).chip.label, '就绪');
  assert.equal(depView({ name: 'auth_server', status: 'ok' }).chip.icon, 'check');
  assert.equal(depView({ name: 'model_gateway', status: 'fail' }).chip.tone, 'red');
  assert.equal(depView({ name: 'assembly', status: 'ready' }).name, '装配目录');
});

test('存活与就绪：路径取接口给的（C-03 / C-04），降级带通过数，卡片换色', () => {
  const now = Date.now();
  const lv = liveView({ status: 'ok', path: '/api/health/live', latency_ms: 3, uptime_ms: 2 * 3600e3 + 40 * 60e3, started_at: new Date(now - 160 * 60e3).toISOString() }, now);
  assert.equal(lv.lead, 'GET /api/health/live');
  assert.match(lv.text, /^3 ms · 进程已运行 2 小时 40 分（上次启动 /);
  assert.equal(liveView(null).lead, 'GET /api/health/live');
  const rd = readyView({ status: 'degraded', path: '/api/health/ready', passed: 5, total: 6, summary: '连接器网关响应慢' });
  assert.deepEqual([rd.chip.label, rd.tone, rd.lead, rd.text], ['降级 · 5 / 6', 'amber', 'GET /api/health/ready', '连接器网关响应慢']);
  assert.equal(readyView({ status: 'ready' }).tone, null);
  assert.equal(readyView({ status: 'not_ready', passed: 4, total: 6 }).chip.label, '未就绪 · 4 / 6');
});

test('上次退出：B3b 的 code 为 null 时不写 exit；B2 的 null 整体为空', () => {
  assert.equal(exitText({ at: local(2026, 9, 31, 8, 1), reason: 'unclean', code: null }), '10-31 08:01 · 意外结束');
  assert.equal(exitText({ at: local(2026, 9, 31, 8, 1), reason: 'unclean', code: 137 }), '10-31 08:01 · 意外结束（exit 137）');
  assert.equal(exitText({ reason: 'shutdown', code: 0 }, { withTime: false }), '正常停止');
  assert.equal(exitText(null), null);
  assert.equal(startupSub({ at: local(2026, 9, 31, 8, 2, 11), last_exit: null }), '10-31 08:02:11 · 上次退出：没有记录');
});

// ---------- 会话池 ----------
test('会话池：运行中不能回收、排队的没有按钮；管理员只读', () => {
  const run = sessionActions({ state: 'running' }, true);
  assert.equal(run.recycle.disabled, true);
  assert.match(run.recycle.title, /只能强制断开/);
  assert.equal(run.kill.disabled, false);
  assert.equal(sessionActions({ state: 'idle' }, true).recycle.disabled, false);
  assert.equal(sessionActions({ state: 'queued' }, true).show, false);
  const ro = sessionActions({ state: 'idle' }, false);
  assert.equal(ro.recycle.disabled && ro.kill.disabled, true);
  assert.match(ro.kill.title, /仅运维可操作/);
  assert.equal(idleText({ state: 'queued', queue_position: 1 }), '排第 1 位');
  assert.equal(idleText({ state: 'idle', idle_ms: 250e3 }), '4 分 10 秒');
  assert.equal(idleText({ state: 'running' }), '—');
  const it = { project: { name: '社区诊所排队分诊', owner: 'liming' }, engine_label: '稳定引擎', state: 'idle', idle_ms: 250e3 };
  assert.equal(sessionSub(it, { liming: '李明' }), '社区诊所排队分诊 · 李明 · 稳定引擎 · 空闲 4 分 10 秒');
  assert.equal(sessionSub({ ...it, state: 'running' }, {}, { engine: false }), '社区诊所排队分诊 · liming · 正在运行');
  assert.deepEqual(drainSummary({ items: [{ state: 'running', autobuild: true }, { state: 'running' }, { state: 'idle' }, { state: 'queued' }] }), { online: 3, running: 2, autobuild: 1 });
});

test('会话池配置：范围校验、有改动才能保存、请求体是整数', () => {
  const cfg = { max_sessions: 12, idle_ttl_s: 600, overflow_policy: 'evict_idle_then_queue' };
  const f = poolForm(cfg);
  assert.deepEqual(validatePool(f), {});
  assert.equal(poolDirty(f, cfg), false);
  assert.deepEqual(Object.keys(validatePool({ ...f, max_sessions: '65', idle_ttl_s: '59' })), ['max_sessions', 'idle_ttl_s']);
  assert.ok(validatePool({ ...f, max_sessions: '1.5' }).max_sessions);
  assert.ok(validatePool({ ...f, overflow_policy: 'x' }).overflow_policy);
  const g = { ...f, max_sessions: ' 16 ', overflow_policy: 'queue_only' };
  assert.equal(poolDirty(g, cfg), true);
  assert.deepEqual(poolBody({ ...g, max_sessions: '16' }), { max_sessions: 16, idle_ttl_s: 600, overflow_policy: 'queue_only' });
});

// ---------- 维护向导 ----------
const maintAt = (cur, pct = null, extra = {}) => ({
  active: true, mode: 'reject_new_sessions', message: 'm', started_at: local(2026, 9, 31, 15, 20), target: { kind: 'core_upgrade', pack: 'core', version: '0.1.1+8e41b7c0' },
  steps: ['restrict', 'drain', 'upgrade', 'resume'].map((id, i, all) => ({
    id, label: id, state: i < all.indexOf(cur) ? 'done' : id === cur ? 'current' : 'pending', detail: id === cur && id === 'drain' ? '剩余 3 个会话' : '', progress_pct: id === cur ? pct : null,
  })),
  ...extra,
});

test('维护四步条（R19）：没在维护时不画；进行中按 steps，第一步带开始时刻与模式，没走到的步骤只有标题', () => {
  assert.deepEqual(maintSteps({ active: false }), []);
  assert.deepEqual(maintSteps(null), []);
  assert.equal(maintTitle({ active: false }), '维护窗口');
  const on = maintSteps(maintAt('drain', 57));
  assert.deepEqual(on.map((s) => s.cls), ['done', 'cur', '', '']);
  assert.deepEqual(on.map((s) => s.title), ['拒绝新会话', '排空', '升级', '恢复']);
  assert.equal(on[0].detail, '15:20 开始');
  assert.equal(on[1].detail, '剩余 3 个会话');
  assert.equal(on[1].progress, 57);
  assert.equal(on[2].detail, '');
  assert.equal(on[3].detail, '');
  assert.equal(on[2].progress, null);
  assert.equal(maintSteps(maintAt('drain', 57, { mode: 'read_only' }))[0].title, '只读');
  assert.equal(maintTitle(maintAt('drain')), '维护窗口 · 升级 core 0.1.1+8e41b7c0');
  assert.equal(currentStep(maintAt('upgrade')).id, 'upgrade');
});

test('维护按钮：排空中不能继续、可以强制断开；排空完可以继续；第 4 步改成「恢复」走退出；管理员全部只读', () => {
  const draining = maintActions(maintAt('drain', 57), true);
  assert.equal(draining.next.disabled, true);
  assert.match(draining.next.title, /排空还没完成/);
  assert.equal(draining.force.disabled, false);
  const drained = maintActions(maintAt('drain', 100), true);
  assert.equal(drained.next.disabled, false);
  assert.equal(drained.force.disabled, true);
  const upgrading = maintActions(maintAt('upgrade', 40), true);
  assert.equal(upgrading.next.disabled, true);
  assert.equal(maintActions(maintAt('upgrade', null), true).next.disabled, false);
  const resume = maintActions(maintAt('resume'), true);
  assert.deepEqual([resume.next.label, resume.next.op, resume.next.disabled], ['恢复', 'exit', false]);
  const ro = maintActions(maintAt('drain', 100), false);
  assert.equal(ro.next.disabled && ro.cancel.disabled && ro.force.disabled, true);
  assert.equal(maintActions({ active: false }, true), null);
});

test('进入维护模式：请求体、校验、默认提示随模式与升级目标变；409 说成人话', () => {
  assert.deepEqual(maintBody({ mode: 'read_only', message: ' 迁移数据盘 ', target: null }), { mode: 'read_only', message: '迁移数据盘', autobuild: 'pause_at_safe_point' });
  assert.deepEqual(maintBody({ mode: 'reject_new_sessions', message: 'x', target: '0.1.1+8e41b7c0' }).target, { kind: 'core_upgrade', pack: 'core', version: '0.1.1+8e41b7c0' });
  assert.equal(maintProblem({ mode: 'reject_new_sessions', message: '  ' }), '请填写对用户的提示');
  assert.equal(maintProblem({ mode: 'x', message: 'a' }), '请选择限制方式');
  assert.equal(maintProblem({ mode: 'read_only', message: 'a' }), null);
  const now = new Date(2026, 9, 31, 15, 13).getTime();
  assert.equal(defaultMaintMessage('reject_new_sessions', { now }), '平台将在 15:30 升级，预计 20 分钟，期间暂停新会话。');
  assert.equal(defaultMaintMessage('read_only', { upgrade: false, now }), '平台将在 15:30 维护，预计 20 分钟，期间只能查看。');
  assert.equal(maintErrorText({ code: 'maintenance_step_blocked', details: { step: 'drain', remaining: 3 } }), '当前步骤还没完成：还剩 3 个会话');
  assert.match(maintErrorText({ code: 'maintenance_step_blocked', details: { step: 'resume' } }), /点「恢复」/);
  assert.equal(maintErrorText({ code: 'other' }, 'fb'), 'fb');
});

// ---------- 插件运维 ----------
test('插件运维按钮：熔断后变「恢复」；下线的不能放量；没有上一个版本不能回滚；错误率 ≥5% 标红', () => {
  const open = packActions({ breaker: 'open', channel: 'stable', rollback_to: '1.0.2' }, true);
  assert.deepEqual([open.breaker.label, open.breaker.action, open.breaker.kind], ['恢复', 'close', 'soft']);
  const half = packActions({ breaker: 'half_open', channel: 'beta', rollback_to: null }, true);
  assert.deepEqual([half.breaker.label, half.rollback.disabled], ['熔断', true]);
  assert.match(half.rollback.title, /没有更早的版本/);
  const retired = packActions({ breaker: 'closed', channel: 'retired', rollback_to: 'x' }, true);
  assert.equal(retired.rollout.disabled && retired.disable.disabled && retired.rollback.disabled && retired.breaker.disabled, true);
  const ro = packActions({ breaker: 'closed', channel: 'stable', rollback_to: 'x' }, false);
  assert.equal(ro.rollout.disabled && ro.rollback.disabled && ro.disable.disabled && ro.breaker.disabled, true);
  assert.deepEqual(errorRateView(12), { text: '12.0%', alert: true });
  assert.deepEqual(errorRateView(2.8), { text: '2.8%', alert: false });
  assert.equal(rolloutProblem('100'), null);
  assert.ok(rolloutProblem('101'));
  assert.ok(rolloutProblem(''));
});

test('core 默认版本表：beta、stable、下线的按发布时间倒序；操作分别是推广、当前默认、回滚到此', () => {
  const rows = coreRows({
    versions: [
      { version: 'a', channel: 'retired', released_at: '2026-09-01T00:00:00.000Z' },
      { version: 'b', channel: 'stable', released_at: '2026-10-12T00:00:00.000Z', is_default: true },
      { version: 'c', channel: 'retired', released_at: '2026-09-26T00:00:00.000Z' },
      { version: 'd', channel: 'beta', released_at: '2026-10-31T00:00:00.000Z' },
    ],
  });
  assert.deepEqual(rows.map((r) => [r.version, r.op]), [['d', 'promote'], ['b', 'current'], ['c', 'rollback'], ['a', 'rollback']]);
  assert.deepEqual(coreRows(null), []);
});

// ---------- 任务、备份、保留、诊断包 ----------
test('任务按钮：进行中取消、失败重试、部分完成的采集续采；428 提示所有者确认；结束后刷新对应的块', () => {
  assert.equal(taskAction({ state: 'running' }, true).op, 'cancel');
  assert.equal(taskAction({ state: 'failed', type: 'export' }, true).label, '重试');
  assert.deepEqual([taskAction({ state: 'partial', type: 'collection' }, true).op, taskAction({ state: 'partial', type: 'collection' }, true).label], ['resume', '续采']);
  assert.equal(taskAction({ state: 'succeeded' }, true), null);
  assert.equal(taskAction({ state: 'running' }, false).disabled, true);
  assert.match(taskActionToast('retry', {}, { status: 428, code: 'confirmation_required', needsConfirmation: true }), /所有者确认/);
  assert.equal(taskActionToast('resume', {}, null), '已续采，从断点继续');
  const before = [{ id: 'a', type: 'backup', state: 'running' }, { id: 'b', type: 'drill', state: 'running' }, { id: 'c', type: 'export', state: 'running' }];
  const after = [{ id: 'a', type: 'backup', state: 'succeeded' }, { id: 'b', type: 'drill', state: 'running' }, { id: 'c', type: 'export', state: 'failed' }, { id: 'n', type: 'pack_repo_backup', state: 'succeeded' }];
  assert.deepEqual(reloadsAfter(before, after), ['backups']);
});

test('备份计划：有固定时刻写每天，B2 只有间隔时写每 N 小时', () => {
  assert.equal(scheduleText({ daily_at: '02:00', keep: 3 }), '每天 02:00 · 保留最近 3 份');
  assert.equal(scheduleText({ daily_at: null, interval_hours: 24, keep: 3 }), '每 24 小时 · 保留最近 3 份');
});

test('数据保留：快照按份数说，表单只送改过的可编辑项，范围校验', () => {
  const items = D.retention(Date.now());
  assert.equal(retentionText(items.find((x) => x.id === 'snapshots')), '每个数据源最近 5 份');
  assert.equal(retentionText(items.find((x) => x.id === 'backups')), '最近 3 份');
  assert.equal(retentionText(items.find((x) => x.id === 'audit')), '365 天');
  const form = retentionForm(items);
  assert.deepEqual(retentionBody(form, items), { items: [] });
  const changed = form.map((f) => (f.id === 'logs' ? { ...f, value: '60' } : f.id === 'audit' ? { ...f, value: '30' } : f));
  assert.deepEqual(retentionBody(changed, items), { items: [{ id: 'logs', retention: { days: 60 } }] });
  const bad = form.map((f) => (f.id === 'snapshots' ? { ...f, value: '0' } : f.id === 'audit' ? { ...f, value: '0' } : f));
  assert.deepEqual(Object.keys(validateRetention(bad)), ['snapshots']);
  // 证据快照一行随证据源开关（本期不开放时不列）
  assert.equal(visibleRetention(items).some((x) => x.id === 'snapshots'), false);
  assert.equal(visibleRetention(items).length, items.length - 1);
  assert.deepEqual(visibleRetention(items, { evidenceOn: true }), items);
  assert.deepEqual(visibleRetention(null), []);
});

test('证据源不可用时运维管控不列证据相关的行：提供方与系统逆向建模包、证据采集任务；可用时原样', () => {
  const packs = D.opsPacks();
  const off = visiblePacks(packs);
  assert.equal(off.some((p) => p.kind === 'provider' || p.name === 'reverse'), false);
  assert.equal(off.length, packs.filter((p) => p.kind !== 'provider' && p.name !== 'reverse').length);
  assert.deepEqual(visiblePacks(packs, { evidenceOn: true }), packs);
  const inv = D.versions();
  assert.equal(visibleInventory(inv).some((r) => r.category === 'provider' || r.component === 'reverse' || r.component.startsWith('evidence')), false);
  assert.deepEqual(visibleInventory(inv, { evidenceOn: true }), inv);
  const tasks = D.tasks(Date.now()).items || D.tasks(Date.now());
  assert.ok(tasks.some((t) => t.type === 'collection'));
  assert.equal(visibleTasks(tasks).some((t) => t.type === 'collection'), false);
  assert.deepEqual(visibleTasks(tasks, { evidenceOn: true }), tasks);
  assert.deepEqual([visiblePacks(null), visibleInventory(null), visibleTasks(null)], [[], [], []]);
});

test('诊断包：默认勾前 5 项；提示里写上次生成的时间与人', () => {
  assert.deepEqual(diagDefaults(), ['logs_24h', 'config_refs', 'versions', 'recent_errors', 'startup_report']);
  assert.equal(diagNote({ created_at: local(2026, 9, 30, 17, 52), created_by: 'ops01' }, { ops01: '周运维' }), '上次生成：10-30 17:52 · 周运维');
  assert.equal(diagNote(null, {}), null);
  // 恢复演练：没做过时只有标题；做过的只写日期、用时、抽检结果
  assert.deepEqual([drillView(null).title, drillView(null).detail], ['还没做过恢复演练', '']);
  const passed = drillView({ at: local(2026, 9, 30, 3, 0), duration_ms: 125e3, detail: '抽检 5 个项目都能打开', result: 'passed' });
  assert.equal(passed.title, '最近一次恢复演练：通过');
  assert.equal(passed.detail.split(' · ').length, 3);
  assert.match(passed.detail, /抽检 5 个项目都能打开$/);
});

// ---------- mock：维护向导与排空按时间推进 ----------
test('mock 维护向导：进入即开始排空（剩 3 个、57%）→ 排空完才能下一步 → 升级 → 第 4 步再推进返回 blocked', () => {
  const t0 = 1_800_000_000_000;
  const pool = D.sessionPool(t0);
  const m = D.enterMaintenance(pool, { mode: 'reject_new_sessions', message: 'x', target: { pack: 'core', version: D.CORE.beta } }, { now: t0, user: 'ops01' });
  const drain = m.steps.find((s) => s.id === 'drain');
  assert.deepEqual([drain.state, drain.progress_pct, drain.detail], ['current', 57, '剩余 3 个会话 · 2 个自主构建已在安全点暂停']);
  assert.equal(pool.items.length, 3);
  assert.deepEqual(D.advanceMaintenance(m, pool, t0 + 1000).error, { step: 'drain', remaining: 3 });
  D.refreshMaintenance(m, pool, t0 + 2 * D.DRAIN_STEP_MS);
  assert.equal(drain.detail, '剩余 1 个会话 · 2 个自主构建已在安全点暂停');
  const t1 = t0 + 3 * D.DRAIN_STEP_MS;
  assert.equal(D.advanceMaintenance(m, pool, t1).state.steps.find((s) => s.state === 'current').id, 'upgrade');
  assert.equal(drain.detail, '已排空 · 7 个会话已回收');
  assert.deepEqual(D.advanceMaintenance(m, pool, t1 + 1000).error, { step: 'upgrade', remaining: 0 });
  D.refreshMaintenance(m, pool, t1 + D.UPGRADE_MS);
  assert.equal(m.upgraded, true);
  assert.equal(D.advanceMaintenance(m, pool, t1 + D.UPGRADE_MS + 1).state.steps.find((s) => s.state === 'current').id, 'resume');
  assert.deepEqual(D.advanceMaintenance(m, pool, t1 + D.UPGRADE_MS + 2).error, { step: 'resume', remaining: 0 });
  const pub = D.publicMaintenance(m);
  assert.equal(pub.until, null);
  assert.equal('upgrade_at' in pub || 'upgraded' in pub, false);
  assert.deepEqual(D.publicMaintenance(null), { active: false });
});

test('mock 维护：没有升级目标时升级这一步直接可过；强制断开剩余会话后排空立即完成', () => {
  const t0 = 1_800_000_000_000;
  const pool = D.sessionPool(t0);
  const m = D.enterMaintenance(pool, { mode: 'read_only', message: 'x' }, { now: t0, user: 'ops01' });
  assert.equal(D.forceDrain(pool), 3);
  D.refreshMaintenance(m, pool, t0 + 1);
  assert.equal(m.steps[1].detail, '已排空 · 7 个会话已回收（强制断开 3 个）');
  D.advanceMaintenance(m, pool, t0 + 2);
  assert.equal(m.steps[2].detail, '这次没有升级目标，直接下一步');
  assert.equal(D.advanceMaintenance(m, pool, t0 + 3).state.steps[3].state, 'current');
});

test('mock 切 core 默认版本：原 stable 下线，O-25 里 core 只留新的 stable 行', () => {
  const core = D.coreDefault(Date.now());
  const packs = D.opsPacks();
  D.setCoreStable(core, packs, D.CORE.beta);
  assert.equal(core.stable, D.CORE.beta);
  assert.equal(core.beta, null);
  assert.equal(core.versions.find((v) => v.version === D.CORE.stable).channel, 'retired');
  const rows = packs.filter((x) => x.name === 'core');
  assert.deepEqual(rows.map((x) => [x.version, x.channel, x.rollout_pct, x.rollback_to]), [[D.CORE.beta, 'stable', 100, D.CORE.stable]]);
  assert.match(D.CORE.stable, /^\d+\.\d+\.\d+\+[0-9a-f]{8}$/);
});

// ---------- mock 路由 ----------
function mockServer(sc = {}, maintenance = null) {
  const routes = [];
  const events = [];
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const err = (status, code, message, details = null) => json(status, { code, message, details });
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json, err, sc, F, maintenance, wait: async () => {},
    emitUser: (type, data) => events.push({ type, data }),
    forbid: (user, roles) => (roles.some((r) => (user?.roles || [user?.role]).includes(r)) ? undefined : err(403, 'forbidden', '没有权限')),
  };
  register(ctx);
  const ops = { username: 'ops01', role: 'ops', roles: ['user', 'ops'] };
  const admin = { username: 'zhangsan', role: 'admin', roles: ['user', 'admin'] };
  const call = async (method, path, body, user = ops) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user });
      if (res !== undefined) {
        const text = res.headers.get('Content-Type') === 'application/json' ? await res.json() : await res.blob();
        return { status: res.status, body: text, headers: res.headers };
      }
    }
    return { status: 404 };
  };
  return { call, events, ops, admin };
}
const O = '/api/v1/ops';

test('mock 角色：admin 能读、能立即备份，其余写操作 403；user 什么都不行', async () => {
  const { call, admin } = mockServer();
  assert.equal((await call('GET', `${O}/health`, null, admin)).status, 200);
  assert.equal((await call('GET', `${O}/sessions`, null, admin)).status, 200);
  assert.equal((await call('PUT', `${O}/sessions/config`, { max_sessions: 8, idle_ttl_s: 600, overflow_policy: 'queue_only' }, admin)).status, 403);
  assert.equal((await call('POST', `${O}/maintenance`, { mode: 'read_only', message: 'x' }, admin)).status, 403);
  assert.equal((await call('POST', `${O}/health/probe`, null, admin)).status, 403);
  assert.equal((await call('POST', `${O}/backups`, null, admin)).status, 202);
  assert.equal((await call('POST', `${O}/backups`, null, admin)).status, 409); // 已有备份在跑
  assert.equal((await call('GET', `${O}/diagnostics/diag-1030/download`, null, admin)).status, 200);
  assert.equal((await call('GET', `${O}/health`, null, { username: 'u', role: 'user', roles: ['user'] })).status, 403);
});

test('mock 健康：缺省降级（左栏角标），ctl:ready / ctl:notready 两种；立即探测返回依赖数组', async () => {
  const { call } = mockServer();
  const h = (await call('GET', `${O}/health`)).body;
  assert.equal(h.ready.status, 'degraded');
  assert.equal(h.dependencies.length, 6);
  assert.ok(h.runtime.last_exit && h.runtime.restarts >= 0);
  assert.equal(h.dependencies.some((d) => /example\.com/.test(d.probe)), true);
  assert.equal((await mockServer({ ctl: 'ready' }).call('GET', `${O}/health`)).body.ready.status, 'ready');
  assert.equal((await mockServer({ ctl: 'notready+nosess' }).call('GET', `${O}/health`)).body.ready.status, 'not_ready');
  const p = await call('POST', `${O}/health/probe`);
  assert.equal(Array.isArray(p.body) && p.body.length, 6);
});

test('mock 会话池：配置校验、运行中不能回收、强制断开要原因、排空回收空闲的', async () => {
  const { call } = mockServer();
  const pool = (await call('GET', `${O}/sessions`)).body;
  assert.deepEqual([pool.active, pool.queued, pool.config.max_sessions], [7, 1, 12]);
  assert.equal(pool.items[0].project.owner, 'zhangsan');
  assert.equal('goal' in pool.items[0].project, false);
  const bad = await call('PUT', `${O}/sessions/config`, { max_sessions: 65, idle_ttl_s: 30, overflow_policy: 'queue_only' });
  assert.deepEqual([bad.status, Object.keys(bad.body.details.fields)], [400, ['max_sessions', 'idle_ttl_s']]);
  assert.deepEqual((await call('PUT', `${O}/sessions/config`, { max_sessions: 16, idle_ttl_s: 900, overflow_policy: 'queue_only' })).body.config.max_sessions, 16);
  const running = pool.items.find((x) => x.state === 'running').project.id;
  const idle = pool.items.find((x) => x.state === 'idle').project.id;
  assert.equal((await call('POST', `${O}/sessions/${running}/recycle`)).body.code, 'session_running');
  assert.equal((await call('POST', `${O}/sessions/${idle}/recycle`)).body.recycled, true);
  assert.equal((await call('POST', `${O}/sessions/${idle}/recycle`)).status, 404);
  assert.equal((await call('POST', `${O}/sessions/${running}/disconnect`, { reason: ' ' })).status, 400);
  assert.equal((await call('POST', `${O}/sessions/${running}/disconnect`, { reason: '维护前清场' })).body.disconnected, true);
  const dr = await call('POST', `${O}/sessions/drain`, {});
  assert.deepEqual([dr.status, dr.body.running, dr.body.autobuild_running], [202, 2, 2]);
  assert.equal((await call('GET', `${O}/sessions`)).body.items.every((x) => x.state === 'running'), true);
  assert.equal((await mockServer({ ctl: 'nosess' }).call('GET', `${O}/sessions`)).body.items.length, 0);
});

test('mock 维护：进入、推进受阻、强制断开、升级、退出都发 maintenance.updated；until 恒为 null', async () => {
  const { call, events } = mockServer();
  assert.deepEqual((await call('GET', `${O}/maintenance`)).body, { active: false });
  assert.equal((await call('POST', `${O}/maintenance`, { mode: 'read_only', message: '' })).status, 400);
  const e = await call('POST', `${O}/maintenance`, { mode: 'reject_new_sessions', message: '升级', autobuild: 'pause_at_safe_point', target: { kind: 'core_upgrade', pack: 'core', version: D.CORE.beta } });
  assert.deepEqual([e.status, e.body.active, e.body.until, e.body.steps[1].state], [201, true, null, 'current']);
  assert.equal(events.at(-1).type, 'maintenance.updated');
  assert.equal((await call('POST', `${O}/maintenance`, { mode: 'read_only', message: 'x' })).body.code, 'maintenance_active');
  const blocked = await call('POST', `${O}/maintenance/advance`);
  assert.deepEqual([blocked.status, blocked.body.code, blocked.body.details.step, blocked.body.details.remaining], [409, 'maintenance_step_blocked', 'drain', 3]);
  assert.equal((await call('POST', `${O}/maintenance/force-disconnect`, {})).status, 400);
  const f = await call('POST', `${O}/maintenance/force-disconnect`, { reason: '到点' });
  assert.equal(f.body.steps[1].progress_pct, 100);
  const up = await call('POST', `${O}/maintenance/advance`);
  assert.equal(up.body.steps[2].state, 'current');
  const x = await call('DELETE', `${O}/maintenance`);
  assert.deepEqual(x.body, { active: false });
  assert.deepEqual(events.at(-1), { type: 'maintenance.updated', data: { active: false } });
  assert.equal((await call('DELETE', `${O}/maintenance`)).body.code, 'maintenance_not_active');
});

test('mock 维护：?mock=maint:reject 时一打开就在排空这一步，与外壳横幅同一模式与提示', async () => {
  const base = F.maintenanceState('reject_new_sessions');
  const { call } = mockServer({ maint: 'reject' }, base);
  const m = (await call('GET', `${O}/maintenance`)).body;
  assert.deepEqual([m.active, m.mode, m.message, m.steps[1].state, m.steps[1].progress_pct], [true, 'reject_new_sessions', base.message, 'current', 57]);
  assert.equal(m.target.version, D.CORE.beta);
});

test('mock 插件运维：灰度、熔断 → 半开、重复熔断 409、禁用要原因、回滚通道', async () => {
  const { call } = mockServer();
  const packs = (await call('GET', `${O}/packs`)).body.items;
  // 包名只用合成场景的（新建向导的包目录 + 证据源提供方与导出器）
  const synthetic = new Set([...F.packs().map((p) => p.name), 'db-mysql', 'exporter-otel']);
  assert.deepEqual(packs.filter((x) => !synthetic.has(x.name)).map((x) => x.name), []);
  assert.ok(packs.find((x) => x.name === 'industry-retail' && x.breaker === 'open'));
  assert.equal((await call('PUT', `${O}/packs/reverse/0.3.0/rollout`, { percent: 101 })).status, 400);
  assert.equal((await call('PUT', `${O}/packs/reverse/0.3.0/rollout`, { percent: 50, by: 'tenant' })).body.rollout_pct, 50);
  assert.equal((await call('POST', `${O}/packs/industry-retail/1.0.3/breaker`, { action: 'open' })).body.code, 'breaker_state_invalid');
  assert.equal((await call('POST', `${O}/packs/industry-retail/1.0.3/breaker`, { action: 'close' })).body.breaker, 'half_open');
  assert.equal((await call('POST', `${O}/packs/trace/0.9.1/breaker`, { action: 'open', reason: '失败率高' })).body.breaker, 'open');
  assert.equal((await call('POST', `${O}/packs/trace/0.9.1/disable`, {})).status, 400);
  assert.equal((await call('POST', `${O}/packs/trace/0.9.1/disable`, { reason: '等修复' })).body.channel, 'retired');
  const rb = await call('POST', `${O}/packs/solver/rollback`, { channel: 'stable', to_version: '0.7.1' });
  assert.equal(rb.body.version, '0.7.1');
  assert.equal((await call('PUT', `${O}/packs/nope/1.0.0/rollout`, { percent: 1 })).body.code, 'pack_version_not_found');
});

test('mock core 默认版本：真实版本号，推广、回滚前先备份包仓库（保留 3 份）', async () => {
  const { call } = mockServer();
  const c = (await call('GET', `${O}/core-default`)).body;
  assert.deepEqual([c.stable, c.beta.version, c.beta.rollout_pct], [D.CORE.stable, D.CORE.beta, 10]);
  const repo = (await call('GET', `${O}/pack-repo/backups`)).body;
  assert.deepEqual([repo.items.length, repo.keep], [3, 3]);
  const r = (await call('POST', `${O}/core-default/rollback`, { version: D.CORE.retired })).body;
  assert.equal(r.stable, D.CORE.retired);
  const repo2 = (await call('GET', `${O}/pack-repo/backups`)).body;
  assert.equal(repo2.items.length, 3);
  assert.notEqual(repo2.items[0].name, repo.items[0].name);
  const p = (await call('POST', `${O}/core-default/promote`, { version: D.CORE.beta })).body;
  assert.deepEqual([p.stable, p.beta], [D.CORE.beta, null]);
  assert.equal((await call('POST', `${O}/pack-repo/backups`)).status, 202);
});

test('mock 启动自检：write_intents 在 index_lock 之后、uncommitted 之前；last_exit 有值；摘要与总览一致', async () => {
  const { call } = mockServer();
  const r = (await call('GET', `${O}/startup-report`)).body;
  const ids = r.items.map((x) => x.id);
  assert.ok(ids.indexOf('index_lock') < ids.indexOf('write_intents') && ids.indexOf('write_intents') < ids.indexOf('uncommitted'));
  assert.equal(r.last_exit.reason, 'unclean');
  const by = Object.fromEntries(r.items.map((x) => [x.id, x]));
  assert.deepEqual([by.index_lock.result, by.uncommitted.result, by.assembly.title, by.dependencies.title], ['cleared', 'committed', '装配目录 38 / 38 一致', '依赖 6 / 6 可达']);
});

test('mock 备份：范围含 packs；演练要指定已有备份；任务列表可按类型筛选；取消与续采', async () => {
  const { call } = mockServer();
  const b = (await call('GET', `${O}/backups`)).body;
  assert.ok(b.scope.includes('packs'));
  assert.equal(b.items[2].scope, 'workspace_only');
  assert.equal(b.last_drill.result, 'passed');
  assert.equal((await call('POST', `${O}/backups/drills`, { backup: 'nope' })).status, 404);
  assert.equal((await call('POST', `${O}/backups/drills`, { backup: b.items[0].name, sample_projects: 5 })).status, 202);
  const drills = (await call('GET', `${O}/tasks?type=drill`)).body.items;
  assert.deepEqual(drills.map((x) => [x.type, x.state]), [['drill', 'running']]);
  assert.equal('onDone' in drills[0] || 'done_at' in drills[0], false);
  const all = (await call('GET', `${O}/tasks`)).body.items;
  const running = all.find((x) => x.type === 'collection' && x.state === 'running');
  assert.equal((await call('POST', `${O}/tasks/${running.id}/cancel`)).body.state, 'cancelled');
  assert.equal((await call('POST', `${O}/tasks/${running.id}/cancel`)).body.code, 'task_state_invalid');
  const partial = all.find((x) => x.state === 'partial');
  assert.equal((await call('POST', `${O}/tasks/${partial.id}/resume`)).body.state, 'running');
  const failed = all.find((x) => x.state === 'failed');
  assert.equal((await call('POST', `${O}/tasks/${failed.id}/resume`)).status, 409);
  assert.equal((await call('POST', `${O}/tasks/nope/retry`)).status, 404);
});

test('mock 版本清单、数据保留、诊断包', async () => {
  const { call } = mockServer();
  const v = (await call('GET', `${O}/versions`)).body.items;
  assert.ok(v.find((x) => x.component === 'page_api'));
  assert.match(v.find((x) => x.component === 'core').version, /\+[0-9a-f]{8} · .* beta$/);
  assert.equal((await call('PUT', `${O}/retention`, { items: [{ id: 'audit', retention: { days: 30 } }] })).body.details.fields.audit, 'not_editable');
  assert.equal((await call('PUT', `${O}/retention`, { items: [{ id: 'logs', retention: { days: 0 } }] })).status, 400);
  const ok = (await call('PUT', `${O}/retention`, { items: [{ id: 'logs', retention: { days: 60 } }] })).body.items;
  assert.deepEqual(ok.find((x) => x.id === 'logs').retention, { days: 60 });
  const d = await call('POST', `${O}/diagnostics`, { include: [] });
  assert.equal(d.status, 201);
  assert.match(d.body.name, /^diag-\d{8}-\d{4}\.zip$/);
  assert.equal((await call('POST', `${O}/diagnostics`, { include: ['secrets'] })).status, 400);
  assert.equal((await call('GET', `${O}/diagnostics`)).body.items[0].id, d.body.id);
  const dl = await call('GET', `${O}/diagnostics/${d.body.id}/download`);
  assert.match(dl.headers.get('Content-Disposition'), /diag-/);
  assert.equal((await call('GET', `${O}/diagnostics/nope/download`)).status, 404);
});
