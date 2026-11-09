// ADR-0087 / 契约 F17：工作台插件开发模式的纯逻辑
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canEditFile, cardDiffRows, defaultFile, diffRows, draftCardHead, draftCardMode, fixMessage, groupFiles, isDraftCard, knowledgeTags,
  latestDraftCards, menuConversations, nodePlacementText, pickConversation, planEnterPlugin, planLeavePlugin,
  pluginReturn, pluginSteps, precheckChip, returnTarget, splitPrecheck,
} from '../src/pages/workbench/pluginModeModel.js';

const conv = (id, kind, updated_at, extra = {}) => ({ id, kind, updated_at, title: id, ...extra });
const ITEMS = [
  conv('p2', 'plugin', '2026-11-09T10:05:00Z'),
  conv('c1', 'chat', '2026-11-09T10:00:00Z'),
  conv('p1', 'plugin', '2026-11-09T09:00:00Z'),
  conv('a1', 'autobuild', '2026-11-08T09:00:00Z'),
  conv('c2', 'chat', '2026-11-08T08:00:00Z'),
];

const draft = (over = {}) => ({
  name: 'naming-check', label: '命名检查', kind: 'capability', category: 'capability', version: '0.1.0', manifest_ok: true,
  file_count: 5, files: [], updated_at: '2026-11-09T14:04:00', conversation_id: 'p2',
  contents: { nodes: [], commands: [], agents: [], skills: [], scripts: [], knowledge: null },
  precheck: { state: 'passed', summary: { pass: 14, warn: 0, fail: 0 }, items: [] },
  install: { state: 'not_installed', enabled_version: null, installed_versions: [], next_version: '0.1.0', changes: [], diff: null },
  ...over,
});

test('不带 ?c= 进工作台：缺省选最近的非插件对话；在线的是插件对话时也跳过', () => {
  assert.equal(pickConversation(ITEMS, {}).id, 'c1');
  assert.equal(pickConversation(ITEMS, { liveId: 'p2' }).id, 'c1');
  assert.equal(pickConversation(ITEMS, { liveId: 'c2' }).id, 'c2');
  // 地址指定的插件对话照进（链接进插件开发）
  assert.equal(pickConversation(ITEMS, { preferredId: 'p1' }).id, 'p1');
  assert.equal(pickConversation(ITEMS, { preferAutobuild: true }).id, 'a1');
  // 只剩插件对话时退回第一条
  assert.equal(pickConversation([conv('p9', 'plugin')], {}).id, 'p9');
  assert.equal(pickConversation([], {}), null);
});

test('选「插件开发」：在跑、排队、自主构建在跑时不切；否则进最近的插件对话，没有就新建', () => {
  assert.deepEqual(planEnterPlugin({ items: ITEMS, sessionState: 'running' }), { action: 'busy', reason: 'session' });
  assert.deepEqual(planEnterPlugin({ items: ITEMS, sessionState: 'queued' }), { action: 'busy', reason: 'session' });
  assert.deepEqual(planEnterPlugin({ items: ITEMS, sessionState: 'idle', autobuildRunning: true }), { action: 'busy', reason: 'autobuild' });
  assert.deepEqual(planEnterPlugin({ items: ITEMS, sessionState: 'idle' }), { action: 'open', id: 'p2' });
  // 列表顺序和更新时间不一致时按更新时间
  assert.deepEqual(planEnterPlugin({ items: [ITEMS[2], ITEMS[0]], sessionState: 'closed' }), { action: 'open', id: 'p2' });
  assert.deepEqual(planEnterPlugin({ items: [ITEMS[1]], sessionState: 'idle' }), { action: 'create' });
});

test('离开插件开发：回最近的建模对话（不回自主构建的运行对话），没有就新建；在跑时不切', () => {
  assert.deepEqual(planLeavePlugin({ items: ITEMS, sessionState: 'idle' }), { action: 'open', id: 'c1' });
  assert.deepEqual(planLeavePlugin({ items: [ITEMS[0], ITEMS[3]], sessionState: 'idle' }), { action: 'create' });
  assert.deepEqual(planLeavePlugin({ items: ITEMS, sessionState: 'running' }), { action: 'busy', reason: 'session' });
  // 旧数据没有 kind 的按建模对话算
  assert.deepEqual(planLeavePlugin({ items: [{ id: 'old' }], sessionState: 'idle' }), { action: 'open', id: 'old' });
});

test('会话菜单：插件对话里只列插件对话，建模对话里不列插件对话', () => {
  assert.deepEqual(menuConversations(ITEMS, true).map((c) => c.id), ['p2', 'p1']);
  assert.deepEqual(menuConversations(ITEMS, false).map((c) => c.id), ['c1', 'a1', 'c2']);
});

test('插件三步：没草稿全空；预检过了没装 → 安装进行中', () => {
  assert.deepEqual(pluginSteps(null).map((s) => s.st), ['', '', '']);
  const s = pluginSteps(draft());
  assert.deepEqual(s.map((x) => x.st), ['ok', 'ok', 'pending']);
  assert.deepEqual(s.map((x) => x.label), ['写草稿', '检查', '安装']);
});

test('插件三步：预检没过 → 检查红叉带项数，安装没开始', () => {
  const s = pluginSteps(draft({ precheck: { state: 'failed', summary: { pass: 12, warn: 0, fail: 2 }, items: [] } }));
  assert.deepEqual(s.map((x) => x.st), ['ok', 'fail', '']);
  assert.equal(s[1].x, '2 项');
  // summary 缺时按失败项数
  const t = pluginSteps(draft({ precheck: { state: 'failed', items: [{ result: 'fail' }, { result: 'pass' }] } }));
  assert.equal(t[1].x, '1 项');
});

test('插件三步：装了且最新 → 安装完成带版本号；装了但草稿改过 → 安装进行中「待更新」', () => {
  const done = pluginSteps(draft({ install: { state: 'installed', enabled_version: '0.1.0' } }));
  assert.equal(done[2].st, 'ok');
  assert.equal(done[2].x, '0.1.0');
  const upd = pluginSteps(draft({ version: '0.1.1', install: { state: 'outdated', enabled_version: '0.1.0', changes: [{ path: 'a', status: 'modified' }] } }));
  assert.equal(upd[2].st, 'pending');
  assert.equal(upd[2].x, '待更新');
});

test('草稿卡按钮：旧卡、没取到状态只显示快照', () => {
  assert.equal(draftCardMode(draft(), { latest: false }), 'snapshot');
  assert.equal(draftCardMode(null, { latest: true }), 'snapshot');
});

test('草稿卡按钮：过了没装 → 安装；没过 → 修复；已装最新 → 停用 / 切回；改过 → 更新', () => {
  assert.equal(draftCardMode(draft(), { latest: true }), 'install');
  assert.equal(draftCardMode(draft({ precheck: { state: 'failed', items: [] } }), { latest: true }), 'fix');
  assert.equal(draftCardMode(draft({ install: { state: 'installed', enabled_version: '0.1.0' } }), { latest: true }), 'installed');
  assert.equal(draftCardMode(draft({ install: { state: 'outdated', enabled_version: '0.1.0' } }), { latest: true }), 'update');
  // 安装时静态检查没过（422）：按修复处理
  assert.equal(draftCardMode(draft(), { latest: true, installFailed: [{ id: 'x', result: 'fail' }] }), 'fix');
  // 改过但预检没过：先修
  assert.equal(draftCardMode(draft({ precheck: { state: 'failed', items: [] }, install: { state: 'outdated', enabled_version: '0.1.0' } }), { latest: true }), 'fix');
});

test('草稿卡头部：草稿、已装、待更新、旧卡快照各自的眉题、标和副标题', () => {
  const now = Date.parse('2026-11-09T18:00:00');
  const h = draftCardHead(draft(), {}, 'install', now);
  assert.deepEqual(h, { eyebrow: '能力包 · 草稿', title: '命名检查', chip: { tone: 'line', text: '0.1.0' }, sub: '5 个文件 · 14:04 更新' });
  const inst = draftCardHead(draft({ install: { state: 'installed', enabled_version: '0.1.0' } }), {}, 'installed', now);
  assert.equal(inst.eyebrow, '能力包');
  assert.deepEqual(inst.chip, { tone: 'teal', icon: 'check', text: '已装 0.1.0' });
  // 改过：标的是再装会用的版本号（0.1.0 已在仓库里，升到 0.1.1）
  const upd = draftCardHead(draft({ install: { state: 'outdated', enabled_version: '0.1.0', next_version: '0.1.1', changes: [{ path: 'scripts/a.py', status: 'modified' }] } }), {}, 'update', now);
  assert.equal(upd.sub, '已装 0.1.0 · 改了 1 个文件');
  assert.equal(upd.chip.text, '0.1.1');
  // 不是今天改的带日期
  assert.equal(draftCardHead(draft({ updated_at: '2026-11-07T09:05:00' }), {}, 'install', now).sub, '5 个文件 · 11-07 09:05 更新');
  // 行业包、系统知识包
  assert.equal(draftCardHead(draft({ category: 'industry' }), {}, 'install', now).eyebrow, '行业包 · 草稿');
  assert.equal(draftCardHead(draft({ category: 'system' }), {}, 'install', now).eyebrow, '系统知识包 · 草稿');
  // 旧卡：按卡片快照
  const snap = draftCardHead(draft({ version: '0.1.2' }), { name: 'naming-check', version: '0.1.0', changed_files: ['a', 'b'] }, 'snapshot', now);
  assert.deepEqual(snap, { eyebrow: '能力包 · 草稿', title: '命名检查', chip: { tone: 'line', text: '0.1.0' }, sub: '改了 2 个文件' });
  assert.equal(draftCardHead(null, { name: 'x' }, 'snapshot', now).title, 'x');
});

test('流程行文字：排在锚点前后，挡发布时加一句', () => {
  assert.equal(nodePlacementText({ after_title: '校验', blocks_publish: true }), '排在「校验」后面，没过挡住发布');
  assert.equal(nodePlacementText({ after_title: '校验', blocks_publish: false }), '排在「校验」后面');
  assert.equal(nodePlacementText({ after_title: null, before_title: '发布' }), '排在「发布」前面');
  assert.equal(nodePlacementText({ blocks_publish: true }), '没过挡住发布');
  assert.equal(nodePlacementText({}), '');
});

test('预检标、失败项拆分、知识条数', () => {
  assert.deepEqual(precheckChip({ state: 'passed', summary: { pass: 14, warn: 0, fail: 0 } }), { tone: 'teal', icon: 'check', text: '14 项通过' });
  assert.deepEqual(precheckChip({ state: 'failed', summary: { pass: 12, warn: 0, fail: 2 } }), { tone: 'red', icon: 'x', text: '2 项没过' });
  assert.equal(precheckChip(null), null);
  const { failed, rest } = splitPrecheck([{ id: 'a', result: 'fail' }, { id: 'b', result: 'pass' }, { id: 'c', result: 'warn' }]);
  assert.deepEqual(failed.map((i) => i.id), ['a']);
  assert.deepEqual(rest.map((i) => i.id), ['b', 'c']);
  assert.deepEqual(knowledgeTags({ concepts: 12, checks: 0, standards: 3, files: 2 }), ['12 个概念', '3 条标准', '2 个文件']);
  assert.deepEqual(knowledgeTags(null), []);
});

test('「让助手修复」的话：逐项「名称：说明」', () => {
  assert.equal(
    fixMessage([{ label: '名字和平台上的包重名', detail: 'naming-check 已被平台上的另一个包使用' }, { label: '依赖的版本平台上没有', detail: '' }]),
    '预检没过：名字和平台上的包重名：naming-check 已被平台上的另一个包使用；依赖的版本平台上没有。请修好，再跑一遍本地检查。',
  );
});

test('每份草稿最新的一张卡；plugin_draft 卡按 card_type 或 kind 认', () => {
  const card = (k, name, type = 'card_type') => ({ kind: 'card', card: { [type]: 'plugin_draft', name } });
  const state = {
    order: ['card:1', 'msg:1', 'card:2', 'card:3', 'card:4'],
    items: {
      'card:1': card(1, 'a'), 'msg:1': { kind: 'user' }, 'card:2': card(2, 'b', 'kind'), 'card:3': card(3, 'a', 'kind'),
      'card:4': { kind: 'card', card: { card_type: 'round_changes' } },
    },
  };
  assert.deepEqual(latestDraftCards(state), { a: 'card:3', b: 'card:2' });
  assert.equal(isDraftCard({ kind: 'plugin_draft' }), true);
  assert.equal(isDraftCard({ card_type: 'round_changes' }), false);
});

test('文件分组：根下在前（清单最前、校验和最后），目录按名称', () => {
  const g = groupFiles([
    { path: 'checksums.sha256' }, { path: 'scripts/check_names.py' }, { path: 'flow.yaml' }, { path: 'commands/naming-check.md' }, { path: 'manifest.yaml' },
  ]);
  assert.deepEqual(g.map((x) => x.dir), ['', 'commands/', 'scripts/']);
  assert.deepEqual(g[0].files.map((f) => f.name), ['manifest.yaml', 'flow.yaml', 'checksums.sha256']);
  assert.equal(g[2].files[0].name, 'check_names.py');
  assert.deepEqual(groupFiles([{ path: 'a/b.md' }]).map((x) => x.dir), ['a/']);
});

test('抽屉默认文件、能不能编辑、diff 行', () => {
  const d = draft({ files: [{ path: 'manifest.yaml' }, { path: 'flow.yaml' }, { path: 'scripts/a.py' }], install: { changes: [{ path: 'scripts/a.py', status: 'modified' }], diff: { path: 'scripts/a.py', lines: [] } } });
  assert.equal(defaultFile(d, false), 'flow.yaml');
  assert.equal(defaultFile(d, true), 'scripts/a.py');
  assert.equal(defaultFile(draft({ files: [{ path: 'manifest.yaml' }] }), false), 'manifest.yaml');
  assert.equal(canEditFile('flow.yaml', { content: 'x', binary: false }), true);
  assert.equal(canEditFile('checksums.sha256', { content: 'x', binary: false }), false);
  assert.equal(canEditFile('big.md', { content: null, binary: false }), false);
  assert.equal(canEditFile('logo.png', { content: null, binary: true }), false);
  assert.deepEqual(diffRows({ path: 'a', lines: [{ op: ' ', text: 'a' }, { op: '-', text: 'b' }, { op: '+', text: 'c' }] }), [
    { kind: '', mark: '', text: 'a' }, { kind: 'del', mark: '−', text: 'b' }, { kind: 'add', mark: '+', text: 'c' },
  ]);
});

test('切回建模试用的记录：还在、还是插件对话才回得去', () => {
  const store = new Map();
  const ss = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
  pluginReturn.set(ss, 'p0', 'p2');
  assert.equal(pluginReturn.get(ss, 'p0'), 'p2');
  assert.equal(pluginReturn.get(ss, 'other'), null);
  assert.equal(returnTarget(ITEMS, 'p2'), 'p2');
  assert.equal(returnTarget(ITEMS, 'c1'), null);
  assert.equal(returnTarget(ITEMS, 'gone'), null);
  pluginReturn.clear(ss, 'p0');
  assert.equal(pluginReturn.get(ss, 'p0'), null);
  // 没有 sessionStorage 时不报错
  assert.equal(pluginReturn.get(undefined, 'p0'), null);
});

test('草稿卡上的改动只放第一处改动附近的一小段', () => {
  const lines = Array.from({ length: 30 }, (_, i) => ({ op: i === 10 ? '+' : ' ', text: `第 ${i} 行` }));
  const rows = cardDiffRows({ path: 'commands/a.md', lines });
  assert.equal(rows.length, 13);
  assert.equal(rows[0].text, '第 8 行');
  assert.equal(rows[2].kind, 'add');
  assert.deepEqual(rows[12], { kind: 'more', mark: '', text: '…' });
  assert.equal(cardDiffRows({ path: 'a', lines: lines.slice(0, 5) }).length, 5);
});
