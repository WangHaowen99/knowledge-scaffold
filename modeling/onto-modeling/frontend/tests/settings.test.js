// 项目设置（proj-settings）：页面纯逻辑，mock 的能力切换、对外 MCP、自主构建策略（AB-01～AB-04、AB-18、AB-19）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register as registerAutobuild } from '../src/api/mock/routes-autobuild.js';
import { actionsOf, extensionState, register as registerSettings } from '../src/api/mock/routes-settings.js';
import { projectPacks, remainingText } from '../src/api/mock/settings-data.js';
import {
  activeSection, advancedSummary, basicPatch, createdText, defaultConfigTab, engineCards,
  isRecommended, lastRunText, modelDisplayName, modelSourceText, moveItem, nameProblem, optionalStepsText, packToolsText,
  pendingSummary, policyBody, policyDirty, policyForm, rowActions, scopeLabel, sectionTarget, startBlocker,
  statusChip, switchToast, thresholdAt, thresholdPos, tokenBody, tokenProblems, toolList, toolsText, uiCell,
} from '../src/pages/settings/settingsModel.js';

const P = `/api/v1/projects/${F.MAIN_PID}`;

// ---------- 页面纯逻辑 ----------
test('分区参数：工作台、蓝图、插件页的跳转都能落到分区，autobuild 落到自主构建策略分区（R11 单列一个分区）', () => {
  assert.deepEqual(sectionTarget('build-mode'), { nav: 'build-mode', anchor: 'build-mode' });
  assert.deepEqual(sectionTarget('autobuild'), { nav: 'autobuild', anchor: 'autobuild' });
  assert.deepEqual(sectionTarget('plugins'), { nav: 'plugins', anchor: 'plugins' });
  assert.deepEqual(sectionTarget('mcp'), { nav: 'mcp', anchor: 'mcp' });
  assert.deepEqual(sectionTarget('packs'), { nav: 'plugins', anchor: 'plugins' });
  assert.equal(sectionTarget('nope'), null);
  assert.equal(sectionTarget(''), null);
  const tops = [['basic', 0], ['engine', 330], ['build-mode', 640]];
  assert.equal(activeSection(tops, 0), 'basic');
  assert.equal(activeSection(tops, 260), 'engine');
  assert.equal(activeSection(tops, 800), 'build-mode');
  assert.equal(activeSection(tops, 10, true), 'build-mode');
});

test('基本信息：只提交改了的字段，重名给候选，创建来源', () => {
  const meta = { name: '门店补货单编制', goal: '目标', created_at: '2026-10-01T02:00:00Z', created_from: 'wizard' };
  assert.deepEqual(basicPatch(meta, { name: '门店补货单编制 ', goal: '目标' }), {});
  assert.deepEqual(basicPatch(meta, { name: '门店补货', goal: ' 新目标 ' }), { name: '门店补货', goal: '新目标' });
  assert.equal(nameProblem(null, '  '), '请输入项目名称');
  assert.equal(nameProblem({ reason: 'conflict', conflict: { candidates: ['共享单车故障诊断 (2)'] } }, '共享单车故障诊断'), '你已经有一个叫「共享单车故障诊断」的项目，可以改成「共享单车故障诊断 (2)」');
  assert.equal(nameProblem({ available: true, reason: null }, '门店补货'), null);
  assert.match(createdText(meta), /^2026-10-0\d · 来自新建向导$/);
});

test('引擎卡：界面只显示标签；内测引擎不再挂「内测」小标签（名字已说明）；没装的标「当前部署未安装」；模型来源的说法', () => {
  const cards = engineCards(F.engines({ betaInstalled: false }), 'claude');
  assert.deepEqual(cards.map((c) => [c.label, c.on, c.installed, c.badge?.[0], c.reason]), [
    ['稳定引擎', true, true, '推荐', null],
    ['内测引擎', false, false, undefined, '当前部署未安装'],
  ]);
  assert.ok(cards.every((c) => !JSON.stringify([c.label, c.desc]).includes('claude')));
  assert.equal(cards[1].desc, '轻量');
  assert.equal(cards[0].desc, '功能最完整');
  assert.equal(modelSourceText('personal', { model: 'model-x' }), '使用我的模型配置 · model-x');
  assert.equal(modelSourceText('admin', F.modelSettings().engines.claude), '使用管理员配置 · model-standard');
  assert.equal(modelSourceText('deployment'), '使用部署默认配置');
  // 项目设置与新建第 4 步同一行只读的模型名
  assert.equal(modelDisplayName({ model: 'model-x' }), 'model-x');
  assert.equal(modelDisplayName(F.modelSettings().engines.claude), 'model-standard');
  assert.equal(modelDisplayName(undefined), '使用我的模型配置');
});

test('策略表单：只读字段、老策略里的预算与生产库授权都不提交', () => {
  const policy = { scope: { mode: 'all_pending', slice_ids: [] }, budget: { max_duration_min: 120, max_tokens: 6e6, max_cost: { amount: 150, currency: 'CNY' } }, production_collection: [], advanced: { confidence_threshold: 0.8 }, boundary_text: 'x', revision: 'p3', platform_limits: {} };
  const form = policyForm(policy);
  assert.deepEqual(Object.keys(form), ['scope', 'advanced']);
  assert.equal(policyDirty(policy, form), false);
  form.advanced.confidence_threshold = 0.9;
  assert.equal(policyDirty(policy, form), true);
  assert.equal(policy.advanced.confidence_threshold, 0.8, '深拷贝，不改原策略');
  assert.equal(policyBody(form, 'p3').base_revision, 'p3');
  assert.deepEqual(Object.keys(policyBody(form, 'p3')), ['scope', 'advanced', 'base_revision']);
});

test('「包括可选步骤」开关：按 AB-01 的 optional_nodes 列节点名，用「、」连；做到哪一步之后的不列；没有剩下的不显示（空串）', () => {
  const two = { optional_nodes: [
    { id: 'diagram.flowchart', name: '绘制流程图', under: 'core.research', order: 1 },
    { id: 'core.metric', name: '提取规则与指标', under: 'core.operation', order: 5 },
  ], until_order: null };
  assert.equal(optionalStepsText(two), '包括可选步骤：绘制流程图、提取规则与指标');
  // 做到「提取对象」（排第 2）：指标排在后面不做
  assert.equal(optionalStepsText({ ...two, until_order: 2 }), '包括可选步骤：绘制流程图');
  assert.equal(optionalStepsText({ ...two, until_order: 5 }), '包括可选步骤：绘制流程图、提取规则与指标', '做到它本身也做');
  // 做到的那一步在所有可选节点前面：一个都不剩，不显示开关
  assert.equal(optionalStepsText({ ...two, until_order: 0 }), '');
  assert.equal(optionalStepsText({ optional_nodes: [{ id: 'core.metric', name: '提取规则与指标' }] }), '包括可选步骤：提取规则与指标');
  assert.equal(optionalStepsText({ optional_nodes: [] }), '');
  assert.equal(optionalStepsText({}), '', '老后端没有这个字段：不显示');
  assert.equal(optionalStepsText(null), '');
  // 只读字段，不进表单
  assert.deepEqual(Object.keys(policyForm({ ...two, scope: {}, advanced: {} })), ['scope', 'advanced']);
});

test('高级设置：摘要、是否推荐值、阈值滑块、资料优先级排序', () => {
  const adv = { confidence_threshold: 0.8, below_threshold: 'conservative_assumption', source_priority: ['09 修订通知', '补货规则', '样例补货单', '数据库'], max_fix_rounds: 2 };
  assert.equal(advancedSummary(adv), '阈值 0.80 · 拿不准时先按最稳妥的做法往下做 · 冲突时修订通知优先 · 修复最多 2 轮');
  assert.equal(isRecommended(adv), true);
  assert.equal(isRecommended({ ...adv, max_fix_rounds: 3 }), false);
  // 设了「一律挂起」的类型也算调整过（V 阶段：设了之后收起的高级设置仍写「已按推荐值设置」）
  assert.equal(isRecommended({ ...adv, must_suspend_kinds: ['时限'] }), false);
  assert.equal(isRecommended({ ...adv, must_suspend_kinds: [] }), true);
  assert.equal(thresholdPos(0.8), 60);
  assert.equal(thresholdAt(61), 0.8);
  assert.equal(thresholdAt(-5), 0.5);
  assert.equal(thresholdAt(200), 1);
  assert.deepEqual(moveItem(adv.source_priority, 3, 0), ['数据库', '09 修订通知', '补货规则', '样例补货单']);
  assert.equal(moveItem(adv.source_priority, 1, 9), adv.source_priority);
});

test('开始：运行中、归档时不能开始；还有没有要做的由开始前预览判定（F8 起不分段）', () => {
  assert.equal(startBlocker({ archived: false, active: null }), null);
  assert.equal(startBlocker({ archived: true, active: null }), '项目已归档，不能开始自主构建');
  assert.equal(startBlocker({ active: { run_no: 4, state: 'queued' } }), '运行 #4 排队中，结束后才能开始下一次');
  assert.equal(lastRunText({ run_no: 3, ended_at: '2026-11-01T09:02:00Z', duration_ms: 112 * 60000, reviewed: false }), `上次运行 #3 · 11-01 · 1 小时 52 分 · 待复核`);
  assert.equal(lastRunText(null), '还没有运行过自主构建');
});

test('能力表：界面列、状态 chip、行内按钮与设计稿一致', () => {
  const items = projectPacks().map((p) => ({ ...p, actions: actionsOf(p) }));
  const by = (n) => items.find((p) => p.name === n);
  assert.deepEqual(uiCell(by('core').ui), { kind: 'kernel', text: '内核：工作台 · Studio · 资料 · 发布' });
  assert.deepEqual(uiCell(by('trace').ui), { kind: 'page', text: '左栏页面：场景模拟' });
  assert.deepEqual(uiCell({ kind: 'subpages', subpages: [{ parent: 'resources', title: '台账' }, { parent: 'resources', title: '周报' }] }), { kind: 'subpages', text: '资料页 2 个子页', pop: '台账、周报' });
  assert.deepEqual(uiCell(by('industry-retail').ui), { kind: 'none', text: '—' });
  assert.deepEqual(statusChip(by('core')), { tone: 'blue', icon: 'lock', text: '必装' });
  assert.equal(statusChip(by('trace')).text, '0.10.0 下一条消息生效');
  assert.equal(statusChip(by('trace')).pop, '下一条消息前切换');
  const disabled = { ...by('diagram'), status: 'disabled' };
  disabled.actions = actionsOf(disabled);
  assert.equal(statusChip(disabled).text, '已停用');
  const labelsOf = (p) => { const r = rowActions(p); return [r.note || null, r.buttons.map((b) => b.label), r.more.map((b) => b.label)]; };
  const labels = (n) => labelsOf(by(n));
  assert.deepEqual(labels('core'), [null, [], []]);
  assert.deepEqual(labels('trace'), [null, ['撤销切换'], []]);
  assert.deepEqual(labels('industry-retail'), [null, ['停用'], []]);
  assert.deepEqual(labels('codegen'), [null, ['回滚', '停用'], []]);
  assert.deepEqual(labels('diagram'), [null, ['升级'], ['停用']]);
  assert.deepEqual(labelsOf(disabled), [null, ['启用'], []]);
  assert.equal(pendingSummary([{ pack: 'trace', from: '0.9.1', to: '0.10.0', kind: 'upgrade' }], items), '1 个待生效的切换：场景模拟 0.9.1 → 0.10.0');
  assert.equal(pendingSummary([], items), '');
  assert.equal(switchToast('upgrade', by('diagram'), { effective: 'next_message', pack: { pending: { to: '0.6.0' } } }), '已升级到图示 0.6.0，下一条消息生效');
  assert.equal(switchToast('disable', by('codegen'), { effective: 'next_message' }), '已停用「代码生成与求解」，左栏的「代码生成」已移除，下一条消息生效');
  assert.equal(switchToast('disable', by('industry-retail'), { effective: 'immediate' }), '已停用「零售连锁」');
  assert.equal(switchToast('cancel_pending', by('trace')), '已撤销，保持 0.9.1');
});

test('MCP：工具的写法、对外工具排序、包贡献工具、权限范围、新建 Token 的校验', () => {
  assert.equal(toolsText(['trace.list', 'trace.get', 'trace.run']), 'trace.list / get / run');
  assert.equal(toolsText(['flow.get']), 'flow.get');
  assert.equal(toolsText([]), '—');
  assert.equal(toolsText(['a_x', 'b_y', 'c_z']), 'a_x 等 3 个');
  const tools = toolList([{ tools: ['rule_get', 'object_list', 'object_delete'] }, { tools: ['object_change_apply', 'object_get', 'object_change_validate'] }]);
  assert.deepEqual(tools, ['object_list', 'object_get', 'object_change_validate', 'object_change_apply', 'object_delete', 'rule_get']);
  assert.equal(packToolsText([{ name: 'flow.get' }, ...['list', 'get', 'run'].map((t) => ({ name: `trace.${t}` }))]), 'flow.get、trace.*（3）');
  assert.deepEqual(['ontology.read', 'ontology.change.validate', 'ontology.change.apply'].map(scopeLabel), ['ontology.read', 'change.validate', 'change.apply']);
  assert.deepEqual(tokenProblems({ name: ' ', scopes: [] }), { name: '请填写名称', scopes: '至少选一个' });
  assert.deepEqual(tokenBody({ name: ' 联调 ', scopes: ['ontology.read'], ttl: '0' }), { name: '联调', scopes: ['ontology.read'], expires_in_s: null });
  assert.equal(tokenBody({ name: 'x', scopes: ['ontology.read'], ttl: '1' }).expires_in_s, 86400);
  assert.equal(defaultConfigTab(['ontology.read']), 'r');
  assert.equal(defaultConfigTab(['ontology.read', 'ontology.change.validate']), 'c');
  const now = Date.parse('2026-11-01T12:00:00Z');
  assert.equal(remainingText('2026-10-31T18:00:00Z', '2026-11-01T18:00:00Z', now), '1 天 · 剩 6 小时');
  assert.equal(remainingText('2026-10-23T12:00:00Z', '2026-11-22T12:00:00Z', now), '30 天 · 剩 21 天');
  assert.equal(remainingText('2026-10-23T12:00:00Z', null, now), '永不过期');
});

// ---------- mock：项目设置的路由 ----------
function harness({ sc = {}, stage = null } = {}) {
  const routes = [];
  const events = [];
  const cards = F.projectCards();
  const convs = new Map();
  const seeded = new Map();
  let n = 0;
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: { scope: new Map() },
    sc,
    uid: () => (n += 1).toString(16),
    F,
    cardOf: (id) => cards.find((c) => c.id === id),
    stageOf: () => stage,
    flowOf: () => F.flow(),
    convsOf: (pid) => { if (!convs.has(pid)) convs.set(pid, F.conversations()); return convs.get(pid); },
    convKey: (pid, cid) => `${pid}:${cid}`,
    hub: { seed: (key, kind, evs) => seeded.set(key, evs) },
    scopeOf: () => ({ state: 'none' }),
    setScope: () => {},
    setSession: () => {},
    emitProj: (pid, type, data) => events.push({ type, data }),
    emitConv: (pid, cid, type, data) => events.push({ type, data }),
  };
  registerAutobuild(ctx);
  registerSettings(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user: F.USERS.zhangsan });
      if (res !== undefined) return { status: res.status, body: res.status === 204 ? null : await res.json() };
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  return { call, events, cards, ctx, convs, seeded };
}

test('mock AB-19：确认结束的运行，运行条收起、项目回到陪伴建模；没结束的不能确认', async () => {
  const live = harness({ stage: 'l4' });
  const busy = await live.call('POST', `${P}/autobuild/runs/run-3/acknowledge`, {});
  assert.deepEqual([busy.status, busy.body.code], [409, 'autobuild_not_running']);

  const { call, events, cards } = harness();
  cards.find((c) => c.id === F.MAIN_PID).build_mode = 'L4';
  assert.equal((await call('GET', `${P}/autobuild/runs/run-3`)).body.acknowledged, false);
  const r = await call('POST', `${P}/autobuild/runs/run-3/acknowledge`, {});
  assert.equal(r.status, 200);
  assert.deepEqual([r.body.state, r.body.acknowledged], ['completed', true]);
  assert.equal(cards.find((c) => c.id === F.MAIN_PID).build_mode, 'L2');
  const upd = events.find((e) => e.type === 'project.updated');
  assert.deepEqual([upd.data.fields, upd.data.meta.build_mode], [['build_mode'], 'L2']);
  assert.equal(events.find((e) => e.type === 'autobuild.run.updated').data.acknowledged, true);
  assert.equal((await call('POST', `${P}/autobuild/runs/run-9/acknowledge`, {})).status, 404);
});

test('mock PL-05～PL-11：停用带页面的能力，左栏立刻少一页、发两个事件；撤销后回来', async () => {
  const { call, events, ctx } = harness();
  const list = await call('GET', `${P}/packs`);
  assert.equal(list.body.pending_switches, 1);
  assert.deepEqual(list.body.items.find((p) => p.name === 'diagram').actions, ['upgrade', 'disable']);
  assert.deepEqual(ctx.db.extensionsOf(F.MAIN_PID).nav.filter((x) => x.kind === 'plugin').map((x) => x.id), ['scenarios', 'scripts']);

  const off = await call('POST', `${P}/packs/codegen/disable`);
  assert.equal(off.status, 200);
  assert.equal(off.body.effective, 'next_message');
  assert.deepEqual(off.body.pack.pending, { from: '0.9.2', to: null, kind: 'disable', effective: 'next_message' });
  assert.deepEqual(events.map((e) => e.type), ['assembly.updated', 'extensions.updated']);
  const ext = ctx.db.extensionsOf(F.MAIN_PID);
  assert.deepEqual(ext.nav.filter((x) => x.kind === 'plugin').map((x) => x.id), ['scenarios']);
  assert.deepEqual(ext.disabled_pages, [{ id: 'scripts', pack: 'codegen', title: '代码生成' }]);
  assert.equal((await call('GET', `${P}/assembly`)).body.pending_changes.length, 2);

  const back = await call('POST', `${P}/packs/codegen/cancel-pending`);
  assert.equal(back.body.pack.status, 'enabled');
  assert.ok(ctx.db.extensionsOf(F.MAIN_PID).nav.some((x) => x.id === 'scripts'));
  assert.equal((await call('POST', `${P}/packs/codegen/cancel-pending`)).body.code, 'no_pending_switch');
});

test('mock PL-06～PL-09：升级、回滚待生效；纯数据包立即生效；必装不能停；新启用的页面带 pending', async () => {
  const { call, ctx } = harness();
  const up = await call('POST', `${P}/packs/diagram/upgrade`, { to_version: '0.6.0' });
  assert.equal(up.body.pack.status, 'pending');
  assert.equal(up.body.pack.pending.kind, 'upgrade');
  assert.equal((await call('POST', `${P}/packs/diagram/upgrade`, { to_version: '0.6.0' })).body.code, 'pack_not_enabled');
  assert.equal((await call('POST', `${P}/packs/codegen/rollback`, { to_version: '9.9.9' })).body.code, 'pack_version_not_found');
  assert.equal((await call('POST', `${P}/packs/codegen/rollback`, { to_version: '0.9.1' })).body.pack.pending.to, '0.9.1');
  const ind = await call('POST', `${P}/packs/industry-retail/disable`);
  assert.equal(ind.body.effective, 'immediate');
  assert.equal(ind.body.pack.status, 'disabled');
  assert.equal((await call('POST', `${P}/packs/core/disable`)).body.code, 'pack_required');
  assert.equal((await call('POST', `${P}/packs`, { name: 'codegen' })).body.code, 'pack_already_enabled');
  assert.equal((await call('POST', `${P}/packs`, { name: 'nope' })).body.code, 'pack_not_found');
  // 一开始就停用的能力（?mock=off:codegen）：启用后待生效，左栏的页面带 pending
  const offH = harness({ sc: { off: 'codegen' } });
  const en = await offH.call('POST', `${P}/packs`, { name: 'codegen' });
  assert.equal(en.status, 201);
  assert.deepEqual(en.body.pack.pending, { from: null, to: '0.9.2', kind: 'enable', effective: 'next_message' });
  assert.equal(offH.ctx.db.extensionsOf(F.MAIN_PID).pages.find((p) => p.id === 'scripts').pending, true);
  // 停用场景模拟（有待生效的升级）要先撤销
  await call('POST', `${P}/packs/trace/cancel-pending`);
  await call('POST', `${P}/packs/trace/disable`);
  await call('POST', `${P}/packs/trace/cancel-pending`);
  const reEnable = await call('POST', `${P}/packs/trace/disable`);
  assert.equal(reEnable.body.pack.pending.kind, 'disable');
  assert.ok(!ctx.db.extensionsOf(F.MAIN_PID).nav.some((x) => x.id === 'scenarios'));
});

test('extensionState：待生效的停用不算在左栏，待生效的启用算', () => {
  const items = [
    { name: 'a', status: 'enabled' }, { name: 'b', status: 'pending', pending: { kind: 'disable' } },
    { name: 'c', status: 'pending', pending: { kind: 'enable' } }, { name: 'd', status: 'disabled' }, { name: 'e', status: 'required' },
  ];
  assert.deepEqual(extensionState(items), { active: ['a', 'c', 'e'], pendingEnable: ['c'], disabled: ['b', 'd'] });
});

test('mock P-12、P-14、M-01～M-04：引擎没装或运行中不能换；服务器按停用名单；Token 明文只给一次', async () => {
  const missing = harness({ sc: { engine: 'missing' } });
  assert.equal((await missing.call('PUT', `${P}/engine`, { engine: 'jiuwen' })).body.code, 'engine_unavailable');
  const h = harness();
  assert.deepEqual((await h.call('GET', `${P}/engine`)).body, { engine: 'claude', model_source: 'admin' });
  const sw = await h.call('PUT', `${P}/engine`, { engine: 'jiuwen' });
  assert.deepEqual(sw.body, { engine: 'jiuwen' });
  assert.equal(h.cards[0].engine_label, '内测引擎');
  assert.equal(h.events.at(-1).type, 'project.updated');

  const srv = await h.call('PUT', `${P}/mcp-servers`, { disabled: ['mermaid-render'] });
  assert.deepEqual(srv.body.items.map((s) => [s.name, s.enabled]), [['flow', true], ['replenish-std-lookup', true], ['mermaid-render', false]]);

  const mcp = (await h.call('GET', `${P}/mcp`)).body;
  assert.equal(mcp.local_only, true);
  assert.equal(toolList(mcp.toolsets).length, 15);
  assert.match(mcp.client_config_templates.read, /<READ_TOKEN>/);
  assert.ok(mcp.endpoints.every((e) => e.url.startsWith('http://127.0.0.1:8100/api/v1/onto-mcp/projects/')));
  const bad = await h.call('POST', `${P}/mcp/tokens`, { name: '', scopes: [], expires_in_s: 10 });
  assert.deepEqual(bad.body.details.fields, { name: 'required', scopes: 'required', expires_in_s: 'out_of_range' });
  const made = await h.call('POST', `${P}/mcp/tokens`, { name: '只读巡检', scopes: ['ontology.read'], expires_in_s: null });
  assert.equal(made.status, 201);
  assert.match(made.body.token, /^onto_pat_[A-Za-z0-9]{28}$/);
  assert.ok(made.body.client_config.read.includes(made.body.token));
  assert.equal(made.body.client_config.change, '', '只读 Token 不给 change 配置');
  const listed = (await h.call('GET', `${P}/mcp/tokens`)).body.items;
  assert.equal(listed[0].name, '只读巡检');
  assert.equal(listed[0].remaining_text, '永不过期');
  assert.ok(!JSON.stringify(listed).includes(made.body.token), '列表里没有明文');
  assert.equal((await h.call('DELETE', `${P}/mcp/tokens/${listed[0].id}`)).status, 204);
  assert.equal((await h.call('DELETE', `${P}/mcp/tokens/${listed[0].id}`)).status, 404);
});

test('mock AB-01～AB-04、AB-18：保存的策略决定预览；开始后开一条运行对话，再开始返回运行中', async () => {
  const { call, convs, seeded } = harness();
  const p0 = (await call('GET', `${P}/autobuild/policy`)).body;
  assert.equal(p0.revision, 'p3');
  assert.equal(p0.last_run.run_no, 3);
  assert.deepEqual([p0.platform_limits, p0.not_enforced], [{}, []]);
  assert.equal(optionalStepsText(p0), '包括可选步骤：绘制流程图、提取规则与指标');
  assert.equal(p0.until_order, null);
  assert.equal(p0.budget, undefined);
  assert.equal('production_collection' in p0, false);

  assert.equal((await call('PUT', `${P}/autobuild/policy`, { base_revision: 'old' })).body.code, 'revision_conflict');
  // 老客户端还发预算、生产库采集授权：不收、不报错
  const legacy = await call('PUT', `${P}/autobuild/policy`, {
    ...policyForm(p0), budget: { max_tokens: 20e6 }, production_collection: [{ source_id: 'x', allowed: true }], base_revision: 'p3',
  });
  assert.equal(legacy.status, 200);
  assert.deepEqual(legacy.body.optional_nodes, p0.optional_nodes, 'AB-02 的响应同 AB-01');
  assert.equal(legacy.body.budget, undefined);
  assert.equal(legacy.body.production_collection, undefined);

  const form = policyForm(p0);
  form.scope = { ...form.scope, mode: 'selected', slice_ids: ['s3', 's4'] };
  const saved = await call('PUT', `${P}/autobuild/policy`, policyBody(form, legacy.body.revision));
  assert.equal(saved.status, 200);
  assert.notEqual(saved.body.revision, legacy.body.revision);
  // 不再分段：老客户端选的分段原样存着、不再起作用，按全部没做完的处理（不报错）
  const kept = (await call('GET', `${P}/autobuild/policy`)).body.scope;
  assert.equal(kept.mode, 'all_pending');
  assert.deepEqual(kept.slice_ids, ['s3', 's4']);
  // 做到「提取对象」：until_order 跟着变，开关只列它之前的
  const until = (await call('PUT', `${P}/autobuild/policy`, { scope: { until_node: 'core.object' } })).body;
  assert.equal(optionalStepsText(until), '包括可选步骤：绘制流程图');
  await call('PUT', `${P}/autobuild/policy`, { scope: { until_node: null } });

  // 一个节点一行：资料分析之后的主线节点，发布不在里面
  const pv = (await call('POST', `${P}/autobuild/runs/preview`, {})).body;
  assert.deepEqual(pv.slices.map((s) => [s.id, s.current, s.included]), [
    ['core.model', '进行中', true], ['core.validate', '未开始', true], ['trace.trace', '有过期', true]]);
  // 建模正做到一半（有待确认的一步）：会按策略决定
  assert.deepEqual(pv.warnings, ['有 1 个未决决策会被按策略决定']);
  assert.ok(pv.preview_token);

  assert.equal((await call('POST', `${P}/autobuild/runs`, {})).body.code, 'preview_expired');
  const run = await call('POST', `${P}/autobuild/runs`, { preview_token: pv.preview_token });
  assert.equal(run.status, 201);
  assert.deepEqual([run.body.run_no, run.body.state, run.body.slices.total], [4, 'queued', 3]);
  assert.deepEqual([run.body.stages, run.body.matrix.map((row) => [row.slice_id, row.state])],
    [[], [['core.model', 'queued'], ['core.validate', 'queued'], ['trace.trace', 'queued']]]);
  const conv = convs.get(F.MAIN_PID)[0];
  assert.deepEqual([conv.id, conv.title, conv.kind, conv.live], [run.body.conversation_id, '自主构建 · 运行 #4', 'autobuild', true]);
  const start = seeded.get(`${F.MAIN_PID}:${conv.id}`).find((e) => e.type === 'card').data;
  assert.equal(start.card_type, 'autobuild_start');
  assert.deepEqual(start.chips.map((c) => c.label), ['范围', '拿不准时']);
  assert.deepEqual(start.chips.map((c) => c.value), ['3 步', '先按最稳妥的做法往下做，做完列出来给你复核']);
  assert.deepEqual([run.body.tokens, run.body.acknowledged], [{ used: 0 }, false]);

  const running = (await call('GET', `${P}/autobuild/runs?state=running`)).body.items;
  assert.deepEqual(running.map((r) => [r.run_no, r.state]), [[4, 'queued']]);
  assert.equal((await call('POST', `${P}/autobuild/runs/preview`, {})).body.code, 'autobuild_running');
  assert.equal((await call('PUT', `${P}/engine`, { engine: 'jiuwen' })).body.code, 'autobuild_running');
});
