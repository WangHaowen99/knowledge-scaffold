// 插件中心与插件详情（plugins、plugin-detail）：筛选与计数、卡脚按状态映射、兼容检查到弹窗文字、主线图布局、
// 「包含什么」分组，以及 mock 的 PL-01～PL-03 与项目设置能力表（PL-05）口径一致
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { compareVersions, satisfies } from '../src/api/mock/plugins-data.js';
import { compatibilityOf, register as registerPlugins } from '../src/api/mock/routes-plugins.js';
import { register as registerSettings } from '../src/api/mock/routes-settings.js';
import {
  blockedView, bringsView, cardFoot, cardTags, catOf, categoryOf, compatRows, compatView, devSkillOf, effectiveNote, enableToast, impactText, matchCard, recView,
  PACK_CATEGORIES, sectionsOf, uiChip, uiEffectText, updateRoute, updateToast, versionOf,
} from '../src/pages/plugins/catalogModel.js';
import {
  changelogView, configText, contentGroups, contentTotal, converterDefaults, dependsText, disableConfirm, flowCaption, flowLayout, fmtSize, heroActions, heroFacts, heroMenu,
  insertionAt, rangeText, statCards,
} from '../src/pages/plugins/detailModel.js';

const card = (o) => ({ name: 'x', label: 'X', category: 'capability', version: '1.0.0', latest: '1.0.0', description: '', publisher: { kind: 'official', label: '官方' }, ...o });

// ---------- 筛选、分组、计数 ----------
test('筛选：分类、已启用（含待生效）、可更新，搜索名称 / ID / 说明不分大小写', () => {
  const items = [
    card({ name: 'trace', label: '场景模拟', project_status: 'update', description: '每段多一步' }),
    card({ name: 'codegen', label: '代码生成与求解', project_status: 'available' }),
    card({ name: 'diagram', label: '图示', project_status: 'pending' }),
    card({ name: 'industry-retail', label: '零售连锁', category: 'industry', project_status: 'enabled' }),
    card({ name: 'convert-pdf', label: 'PDF 转换', category: 'provider', project_status: 'platform_installed' }),
  ];
  assert.deepEqual(items.filter((p) => matchCard(p, { cat: 'inst' })).map((p) => p.name), ['trace', 'diagram', 'industry-retail']);
  assert.deepEqual(items.filter((p) => matchCard(p, { cat: 'upd' })).map((p) => p.name), ['trace']);
  assert.deepEqual(items.filter((p) => matchCard(p, { q: 'pdf' })).map((p) => p.name), ['convert-pdf']);
  assert.deepEqual(items.filter((p) => matchCard(p, { q: '每段' })).map((p) => p.name), ['trace']);
  const secs = sectionsOf(items, { cat: 'all', q: '' });
  assert.deepEqual(secs.map((s) => [s.key, s.items.length]), [['capability', 3], ['industry', 1], ['provider', 1]]);
  assert.deepEqual(sectionsOf(items, { cat: 'industry', q: '模拟' }), []);
  assert.equal(catOf({ cat: 'upd' }), 'upd');
  assert.equal(catOf({ cat: 'provider' }), 'provider');
  assert.equal(catOf({ cat: 'nope' }), 'all');
  assert.equal(catOf({}), 'all');
});

test('多源连接器里的转换器（ADR-0073）：归在「多源连接器」分类，卡脚是平台已安装', () => {
  const conv = card({ name: 'convert-pdf', category: 'provider', project_status: 'platform_installed', converter: { types: ['.pdf'], default_for: ['.pdf'] } });
  assert.deepEqual(sectionsOf([conv, card({ name: 'trace' })], { cat: 'provider', q: '' }).map((s) => [s.key, s.items.map((p) => p.name)]), [['provider', ['convert-pdf']]]);
  assert.equal(PACK_CATEGORIES.provider[0], '多源连接器');
  assert.deepEqual(cardFoot(conv).left, { type: 'chip', text: '平台已安装', tone: 'teal', icon: 'check' });
});

test('转换器默认（PL-20 / PL-21）：只有一个转换器的类型不出；有别的时默认的标出来，管理员能改', () => {
  const d = { name: 'pdf-alt', converter: { types: ['.pdf'] } };
  const one = { types: [{ type: '.pdf', label: 'PDF', default: 'pdf-alt', converters: ['pdf-alt'] }] };
  assert.deepEqual(converterDefaults(d, one, { admin: true }), []);
  const two = { types: [{ type: '.pdf', label: 'PDF', default: 'convert-pdf', converters: ['convert-pdf', 'pdf-alt'] }] };
  assert.deepEqual(converterDefaults(d, two, { admin: true }), [{ type: '.pdf', label: 'PDF', isDefault: false, canSet: true }]);
  assert.deepEqual(converterDefaults(d, two, { admin: false }), [{ type: '.pdf', label: 'PDF', isDefault: false, canSet: false }]);
  assert.deepEqual(converterDefaults({ ...d, name: 'convert-pdf' }, two, { admin: true }), [{ type: '.pdf', label: 'PDF', isDefault: true, canSet: false }]);
  assert.deepEqual(converterDefaults({ name: 'trace' }, two), []);
  assert.deepEqual(heroActions({ project_status: 'platform_installed', category: 'provider', converter: {} }, { hasProject: true }), { primary: { label: '平台已安装', icon: 'check', disabled: true, kind: 'soft' } });
});

test('节点包归进能力包：不单列分类，旧数据里的 node 分类也算能力包', () => {
  const items = [card({ name: 'trace' }), card({ name: 'naming', label: '命名检查', category: 'node' })];
  assert.deepEqual(sectionsOf(items, { cat: 'all', q: '' }).map((s) => [s.key, s.items.length]), [['capability', 2]]);
  assert.deepEqual(items.filter((p) => matchCard(p, { cat: 'capability' })).map((p) => p.name), ['trace', 'naming']);
  assert.equal(catOf({ cat: 'node' }), 'all');
  assert.equal(categoryOf(items[1]), 'capability');
});

// ---------- 卡片 ----------
test('卡脚：8 种 project_status 的 chip 与按钮（契约 PL-01 的表）', () => {
  const foot = (st, o, extra) => cardFoot(card({ project_status: st, locked_version: '0.9.1', latest: '0.10.0', usage_note: '2 个项目在用', ...extra }), o);
  assert.deepEqual(foot('required').left, { type: 'chip', text: '必装 · 已启用', tone: 'blue', icon: 'lock' });
  assert.equal(foot('enabled').right.type, 'detail');
  const upd = foot('update');
  assert.equal(upd.left.text, '已启用 0.9.1');
  assert.deepEqual([upd.right.label, upd.right.action], ['更新到 0.10.0', 'update']);
  assert.equal(foot('available').left.action, 'enable');
  assert.equal(foot('available').left.kind, 'primary');
  assert.deepEqual([foot('offline').left.text, foot('offline').right.disabled], ['离线部署不可用', true]);
  assert.equal(foot('not_applicable', {}, { usage_note: '适用于物业服务项目' }).note, '适用于物业服务项目');
  assert.deepEqual([foot('platform_installed').left.text, foot('platform_installed').note], ['平台已安装', '2 个项目在用']);
  assert.equal(foot('pending').left.text, '已启用 · 下一条消息生效');
  assert.equal(foot('pending', { pendingKind: 'disable' }).left.text, '停用 · 下一条消息生效');
  assert.equal(foot(null).note, '先建一个项目才能启用');
});

test('卡片标记：只标带页面的能力包、提供方批次、租户发布方；分类、官方、无独立页面、可更新、后端 meta 都不标', () => {
  const cap = cardTags(card({ ui: { kind: 'page', pages: [{ id: 'scenarios', title: '场景模拟' }] }, meta: '5 命令', project_status: 'enabled' }));
  assert.deepEqual(cap.tags.map((t) => t.text), ['带页面：场景模拟']);
  assert.equal('meta' in cap, false);
  const upd = cardTags(card({ project_status: 'update', latest: '0.6.0', meta: '2 命令' }));
  assert.deepEqual(upd.tags, []);
  assert.deepEqual(cardTags(card({ ui: { kind: 'none', note: '无独立页面' } })).tags, []);
  assert.deepEqual(cardTags(card({ ui: { kind: 'kernel', note: '内核：工作台' } })).tags.map((t) => t.text), ['内核页面']);
  const prov = cardTags(card({ category: 'provider', batch: 'second', meta: 'x' }));
  assert.deepEqual(prov.tags.map((t) => t.text), ['第二批']);
  const tenant = cardTags(card({ category: 'industry', publisher: { kind: 'tenant', label: '示例租户' }, ui: null }));
  assert.deepEqual(tenant.tags, [{ text: '租户 · 示例租户', tone: 'indigo' }]);
  assert.equal(versionOf(card({ project_status: 'update', locked_version: '0.9.1', version: '0.10.0' })), '0.9.1');
  assert.equal(versionOf(card({ project_status: 'available', locked_version: '0.2.0', version: '0.2.1' })), '0.2.1');
});

test('界面标记：内核、无独立页面（悬浮说明去掉前缀）、资料页子页、左栏页面', () => {
  assert.equal(uiChip({ kind: 'kernel', note: '内核：工作台、Studio、资料、发布' }).pop, '工作台、Studio、资料、发布');
  assert.deepEqual(uiChip({ kind: 'none', note: '无独立页面 · 对话里出现流程图卡' }), { text: '无独立页面', tone: 'gray', icon: null, pop: '对话里出现流程图卡' });
  assert.equal(uiChip({ kind: 'subpages', subpages: [{ parent: 'resources' }, { parent: 'resources' }, { parent: 'resources' }] }).text, '带页面：资料页 3 个子页');
  assert.equal(uiChip({ kind: 'page', pages: [{ title: '场景模拟' }] }).pop, '启用后项目左栏「插件」分组出现「场景模拟」');
  assert.equal(uiChip(null), null);
});

test('推荐：处理过的那条换成 chip', () => {
  assert.deepEqual(recView({ action: 'enable' }, { project_status: 'available' }), { label: '启用' });
  assert.deepEqual(recView({ action: 'enable' }, { project_status: 'pending' }), { done: '已启用' });
  assert.deepEqual(recView({ action: 'upgrade' }, { project_status: 'update' }), { label: '更新' });
  assert.deepEqual(recView({ action: 'upgrade' }, { project_status: 'pending' }), { done: '已更新' });
});

// ---------- 兼容检查 → 弹窗 ----------
test('兼容检查到弹窗：通过的页面接口在不带页面的包上不列；不通过时原因与说明', () => {
  const checks = [{ id: 'kernel_api', ok: true }, { id: 'depends', label: '依赖 core ">=1.5"', ok: false, detail: '新版本在试用' }, { id: 'page_api', ok: true }];
  assert.deepEqual(compatRows(checks, { kind: 'none' }).map((c) => c.id), ['kernel_api', 'depends']);
  assert.deepEqual(compatRows(checks, { kind: 'page', pages: [] }).map((c) => c.id), ['kernel_api', 'depends', 'page_api']);
  assert.deepEqual(blockedView({ ok: false, checks, blocked_reason: '需要先升级基础建模能力' }), { reason: '需要先升级基础建模能力', details: ['新版本在试用'] });
  assert.equal(blockedView({ ok: false, checks }).reason, '依赖 core ">=1.5"');
  assert.equal(updateRoute(card({ category: 'industry' }), { ok: true, effective: 'immediate' }), 'confirm');
  assert.equal(updateRoute(card(), { ok: true, effective: 'next_message' }), 'direct');
  assert.equal(updateRoute(card({ category: 'industry' }), { ok: false }), 'blocked');
});

test('兼容检查（R13）：全部通过折成一行可展开；有不通过的默认展开、只列不通过的', () => {
  const ok = [{ id: 'kernel_api', ok: true }, { id: 'depends', ok: true }, { id: 'engines', ok: true }, { id: 'names', ok: true }, { id: 'flow', ok: true }, { id: 'page_api', ok: true }];
  const v = compatView(ok, { kind: 'none' });
  assert.deepEqual([v.ok, v.summary, v.expanded, v.rows.length], [true, '兼容检查通过（5 项）', false, 5]);
  const bad = [{ id: 'kernel_api', ok: true }, { id: 'depends', label: '依赖 core', ok: false }, { id: 'page_api', ok: false, label: '页面接口' }];
  const w = compatView(bad, { kind: 'none' });
  assert.deepEqual([w.ok, w.summary, w.expanded, w.rows.map((c) => c.id)], [false, '兼容检查不通过（2 项）', true, ['depends', 'page_api']]);
});

test('会带来什么、界面、生效时机、影响、提示语', () => {
  assert.deepEqual(bringsView({ summary_md: '- 规则 3 条\n- 样例 2 份' }), { list: ['规则 3 条', '样例 2 份'] });
  assert.deepEqual(bringsView({ summary_md: '1 个 skill。' }), { text: '1 个 skill。' });
  assert.equal(bringsView({ commands: 2, skills: 1, nodes: [{ id: 'a.b', title: 'a.b' }] }).text, '2 个命令、1 个 skill、1 个流程节点（a.b）。');
  assert.equal(uiEffectText(null, { kind: 'page', pages: [{ title: '场景模拟' }] }), '启用后左栏出现「场景模拟」');
  assert.equal(uiEffectText(null, { kind: 'none', note: '无独立页面，在对话里生效' }), '在对话里生效');
  assert.equal(uiEffectText({ text: '自定' }, null), '自定');
  assert.deepEqual(['next_message', 'immediate', 'next_safe_point', undefined].map(effectiveNote), ['下一条消息生效', '现读即生效', '下一个安全点后生效', '下一条消息生效']);
  assert.equal(impactText({ note: '原样' }), '原样');
  assert.equal(impactText({ needs_review_after_upgrade: 0, affected_artifacts: 4 }), '节点完成判定没有变化，已确认的产物不会被标为需复核。用到这个包的 4 个产物会在下次校验时按新内容检查。');
  assert.match(impactText({ needs_review_after_upgrade: 2 }), /2 个已确认的产物会标为需复核/);
  assert.equal(enableToast({ label: '图示' }, { version_label: 'v0.13' }), '已启用 图示，保存为版本 v0.13');
  assert.equal(updateToast({ label: '图示', name: 'diagram' }, '0.6.0', { effective: 'next_message' }), '已更新图示到 0.6.0，下一条消息生效');
  assert.equal(updateToast({ label: '零售连锁', name: 'industry-retail' }, '1.1.0', { effective: 'immediate' }, true), '已更新到 industry-retail 1.1.0，现读即生效');
});

// ---------- 详情 ----------
test('主线图：插入点挂到对应的段，同一段竖排，图下那句话居中在插入点之间', () => {
  const L = flowLayout([
    { id: 'trace.scenario', title: '每段多一步「场景」', at: 'model', note: '节点钩子' },
    { id: 'trace.trace', title: '场景模拟', at: 'validate' },
  ]);
  assert.equal(L.width, 746);
  assert.equal(L.height, 162); // 和设计稿 viewBox 一致
  assert.deepEqual(L.pills.map((p) => [p.at, p.x, p.y, p.lineFrom]), [['model', 204, 88, 69], ['validate', 402, 88, 69]]);
  assert.equal(L.pills[0].sub, ''); // 后端下发的节点说明不画（口径 5）
  assert.equal(L.caption.x, (198 + 396 + 150) / 2);
  const stacked = flowLayout([{ id: 'a', at: 'model' }, { id: 'b', at: 'model' }]);
  assert.deepEqual(stacked.pills.map((p) => [p.y, p.lineFrom]), [[88, 69], [140, 132]]);
  assert.equal(stacked.height, 140 + 44 + 22 + 8);
  const none = flowLayout([]);
  assert.equal(none.caption.x, 373);
  const kernel = flowLayout([{ id: 'x', at: 'model' }], { kernel: true });
  assert.equal(kernel.pills.length, 0);
  assert.ok(kernel.nodes.every((n) => n.hi));
  // 真实后端只给节点 ID：按 ID 猜段
  assert.deepEqual(['core.research', 'trace.regression', 'retail.compliance', 'foo.bar'].map((id) => insertionAt({ id })), ['research', 'validate', 'publish', 'model']);
  // 官方包的真实节点编号（各包 flow.yaml 的 after）
  assert.deepEqual(['trace.trace', 'codegen.codegen', 'codegen.solver', 'diagram.flowchart'].map((id) => insertionAt({ id })), ['validate', 'validate', 'model', 'research']);
  // 图下只用包自带的说明；处数和统计卡、图本身重复，不写
  assert.equal(flowCaption({ flow_insertions: [{}, {}], flow_note: '示例' }), '示例');
  assert.equal(flowCaption({ flow_insertions: [{}, {}] }), '');
  assert.equal(flowCaption({ flow_insertions: [] }), '这个包不往流程里加步骤');
  assert.equal(flowCaption({ flow_note: '主线' }, true), '主线');
  assert.equal(flowCaption({}, true), '');
});

test('包含什么：mock 的完整分组原样，真实后端的 contents 补上加进流程、界面入口、可调设置', () => {
  const mock = contentGroups({
    contents: [{ group: '加进流程', items: ['a', 'b'], note: 'n' }, { group: '命令', items: ['c'] }, { group: '术语', total: 312, items: ['x'] }],
    config: [{ label: '回归模式', value: '开' }],
    ui: { kind: 'page', pages: [{ title: '场景模拟' }] },
  });
  assert.deepEqual(mock.map((g) => [g.title, g.count]), [['加进流程', 2], ['命令', 1], ['术语', 312], ['界面入口', 1], ['可调设置', 1]]);
  assert.equal(mock[4].items[0], '回归模式 开');
  assert.equal(mock[4].note, null);
  assert.equal(contentTotal(mock), 317);
  const backend = contentGroups({
    flow_insertions: [{ id: 'trace.scenario', title: 'trace.scenario' }],
    contents: [{ group: '命令', items: ['trace-run'] }, { group: '子代理', items: ['tracer'] }],
    config: [{ id: 'max_per_slice', default: 6 }, 'regression_mode'],
    ui: { kind: 'subpages', subpages: [{ title: '台账' }] },
  });
  assert.deepEqual(backend.map((g) => g.title), ['加进流程', '命令', '子代理', '界面入口', '可调设置']);
  assert.deepEqual(backend[4].items, ['max_per_slice 6', 'regression_mode']);
  assert.deepEqual(contentGroups({}), []);
  assert.equal(configText(null), '');
});

test('英雄区：依赖、大小、日期；按钮与菜单随状态变', () => {
  assert.equal(rangeText('>=1.4'), '1.4 以上');
  assert.equal(rangeText('>=1.0 <2.0'), '1.0 以上、2.0 以下');
  assert.equal(dependsText({ core: '>=1.4' }), '基础建模能力 1.4 以上');
  assert.equal(fmtSize(186 * 1024), '186 KB');
  assert.equal(fmtSize(2.5 * 1024 * 1024), '2.5 MB');
  const d = {
    category: 'capability', project_status: 'update', latest: '0.10.0', locked_version: '0.9.1', ui: { kind: 'page', pages: [{ title: '场景模拟' }] },
    requires: { depends: { core: '>=1.4' } }, size: 190464, released_at: '2026-10-12T10:00:00.000Z', enabled_at: null,
  };
  assert.deepEqual(heroFacts(d), [['需要', '基础建模能力 1.4 以上'], ['发布', '2026-10-12']]);
  const h = heroActions(d, { hasProject: true });
  assert.deepEqual([h.menu.label, h.primary.label, h.hint], ['已启用到本项目', '更新到 0.10.0', undefined]);
  // R14：下拉菜单只写动作，停用的后果在确认框里说一次
  const menu = heroMenu(d);
  assert.deepEqual(menu.map((x) => (x === '-' ? x : x.label)), ['停用', '只在下次自主构建时启用新版本', '-', '在项目设置里查看']);
  assert.equal(menu[1].disabled, false);
  assert.deepEqual(disableConfirm({ ...d, label: '场景模拟' }), { title: '停用场景模拟？', body: '左栏「场景模拟」页面会消失，已有产物保留。' });
  assert.equal(disableConfirm({ label: '零售连锁', category: 'industry' }).body, '已有产物保留。');
  assert.equal(heroMenu({ ...d, project_status: 'enabled' })[1].disabled, true);
  assert.equal(heroMenu({ ...d, project_status: 'required' })[0].disabled, true);
  const pend = heroMenu({ ...d, project_status: 'pending' }, { pending: { kind: 'disable', from: '0.9.1' } });
  assert.deepEqual(pend.map((x) => (x === '-' ? x : x.key)), ['disable', 'cancel_pending', 'next_autobuild', '-', 'settings']);
  assert.equal(pend[0].disabled, true);
  assert.equal(heroActions({ ...d, project_status: 'pending' }, { hasProject: true, pending: { kind: 'upgrade', to: '0.10.0' } }).menu.label, '0.10.0 · 下一条消息生效');
  assert.equal(heroActions({ ...d, project_status: 'available' }, { hasProject: true }).primary.action, 'enable');
  assert.equal(heroActions(d, { hasProject: false }).primary, undefined);
});

test('统计卡：project_stats 为 null 时显示「—」；更新记录标记', () => {
  // 「加进流程」那格和主线图重复，只留两格
  const s = statCards({ category: 'capability', project_status: 'enabled', flow_insertions: [{ short: '段内「场景」' }, { title: '场景模拟' }], project_stats: null }, { hasProject: true });
  assert.deepEqual(s.map((x) => [x.label, x.value]), [['在本项目产出', '—'], ['运行情况', '—']]);
  assert.equal(s[0].desc, '暂时没有统计');
  const withStats = statCards({ project_status: 'enabled', project_stats: { produced: { value: 23, unit: '个场景', note: 'n' }, metric: { label: '推演通过率', value: '83%', note: 'm' } } }, { hasProject: true });
  assert.deepEqual(withStats.map((x) => [x.label, x.value]), [['在本项目产出', 23], ['推演通过率', '83%']]);
  assert.deepEqual(statCards({ ui: { kind: 'kernel' } }).map((x) => x.label), ['在本项目产出', '运行情况']);
  const cl = changelogView([{ version: '0.10.0', date: '2026-10-28T10:00:00.000Z', notes: 'n', tags: ['needs_core_upgrade'] }, { version: '0.9.1', tags: ['in_use'] }]);
  assert.deepEqual(cl.map((c) => c.chips.map((x) => x.text)), [['需要先升级基础建模能力'], ['本项目在用']]);
  assert.equal(cl[0].date, '2026-10-28');
});

// ---------- mock：PL-01～PL-03 与 PL-05 ----------
function harness({ sc = {}, user = F.USERS.zhangsan } = {}) {
  const routes = [];
  const cards = F.projectCards();
  let n = 0;
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user, init: {} });
      if (res !== undefined) return res;
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json,
    err: (status, code, message, details = null) => json(status, { code, message, details }),
    db: {},
    sc,
    uid: () => (n += 1).toString(16),
    F,
    cardOf: (id) => cards.find((c) => c.id === id),
    emitProj: () => {},
    emitUser: () => {},
    forbid: (u, roles) => (roles.some((r) => (u?.roles || [u?.role]).includes(r)) ? undefined : json(403, { code: 'forbidden' })),
    self: (url, init) => call(init?.method || 'GET', url, init?.body),
  };
  registerSettings(ctx);
  registerPlugins(ctx);
  const api = async (method, path, body) => {
    const res = await call(method, path, body);
    return { status: res.status, body: await res.json() };
  };
  return { api, ctx };
}
const P = F.MAIN_PID;

test('mock PL-01 不带项目：新建向导的结果不变（名字、版本、默认开启），不列租户包', async () => {
  const { api } = harness();
  const r = await api('GET', '/api/v1/packs?category=industry,system,capability');
  const pick = (p) => [p.name, p.latest, p.version, p.default_on, p.project_status];
  assert.deepEqual(r.body.items.map(pick), F.packs().map(pick));
  assert.equal(r.body.recommendations, undefined);
  const all = await api('GET', '/api/v1/packs');
  assert.equal(all.body.counts.provider, 3); // 3 个转换器（ADR-0073）
  assert.equal(all.body.items.find((p) => p.name === 'convert-pdf').project_status, 'platform_installed');
  assert.deepEqual([all.body.counts.enabled, all.body.counts.updatable], [0, 0]);
  assert.ok(!all.body.items.some((p) => p.name === 'property-compliance'));
  assert.equal((await api('GET', '/api/v1/packs?category=bogus')).status, 400);
});

test('mock PL-01 带项目：状态按项目设置的能力表算，计数、推荐', async () => {
  const { api } = harness();
  const r = await api('GET', `/api/v1/packs?project_id=${P}`);
  const st = Object.fromEntries(r.body.items.map((p) => [p.name, p.project_status]));
  assert.equal(st.core, 'required');
  assert.equal(st.trace, 'pending'); // 项目设置里有一个待生效的升级
  assert.equal(st.diagram, 'update');
  assert.equal(r.body.items.find((p) => p.name === 'diagram').latest, '0.6.0');
  assert.equal(st['industry-property'], 'available'); // 没启用过
  assert.equal(st['property-compliance'], 'not_applicable');
  assert.equal(r.body.items.find((p) => p.name === 'property-compliance').usage_note, '适用于物业服务项目');
  assert.equal(st['convert-pdf'], 'platform_installed');
  assert.equal(r.body.items.find((p) => p.name === 'convert-pdf').usage_note, null);
  assert.deepEqual(r.body.counts, { all: 14, capability: 4, industry: 4, system: 3, provider: 3, node: 0, exporter: 0, enabled: 6, updatable: 1 });
  assert.deepEqual(r.body.recommendations.map((x) => [x.pack.name, x.action]), [['diagram', 'upgrade']]);
  const inst = await api('GET', `/api/v1/packs?project_id=${P}&installed_only=true&q=模拟`);
  assert.deepEqual(inst.body.items.map((p) => p.name), ['trace']);
  assert.equal((await api('GET', '/api/v1/packs?project_id=nope')).status, 404);
  const other = await api('GET', '/api/v1/packs?project_id=bike-fleet-7b20e4a1');
  assert.deepEqual(other.body.recommendations, []);
});

test('mock：插件中心启用后，项目设置的能力表是同一个状态', async () => {
  const { api } = harness({ sc: { off: 'codegen' } });
  const before = await api('GET', `/api/v1/packs?project_id=${P}`);
  assert.equal(before.body.items.find((p) => p.name === 'codegen').project_status, 'available'); // 停用过
  const en = await api('POST', `/api/v1/projects/${P}/packs`, { name: 'codegen' });
  assert.equal(en.status, 201);
  const r = await api('GET', `/api/v1/packs?project_id=${P}`);
  assert.equal(r.body.items.find((p) => p.name === 'codegen').project_status, 'pending');
  const pl05 = await api('GET', `/api/v1/projects/${P}/packs`);
  assert.equal(pl05.body.items.find((p) => p.name === 'codegen').status, 'pending');
  // 行业包现读即生效：启用物业服务后，只适用于物业服务项目的租户包变成可启用，也能经 PL-06 启用
  await api('POST', `/api/v1/projects/${P}/packs`, { name: 'industry-property' });
  const r2 = await api('GET', `/api/v1/packs?project_id=${P}`);
  assert.equal(r2.body.items.find((p) => p.name === 'industry-property').project_status, 'enabled');
  assert.equal(r2.body.items.find((p) => p.name === 'property-compliance').project_status, 'available');
  assert.equal((await api('POST', `/api/v1/projects/${P}/packs`, { name: 'property-compliance' })).status, 201);
});

test('mock ?mock=plugins:held+ind+norec：场景模拟没排升级且更新被挡，零售连锁可更新，推荐为空', async () => {
  const { api } = harness({ sc: { plugins: 'held ind norec' } });
  const r = await api('GET', `/api/v1/packs?project_id=${P}`);
  const st = Object.fromEntries(r.body.items.map((p) => [p.name, p]));
  assert.equal(st.trace.project_status, 'update');
  assert.equal(st.trace.latest, '0.10.0');
  assert.equal(st['industry-retail'].project_status, 'update');
  assert.equal(st['industry-retail'].latest, '1.1.0');
  assert.equal(r.body.counts.updatable, 3);
  assert.deepEqual(r.body.recommendations, []);
  // 项目设置看到的是同一件事
  const pl05 = await api('GET', `/api/v1/projects/${P}/packs`);
  assert.equal(pl05.body.items.find((p) => p.name === 'trace').pending, null);
  assert.equal(pl05.body.pending_switches, 0);
  const c = await api('GET', `/api/v1/packs/trace/versions/0.10.0/compatibility?project_id=${P}`);
  assert.equal(c.body.ok, false);
  assert.equal(c.body.blocked_reason, '需要先升级基础建模能力');
  assert.deepEqual(c.body.checks.filter((x) => !x.ok).map((x) => x.id), ['depends']);
  const ind = await api('GET', `/api/v1/packs/industry-retail/versions/1.1.0/compatibility?project_id=${P}`);
  assert.equal(ind.body.ok, true);
  assert.equal(ind.body.effective, 'immediate');
  assert.match(ind.body.brings.summary_md, /^- 临期损耗规则 3 条/);
  assert.equal(ind.body.impact.affected_artifacts, 4);
  const up = await api('POST', `/api/v1/projects/${P}/packs/industry-retail/upgrade`, { to_version: '1.1.0' });
  assert.equal(up.body.effective, 'immediate');
  const after = await api('GET', `/api/v1/packs?project_id=${P}`);
  assert.equal(after.body.items.find((p) => p.name === 'industry-retail').project_status, 'enabled');
  const withRecs = harness({ sc: { plugins: 'ind' } });
  const recs = await withRecs.api('GET', `/api/v1/packs?project_id=${P}`);
  assert.deepEqual(recs.body.recommendations.map((x) => x.pack.name), ['industry-retail', 'diagram']);
});

test('mock PL-03：六项检查齐全；启用代码生成与求解的说法；参数校验', async () => {
  const { api } = harness({ sc: { off: 'codegen' } });
  const c = await api('GET', `/api/v1/packs/codegen/versions/0.9.2/compatibility?project_id=${P}`);
  assert.equal(c.status, 200);
  assert.deepEqual(c.body.checks.map((x) => x.id), ['kernel_api', 'engines', 'depends', 'names', 'flow', 'page_api']);
  assert.equal(c.body.checks.find((x) => x.id === 'flow').detail, '新增旁支节点「代码生成」、旁支节点「求解适配」，无环、输出路径不重复');
  assert.equal(c.body.checks.find((x) => x.id === 'depends').detail, '已锁定 core@1.4.2');
  assert.equal(c.body.brings.summary_md, '命令 3 个、助手角色 1 个、模板 2 个、检查规则 1 条、流程步骤 2 个。');
  assert.equal(c.body.effective, 'next_message');
  assert.equal(c.body.ui_effect.text, '启用后左栏出现「代码生成」');
  assert.equal((await api('GET', '/api/v1/packs/codegen/versions/0.9.2/compatibility')).status, 400);
  assert.equal((await api('GET', `/api/v1/packs/codegen/versions/9.9.9/compatibility?project_id=${P}`)).body.code, 'pack_version_not_found');
  assert.equal((await api('GET', `/api/v1/packs/nope/versions/1.0.0/compatibility?project_id=${P}`)).body.code, 'pack_not_found');
  // 纯函数：升级时用这个版本的更新说明
  const d = { flow_insertions: [], contents: [], changelog: [{ version: '0.6.0', notes: '泳道' }] };
  const r = compatibilityOf({ name: 'diagram', label: '图示', category: 'capability', ui: { kind: 'none', note: '无独立页面' } }, '0.6.0', d, {
    items: [{ name: 'core', status: 'required', locked_version: '1.4.2' }, { name: 'diagram', status: 'enabled', locked_version: '0.5.0' }], flags: new Set(),
  });
  assert.equal(r.brings.summary_md, '- 泳道');
  assert.equal(r.checks.find((x) => x.id === 'names').detail, '和已启用的 1 个包无冲突');
});

test('mock PL-02：详情、在用 / 需要先升级的标记、统计只给主示例项目，找不到时 404', async () => {
  const { api } = harness({ sc: { plugins: 'held' } });
  const d = await api('GET', `/api/v1/packs/trace?project_id=${P}`);
  assert.equal(d.body.version, '0.9.1');
  assert.equal(d.body.project_status, 'update');
  assert.deepEqual(d.body.requires.depends, { core: '>=1.4' });
  assert.deepEqual(d.body.flow_insertions.map((x) => x.at), ['validate']);
  assert.deepEqual(d.body.changelog.map((c) => [c.version, c.tags]), [['0.10.0', ['needs_core_upgrade']], ['0.9.1', ['in_use']], ['0.9.0', []], ['0.8.2', []]]);
  assert.equal(d.body.project_stats.produced.value, 23);
  assert.equal(d.body.hot_level, 'H1');
  const other = await api('GET', '/api/v1/packs/trace?project_id=bike-fleet-7b20e4a1');
  assert.equal(other.body.project_stats, null);
  const ind = await api('GET', `/api/v1/packs/industry-retail?project_id=${P}`);
  assert.deepEqual([ind.body.hot_level, ind.body.effective], ['H0', 'immediate']);
  // 转换器装在连接器网关里：热加载等级 H2，不按项目统计
  const conv = await api('GET', `/api/v1/packs/convert-pdf?project_id=${P}`);
  assert.deepEqual([conv.body.project_status, conv.body.hot_level, conv.body.project_stats], ['platform_installed', 'H2', null]);
  const core = await api('GET', `/api/v1/packs/core?project_id=${P}`);
  assert.equal(core.body.ui.kind, 'kernel');
  assert.match(core.body.project_stats.produced.note, /^对象 15/);
  for (const name of ['codegen', 'diagram', 'industry-property', 'sys-wms-generic', 'convert-docx', 'convert-xlsx']) {
    assert.equal((await api('GET', `/api/v1/packs/${name}?project_id=${P}`)).status, 200, name);
  }
  assert.equal((await api('GET', '/api/v1/packs/nope')).body.code, 'pack_not_found');
  assert.equal((await api('GET', '/api/v1/packs/trace?version=7.0.0')).body.code, 'pack_version_not_found');
});

test('版本比较与范围', () => {
  assert.equal(compareVersions('0.10.0', '0.9.1'), 1);
  assert.equal(compareVersions('1.4.2', '1.4.2+3f9a1c2d'), 0);
  assert.ok(satisfies('1.4.2', '>=1.4'));
  assert.ok(!satisfies('1.4.2', '>=1.5'));
  assert.ok(satisfies('1.2.0', '>=1.0 <2.0'));
  assert.ok(!satisfies('2.0.0', '>=1.0 <2.0'));
});

test('publisherChip：上传时没填租户名的包只标「租户」（V 阶段：卡片上是「租户 ·」）', async () => {
  const { publisherChip } = await import('../src/pages/plugins/catalogModel.js');
  assert.equal(publisherChip({ publisher: { kind: 'tenant', label: '' } }).text, '租户');
  assert.equal(publisherChip({ publisher: { kind: 'tenant', label: '示例租户' } }).text, '租户 · 示例租户');
  assert.equal(publisherChip({ publisher: { kind: 'tenant', label: '租户 A' } }).text, '租户 A');
});

// ---------- 开发 skill（PL-27）----------
test('开发 skill：四个分类有下载，全部、已启用、可更新、导出器没有', () => {
  for (const cat of ['capability', 'industry', 'system', 'provider']) {
    assert.deepEqual(devSkillOf(cat), { path: `/packs/dev-skills/${cat}`, filename: `onto-plugin-dev-${cat}.zip` });
  }
  for (const cat of ['all', 'inst', 'upd', 'exporter', undefined]) assert.equal(devSkillOf(cat), null);
  assert.equal(devSkillOf(catOf({ cat: 'system' })).path, '/packs/dev-skills/system');
  assert.equal(devSkillOf(catOf({})), null);
});

test('mock PL-27：四个分类给 zip 和文件名，导出器和不认识的分类 404', async () => {
  const { ctx } = harness();
  const res = await ctx.self('/api/v1/packs/dev-skills/provider');
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('Content-Type'), 'application/zip');
  assert.match(res.headers.get('Content-Disposition'), /filename="onto-plugin-dev-provider\.zip"/);
  assert.ok((await res.arrayBuffer()).byteLength > 0);
  assert.equal((await ctx.self('/api/v1/packs/dev-skills/exporter')).status, 404);
  assert.equal((await ctx.self('/api/v1/packs/dev-skills/nope')).status, 404);
});
