// 项目设置（proj-settings）：页面纯逻辑，mock 的能力切换、对外 MCP、自主构建策略（AB-01～AB-04、AB-18）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register as registerAutobuild } from '../src/api/mock/routes-autobuild.js';
import { actionsOf, extensionState, register as registerSettings } from '../src/api/mock/routes-settings.js';
import { mcpServers, projectPacks, remainingText } from '../src/api/mock/settings-data.js';
import {
  activeSection, advancedSummary, basicPatch, budgetOptions, budgetPct, createdText, defaultConfigTab, engineCards, finalizePolicy,
  fmtDuration, fmtMoney, fmtTokens, isRecommended, lastRunText, modelDisplayName, modelSourceText, moveItem, nameProblem, packToolsText, parseScope,
  parseWindow, pendingSummary, policyBody, policyDirty, policyForm, rowActions, scopeLabel, sectionTarget, sliceRows, startBlocker,
  statusChip, switchToast, thresholdAt, thresholdPos, tokenBody, tokenProblems, toolList, toolsText, uiCell, visibleMcp, windowText,
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

test('策略表单：只读字段不提交；生产库的时间窗口、采集范围文字并进表单', () => {
  const policy = { scope: { mode: 'all_pending', slice_ids: [] }, budget: { max_duration_min: 120, max_tokens: 6e6, max_cost: { amount: 150, currency: 'CNY' } }, production_collection: [{ source_id: 'erp-prod', allowed: false, window: null, scope: [] }], advanced: { confidence_threshold: 0.8 }, boundary_text: 'x', revision: 'p3', platform_limits: {} };
  const form = policyForm(policy);
  assert.deepEqual(Object.keys(form), ['scope', 'budget', 'production_collection', 'advanced']);
  assert.equal(policyDirty(policy, form), false);
  form.budget.max_tokens = 4e6;
  assert.equal(policyDirty(policy, form), true);
  assert.equal(policy.budget.max_tokens, 6e6, '深拷贝，不改原策略');
  assert.equal(policyBody(form, 'p3').base_revision, 'p3');

  const on = { ...form, production_collection: [{ ...form.production_collection[0], allowed: true }] };
  const ok = finalizePolicy(on, { 'erp-prod': { window: '22:00 – 06:00', scope: '补货标准表、商品品类表' } });
  assert.deepEqual(ok.problems, {});
  assert.deepEqual(ok.form.production_collection[0].window, { start: '22:00', end: '06:00' });
  assert.deepEqual(ok.form.production_collection[0].scope, ['补货标准表', '商品品类表']);
  const bad = finalizePolicy(on, { 'erp-prod': { window: '晚上' } });
  assert.match(bad.problems['window:erp-prod'], /22:00 – 06:00/);
  assert.deepEqual(finalizePolicy({ ...form, scope: { mode: 'selected', slice_ids: [] } }).problems, { scope: '至少选一片' });
  assert.deepEqual(parseWindow('8:00-9:00'), { error: '写成「22:00 – 06:00」这样的起止时间' });
  assert.deepEqual(parseWindow('22:00-06:00'), { value: { start: '22:00', end: '06:00' } });
  assert.equal(windowText({ start: '22:00', end: '06:00' }), '22:00 – 06:00');
  assert.deepEqual(parseScope('补货标准表，商品品类表 ;门店清单'), ['补货标准表', '商品品类表', '门店清单']);
});

test('预算：写法同设计稿，下拉只给平台上限以内的，进度条按上限算', () => {
  assert.equal(fmtDuration(120), '2 小时');
  assert.equal(fmtDuration(90), '1 小时 30 分');
  assert.equal(fmtDuration(45), '45 分钟');
  assert.equal(fmtTokens(6e6), '6M');
  assert.equal(fmtTokens(2.5e6), '2.5M');
  assert.equal(fmtMoney({ amount: 150, currency: 'CNY' }), '¥150');
  const limits = { max_duration_min: 240, max_tokens: 10e6, max_cost: { amount: 300, currency: 'CNY' } };
  assert.deepEqual(budgetOptions('duration', limits, 120), [30, 60, 90, 120, 180, 240]);
  assert.deepEqual(budgetOptions('cost', limits, 150), [30, 50, 100, 150, 200, 300]);
  assert.deepEqual(budgetOptions('tokens', limits, 5e6), [1e6, 2e6, 4e6, 5e6, 6e6, 8e6, 10e6]);
  assert.deepEqual([budgetPct(120, 240), budgetPct(6e6, 10e6), budgetPct(150, 300)], [50, 60, 50]);
});

test('高级设置：摘要、是否推荐值、阈值滑块、资料优先级排序', () => {
  const adv = { confidence_threshold: 0.8, below_threshold: 'conservative_assumption', source_priority: ['09 修订通知', '补货规则', '样例补货单', '数据库'], max_fix_rounds: 2 };
  assert.equal(advancedSummary(adv), '阈值 0.80 · 低于阈值采用保守选项并记为假设 · 冲突时修订通知优先 · 修复最多 2 轮');
  assert.equal(isRecommended(adv), true);
  assert.equal(isRecommended({ ...adv, max_fix_rounds: 3 }), false);
  assert.equal(thresholdPos(0.8), 60);
  assert.equal(thresholdAt(61), 0.8);
  assert.equal(thresholdAt(-5), 0.5);
  assert.equal(thresholdAt(200), 1);
  assert.deepEqual(moveItem(adv.source_priority, 3, 0), ['数据库', '09 修订通知', '补货规则', '样例补货单']);
  assert.equal(moveItem(adv.source_priority, 1, 9), adv.source_priority);
});

test('范围与开始：切片现状取流程，已确认的不进范围；运行中、归档时不能开始', () => {
  const rows = sliceRows([{ id: 's1', name: '补货申请与门店需求' }, { id: 's2', name: '安全库存测算' }], F.flow().slices);
  assert.deepEqual(rows.map((r) => [r.id, r.group, r.done]), [['s1', 'done', true], ['s2', 'wait', false]]);
  assert.equal(rows[1].note, '2 条规则待确认');
  assert.equal(sliceRows(null, F.flow().slices).length, 5);
  assert.equal(startBlocker({ archived: false, active: null, rows }), null);
  assert.equal(startBlocker({ archived: true, active: null, rows }), '项目已归档，不能开始自主构建');
  assert.equal(startBlocker({ active: { run_no: 4, state: 'queued' }, rows }), '运行 #4 排队中，结束后才能开始下一次');
  assert.equal(startBlocker({ rows: [rows[0]] }), '没有未完成的切片');
  assert.equal(lastRunText({ run_no: 3, ended_at: '2026-11-01T09:02:00Z', duration_ms: 112 * 60000, reviewed: false }), `上次运行 #3 · 11-01 · 1 小时 52 分 · 待复核`);
  assert.equal(lastRunText(null), '还没有运行过自主构建');
});

test('能力表：界面列、状态 chip、行内按钮与设计稿一致', () => {
  const items = projectPacks().map((p) => ({ ...p, actions: actionsOf(p) }));
  const by = (n) => items.find((p) => p.name === n);
  assert.deepEqual(uiCell(by('core').ui), { kind: 'kernel', text: '内核：工作台 · Studio · 资料 · 发布' });
  assert.deepEqual(uiCell(by('trace').ui), { kind: 'page', text: '左栏页面：场景走查' });
  assert.deepEqual(uiCell(by('reverse').ui), { kind: 'subpages', text: '资料页 3 个子页', pop: '业务系统数据源、采集与快照、证据浏览器' });
  assert.deepEqual(uiCell(by('industry-retail').ui), { kind: 'none', text: '—' });
  assert.deepEqual(statusChip(by('core')), { tone: 'blue', icon: 'lock', text: '必装' });
  assert.equal(statusChip(by('trace')).text, '0.10.0 下一条消息生效');
  assert.equal(statusChip(by('trace')).pop, '下一条消息前切换');
  assert.equal(statusChip(by('migrate')).text, '已停用');
  const labels = (n) => { const r = rowActions(by(n)); return [r.note || null, r.buttons.map((b) => b.label), r.more.map((b) => b.label)]; };
  assert.deepEqual(labels('core'), [null, [], []]);
  assert.deepEqual(labels('trace'), [null, ['撤销切换'], []]);
  assert.deepEqual(labels('knowledge'), [null, ['停用'], []]);
  assert.deepEqual(labels('codegen'), [null, ['回滚', '停用'], []]);
  assert.deepEqual(labels('diagram'), [null, ['升级'], ['停用']]);
  assert.deepEqual(labels('migrate'), [null, ['启用'], []]);
  assert.equal(pendingSummary([{ pack: 'trace', from: '0.9.1', to: '0.10.0', kind: 'upgrade' }], items), '1 个待生效的切换：场景走查 0.9.1 → 0.10.0');
  assert.equal(pendingSummary([], items), '');
  assert.equal(switchToast('upgrade', by('diagram'), { effective: 'next_message', pack: { pending: { to: '0.6.0' } } }), '已升级到图示 0.6.0，下一条消息生效');
  assert.equal(switchToast('disable', by('knowledge'), { effective: 'next_message' }), '已停用「知识沉淀」，左栏的「知识沉淀」已移除，下一条消息生效');
  assert.equal(switchToast('disable', by('industry-retail'), { effective: 'immediate' }), '已停用「零售连锁」');
  assert.equal(switchToast('cancel_pending', by('trace')), '已撤销，保持 0.9.1');
});

test('MCP：工具的写法、对外工具排序、包贡献工具、权限范围、新建 Token 的校验', () => {
  assert.equal(toolsText(['evidence.search', 'evidence.show', 'evidence.where-used', 'evidence.lineage', 'evidence.diff']), 'evidence.search / show / where-used / lineage / diff');
  assert.equal(toolsText(['flow.get']), 'flow.get');
  assert.equal(toolsText([]), '—');
  assert.equal(toolsText(['a_x', 'b_y', 'c_z']), 'a_x 等 3 个');
  const tools = toolList([{ tools: ['rule_get', 'object_list', 'object_delete'] }, { tools: ['object_change_apply', 'object_get', 'object_change_validate'] }]);
  assert.deepEqual(tools, ['object_list', 'object_get', 'object_change_validate', 'object_change_apply', 'object_delete', 'rule_get']);
  assert.equal(packToolsText([{ name: 'flow.get' }, ...['search', 'show', 'where-used', 'lineage', 'diff', 'er'].map((t) => ({ name: `evidence.${t}` }))]), 'flow.get、evidence.*（6）');
  assert.deepEqual(['ontology.read', 'ontology.change.validate', 'ontology.change.apply'].map(scopeLabel), ['ontology.read', 'change.validate', 'change.apply']);
  assert.deepEqual(tokenProblems({ name: ' ', scopes: [] }), { name: '请填写名称', scopes: '至少选一个' });
  // 证据源本期不开放：EV-01 读不到时不列内置的 evidence 服务器与 evidence.* 工具；读得到时照常
  assert.deepEqual(visibleMcp(mcpServers(), false).map((s) => s.name), ['flow', 'replenish-std-lookup', 'mermaid-render']);
  assert.equal(visibleMcp(mcpServers(), true).length, 4);
  assert.deepEqual(visibleMcp([{ name: 'flow.get' }, { name: 'evidence.search' }], false).map((x) => x.name), ['flow.get']);
  assert.deepEqual(visibleMcp(undefined, false), []);
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

test('mock PL-05～PL-11：停用带页面的能力，左栏立刻少一页、发两个事件；撤销后回来', async () => {
  const { call, events, ctx } = harness();
  const list = await call('GET', `${P}/packs`);
  assert.equal(list.body.pending_switches, 1);
  assert.deepEqual(list.body.items.find((p) => p.name === 'diagram').actions, ['upgrade', 'disable']);
  assert.deepEqual(ctx.db.extensionsOf(F.MAIN_PID).nav.filter((x) => x.kind === 'plugin').map((x) => x.id), ['scenarios', 'knowledge', 'scripts']);

  const off = await call('POST', `${P}/packs/knowledge/disable`);
  assert.equal(off.status, 200);
  assert.equal(off.body.effective, 'next_message');
  assert.deepEqual(off.body.pack.pending, { from: '0.6.0', to: null, kind: 'disable', effective: 'next_message' });
  assert.deepEqual(events.map((e) => e.type), ['assembly.updated', 'extensions.updated']);
  const ext = ctx.db.extensionsOf(F.MAIN_PID);
  assert.deepEqual(ext.nav.filter((x) => x.kind === 'plugin').map((x) => x.id), ['scenarios', 'scripts']);
  assert.deepEqual(ext.disabled_pages, [{ id: 'knowledge', pack: 'knowledge', title: '知识沉淀' }]);
  assert.equal((await call('GET', `${P}/assembly`)).body.pending_changes.length, 2);

  const back = await call('POST', `${P}/packs/knowledge/cancel-pending`);
  assert.equal(back.body.pack.status, 'enabled');
  assert.ok(ctx.db.extensionsOf(F.MAIN_PID).nav.some((x) => x.id === 'knowledge'));
  assert.equal((await call('POST', `${P}/packs/knowledge/cancel-pending`)).body.code, 'no_pending_switch');
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
  const en = await call('POST', `${P}/packs`, { name: 'migrate' });
  assert.equal(en.status, 201);
  assert.deepEqual(en.body.pack.pending, { from: null, to: '0.2.0', kind: 'enable', effective: 'next_message' });
  assert.equal((await call('POST', `${P}/packs`, { name: 'knowledge' })).body.code, 'pack_already_enabled');
  assert.equal((await call('POST', `${P}/packs`, { name: 'websearch' })).body.code, 'pack_unavailable');
  // 停用场景走查（有待生效的升级）要先撤销
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

  const srv = await h.call('PUT', `${P}/mcp-servers`, { disabled: ['evidence', 'mermaid-render'] });
  assert.deepEqual(srv.body.items.map((s) => [s.name, s.enabled]), [['evidence', false], ['flow', true], ['replenish-std-lookup', true], ['mermaid-render', false]]);

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
  assert.equal(p0.platform_limits.max_duration_min, 240);

  assert.equal((await call('PUT', `${P}/autobuild/policy`, { base_revision: 'old' })).body.code, 'revision_conflict');
  const over = await call('PUT', `${P}/autobuild/policy`, { ...policyForm(p0), budget: { ...p0.budget, max_tokens: 20e6 }, base_revision: 'p3' });
  assert.equal(over.status, 422);
  assert.deepEqual([over.body.code, over.body.details.dimension], ['budget_exceeds_quota', 'tokens']);
  assert.equal((await call('PUT', `${P}/autobuild/policy`, { ...policyForm(p0), production_collection: [{ source_id: 'nope', allowed: true, window: null, scope: [] }], base_revision: 'p3' })).body.code, 'source_not_authorized');

  const form = policyForm(p0);
  form.scope = { ...form.scope, mode: 'selected', slice_ids: ['s3', 's4'] };
  form.production_collection = [{ source_id: 'erp-prod', allowed: true, window: { start: '22:00', end: '06:00' }, scope: ['补货标准表'] }];
  const saved = await call('PUT', `${P}/autobuild/policy`, policyBody(form, 'p3'));
  assert.equal(saved.status, 200);
  assert.notEqual(saved.body.revision, 'p3');
  assert.deepEqual((await call('GET', `${P}/autobuild/policy`)).body.scope.slice_ids, ['s3', 's4']);

  const pv = (await call('POST', `${P}/autobuild/runs/preview`, {})).body;
  assert.deepEqual(pv.slices.filter((s) => s.included).map((s) => s.id), ['s3', 's4']);
  assert.equal(pv.production, '允许采集 ERP 生产库（22:00 – 06:00）');
  assert.deepEqual(pv.warnings, []);
  assert.ok(pv.preview_token);

  assert.equal((await call('POST', `${P}/autobuild/runs`, {})).body.code, 'preview_expired');
  const run = await call('POST', `${P}/autobuild/runs`, { preview_token: pv.preview_token });
  assert.equal(run.status, 201);
  assert.deepEqual([run.body.run_no, run.body.state, run.body.slices.total], [4, 'queued', 2]);
  const conv = convs.get(F.MAIN_PID)[0];
  assert.deepEqual([conv.id, conv.title, conv.kind, conv.live], [run.body.conversation_id, '自主构建 · 运行 #4', 'autobuild', true]);
  const start = seeded.get(`${F.MAIN_PID}:${conv.id}`).find((e) => e.type === 'card').data;
  assert.equal(start.card_type, 'autobuild_start');
  assert.deepEqual(start.chips.map((c) => c.value), ['选中的 2 片', '¥150 · 2 小时', '允许采集 ERP 生产库（22:00 – 06:00）', '采用保守选项并记为假设']);

  const running = (await call('GET', `${P}/autobuild/runs?state=running`)).body.items;
  assert.deepEqual(running.map((r) => [r.run_no, r.state]), [[4, 'queued']]);
  assert.equal((await call('POST', `${P}/autobuild/runs/preview`, {})).body.code, 'autobuild_running');
  assert.equal((await call('PUT', `${P}/engine`, { engine: 'jiuwen' })).body.code, 'autobuild_running');
});
