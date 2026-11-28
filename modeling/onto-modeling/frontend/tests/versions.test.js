// 版本历史与对比：页面纯逻辑（versionsModel）与 mock（V-01～V-08、F-06）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register as registerVersions } from '../src/api/mock/routes-versions.js';
import { mainHistory, undoChange } from '../src/api/mock/versions-data.js';
import {
  compareFromOf, compareOptions, defaultCompare, diffLines, editBlocks, entryPath, pathLabel, pathLocked, pickLanding, previousOf,
  rangeChoices, rangeValue, revertBlockedText, revertConfirmText, selfRange, srcClass, srcLabel, syncMeta, targetLabel, undoneText,
  versionMenu, versionSub,
} from '../src/pages/versions/versionsModel.js';

// ---------- 纯逻辑 ----------

test('逐行对比：改一行、加一行、去掉一行，hunks 同契约 V-04', () => {
  const a = 'a\nb\nc\nd';
  const b = 'a\nB\nc\nd\ne';
  const d = diffLines(a, b);
  assert.deepEqual(d.left.map((x) => x.k), ['', 'd', '', '']);
  assert.deepEqual(d.right.map((x) => x.k), ['', 'a', '', '', 'a']);
  assert.deepEqual(d.hunks, [
    { left_start: 2, left_lines: 1, right_start: 2, right_lines: 1 },
    { left_start: 5, left_lines: 0, right_start: 5, right_lines: 1 },
  ]);
  assert.equal(diffLines('x', 'x').hunks.length, 0);
  // 左边没有这个文件：右边全是新增
  assert.deepEqual(diffLines('', 'p\nq').right.map((x) => x.k), ['a', 'a']);
  assert.equal(editBlocks('a\nb', 'a\nb'), 0);
  assert.equal(editBlocks('a\nb\nc', 'a\nX\nc\nY'), 2);
});

test('来源标记：契约来源 → 界面叫法与设计稿配色，回退用产品补的配色', () => {
  assert.equal(srcLabel({ source: 'autobuild' }), '自主构建');
  assert.equal(srcLabel({ source: 'plugin', source_label: '插件' }), '插件');
  assert.equal(srcClass('autobuild'), 'l4');
  assert.equal(srcClass('revert'), 'r');
  assert.equal(srcClass('mcp'), 'm');
});

test('条目说法与路径：对象「陈列量标准」 ↔ ontology/object/陈列量标准.md', () => {
  const ref = { kind: 'object', id: 'object:display_qty_std', key: 'display_qty_std', name: '陈列量标准' };
  assert.equal(entryPath(ref), 'ontology/object/陈列量标准.md');
  assert.equal(pathLabel('ontology/object/陈列量标准.md'), '对象「陈列量标准」');
  assert.equal(pathLabel('scripts/build_replenish_order.py'), '脚本「build_replenish_order.py」');
  assert.equal(targetLabel({ type: 'trace.scenario', key: 's-03', title: '场景 3' }), '场景「场景 3」');
  assert.equal(entryPath({ type: 'trace.scenario', key: 's-03' }), null);
});

const LIST = mainHistory().map((v, i, all) => ({ ...v, current: i === all.length - 1 }));

test('落地：查看改动按版本号选中，对不上运行和分段时改按运行 + 分段找', () => {
  const at = (q) => LIST.find((v) => v.commit === pickLanding(LIST, q).commit)?.version_label;
  assert.equal(at({ v: 'v0.15', run: 'run-3', slice: 's3' }), 'v0.15');
  assert.equal(pickLanding(LIST, { v: 'v0.15', run: 'run-3', slice: 's3' }).note, '');
  // 版本号对不上（比如运行中还没生成），按运行 + 分段找最新的那一版
  assert.equal(at({ v: '', run: 'run-3', slice: 's4' }), 'v0.17');
  assert.equal(at({ v: 'v0.99', run: 'run-3', slice: 's2' }), 'v0.14');
  assert.equal(at({ v: 'v0.14', run: 'run-3', slice: 's5' }), 'v0.19');
  // 运行没做的那一段（报告里写的是运行前版本 v0.12，它不带运行信息）：就选 v0.12
  assert.equal(at({ v: 'v0.12', run: 'run-3', slice: 's5' }), 'v0.12');
  // 版本号找到了但属于别的分段：还有这一段的版本就改选它，没有就仍选中并说明
  const q = pickLanding(LIST.filter((v) => v.n <= 16), { v: 'v0.14', run: 'run-3', slice: 's5' });
  assert.equal(LIST.find((v) => v.commit === q.commit).version_label, 'v0.14');
  assert.match(q.note, /不是这次运行生成的/);
  // 版本没带运行信息（P1 后端）：只认版本号，不提示
  const bare = LIST.map((v) => ({ ...v, run: null }));
  assert.deepEqual(pickLanding(bare, { v: 'v0.15', run: 'run-3', slice: 's3' }), { commit: '8d3f6a10', note: '' });
  // 都找不到：当前版本，并说明
  assert.match(pickLanding(LIST, { v: 'v0.99' }).note, /没有找到版本 v0.99/);
  assert.equal(at({}), 'v0.19');
});

test('落地：刷新后对比按提交找；找不到时说明', () => {
  const p = pickLanding(LIST, { path: 'ontology/object/陈列量标准.md', from: 'c0ffee13', to: 'WORKTREE' });
  assert.equal(LIST.find((v) => v.commit === p.commit).version_label, 'v0.13');
  assert.equal(pickLanding(LIST, { path: 'x', from: 'c0ffee' }).commit, 'c0ffee13');
  const miss = pickLanding(LIST, { path: 'x', from: 'deadbeef' }).note;
  assert.match(miss, /没有找到这个版本/);
  assert.ok(!miss.includes('deadbeef'), '不显示提交哈希');
});

test('选中一个版本看它改了什么：上一版 → 这一版（当前版本右侧是可编辑的当前文件），也可改看这一版到现在', () => {
  const head = LIST[LIST.length - 1];
  const mid = LIST[14];
  assert.equal(previousOf(mid, LIST).n, mid.n - 1);
  assert.equal(previousOf(LIST[0], LIST), null);
  assert.deepEqual(selfRange(head, LIST), { from: previousOf(head, LIST).commit, to: 'WORKTREE' });
  assert.deepEqual(selfRange(mid, LIST), { from: previousOf(mid, LIST).commit, to: mid.commit });
  const choices = rangeChoices(mid, LIST, selfRange(mid, LIST));
  assert.deepEqual(choices.map((c) => [c.value, c.label]), [
    ['self', `迭代 ${mid.n - 1} → 迭代 ${mid.n}`], ['now', `迭代 ${mid.n} → 当前`],
  ]);
  assert.equal(rangeValue(choices, { from: mid.commit, to: 'WORKTREE' }), 'now');
  // 当前版本只有「上一版 → 当前」；第一版没有可比的上一版
  assert.deepEqual(rangeChoices(head, LIST, selfRange(head, LIST)).map((c) => c.label), [`迭代 ${head.n - 1} → 当前`]);
  assert.deepEqual(rangeChoices(LIST[0], LIST, selfRange(LIST[0], LIST)).map((c) => c.value), LIST[0].current ? [] : ['now']);
  // 跳转带来的其他范围原样列在最前
  const other = rangeChoices(mid, LIST, { from: LIST[3].commit, to: 'WORKTREE' });
  assert.equal(other[0].value, 'other');
  assert.equal(other[0].label, `迭代 ${LIST[3].n} → 当前`);
});

test('对比左侧：选中当前版本时和上一个版本比；对象下拉取这个版本改过的条目', () => {
  const head = LIST[LIST.length - 1];
  assert.equal(compareFromOf(head, LIST).version_label, 'v0.18');
  assert.equal(compareFromOf(LIST[14], LIST).version_label, 'v0.15');
  const opts = compareOptions(LIST[14].changes, null);
  assert.deepEqual(opts.map((o) => o.label), ['对象「陈列量标准」', '操作「陈列量核定」', '规则「R-12 区域配额≤150箱」']);
  assert.equal(defaultCompare(opts, null), 'ontology/object/陈列量标准.md');
  assert.equal(defaultCompare(opts, 'ontology/object/门店.md'), 'ontology/object/门店.md');
  // 跳转带来的路径排在最前
  assert.equal(compareOptions(LIST[14].changes, 'ontology/object/门店.md')[0].label, '对象「门店」');
  assert.equal(versionSub(LIST[14]), '自主构建运行 #3 · 陈列量核定');
});

test('回退说明（develop 原文）：确认框按业务文件计数，挡住时各原因用「；」连起来', () => {
  assert.equal(undoneText(['v0.12', 'v0.13']), 'v0.12、v0.13');
  assert.equal(undoneText(['v0.15', 'v0.16', 'v0.17', 'v0.18', 'v0.19']), 'v0.15～v0.19');
  assert.equal(
    revertConfirmText({ added: ['a'], modified: ['b', 'c'], deleted: [], total: 3 }),
    '将新增 1、修改 2、删除 0 个业务文件；项目设置和运行状态不会回退。',
  );
  assert.equal(revertBlockedText({ ready: false, blockers: [{ message: '甲' }, { message: '乙' }] }), '甲；乙');
  assert.equal(revertBlockedText({ ready: false, blockers: [] }), '当前项目暂不能回退');
});

test('⋯ 菜单放查看文件、标记、回退（当前版本不能回退）、写入范围只读、顶栏版本同步', () => {
  const menu = versionMenu({ current: true });
  assert.deepEqual(menu.map((x) => (x === '-' ? x : x.key)), ['files', 'tag', '-', 'revert']);
  assert.equal(menu.find((x) => x.key === 'revert').disabled, true);
  assert.equal(versionMenu({ current: false }).find((x) => x.key === 'revert').disabled, false);
  assert.equal(versionMenu({ current: false, can_revert: false }).find((x) => x.key === 'revert').disabled, true);
  // develop !647：没有写权限（can_tag=false）时不给标记入口
  assert.deepEqual(versionMenu({ current: false, can_tag: false }).map((x) => (x === '-' ? x : x.key)), ['files', '-', 'revert']);
  const lock = { state: 'pending_confirm', entries: [{ kind: 'object', id: 'object:display_qty_std', key: 'display_qty_std', name: '陈列量标准' }], artifacts: [], paths: ['ext/trace/scenarios/**'] };
  assert.equal(pathLocked(lock, 'ontology/object/陈列量标准.md'), true);
  assert.equal(pathLocked(lock, 'ext/trace/scenarios/s-01.yaml'), true);
  assert.equal(pathLocked(lock, 'ontology/object/门店.md'), false);
  assert.equal(pathLocked({ ...lock, state: 'none' }, 'ontology/object/陈列量标准.md'), false);
  const meta = { name: 'x', version_label: 'v0.12', head: 'a71e03c9' };
  assert.deepEqual(syncMeta(meta, { current_version_label: 'v0.19', head: 'b2c8f5e0' }), { name: 'x', version_label: 'v0.19', head: 'b2c8f5e0' });
  assert.equal(syncMeta(meta, { current_version_label: 'v0.12', head: 'a71e03c9' }), meta);
});

// ---------- mock ----------

const DISPLAY = [
  '# 陈列量标准', '', '按商品品类和到货周期确定的陈列量上下限。', '', '## 属性', '| 属性 | 英文名 | 类型 |', '| --- | --- | --- |',
  '| 商品品类 | category_code | 文本 |', '| 到货周期 | delivery_cycle | 枚举 |', '| 陈列上限 | display_max | 数值 · 箱 |', '| 生效日期 | effective_from | 日期 |',
  '', '# 关联对象', '[[商品品类]]、[[到货周期]]', '', '## 数据来源', 'erp.replenish_std',
].join('\n');

function mockServer({ sc = {}, lock = null, stage = null } = {}) {
  const routes = [];
  const events = [];
  const buffers = new Map();
  const cards = F.projectCards();
  const scope = { lock };
  const files = new Map([['ontology/object/陈列量标准.md', { content: DISPLAY, revision: 'r1' }]]);
  const generic = (path) => {
    const name = path.split('/').pop().replace(/\.md$/, '');
    return `# ${name}\n\n${name}的说明。\n\n## 属性\n| 属性 | 英文名 | 类型 |\n| --- | --- | --- |\n| 甲 | a | 文本 |\n| 乙 | b | 文本 |`;
  };
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {},
    sc,
    uid: (() => { let i = 0; return () => (i += 1).toString(16); })(),
    F,
    cardOf: (id) => cards.find((c) => c.id === id),
    scopeOf: () => scope.lock || { state: 'none', entries: [], artifacts: [], paths: [] },
    stageOf: () => stage,
    projKey: (pid) => `proj:${pid}`,
    convKey: (pid, cid) => `${pid}:${cid}`,
    convsOf: () => [{ id: 'c1' }],
    hub: { history: (key) => buffers.get(key) || [] },
    emitProj: (pid, type, data) => {
      const e = { type, data, ts: new Date().toISOString() };
      events.push(e);
      buffers.set(`proj:${pid}`, [...(buffers.get(`proj:${pid}`) || []), e]);
    },
    readFile: async (pid, path) => {
      if (pid !== F.MAIN_PID || !/^ontology\//.test(path)) return null;
      if (!files.has(path)) files.set(path, { content: generic(path), revision: 'r1' });
      return { ...files.get(path) };
    },
  };
  registerVersions(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user: { username: 'zhangsan' }, init: {} });
      if (res !== undefined) return { status: res.status, body: res.status === 204 ? null : await res.json().catch(() => null) };
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  return { call, events, scope, cards, files, ctx };
}

const P = `/api/v1/projects/${F.MAIN_PID}`;
const PATH = encodeURIComponent('ontology/object/陈列量标准.md');

test('V-01：19 个版本从新到旧，最新的是当前；项目卡的主线版本跟上', async () => {
  const { call, events, cards } = mockServer();
  const r = await call('GET', `${P}/versions`);
  assert.equal(r.status, 200);
  assert.equal(r.body.total, 19);
  assert.equal(r.body.items[0].version_label, 'v0.19');
  assert.equal(r.body.items[0].current, true);
  assert.equal(r.body.current_version_label, 'v0.19');
  assert.equal(r.body.items.find((v) => v.version_label === 'v0.7').tags[0].label, '校验通过的里程碑');
  assert.equal(r.body.items.find((v) => v.version_label === 'v0.11').release, '0.11.0');
  assert.deepEqual(r.body.items.find((v) => v.version_label === 'v0.15').run, { run_id: 'run-3', run_no: 3, slice_id: 's3', slice_name: '陈列量核定' });
  assert.equal(cards[0].version_label, 'v0.19');
  assert.ok(events.some((e) => e.type === 'project.updated' && e.data.meta.version_label === 'v0.19'));
  const studio = await call('GET', `${P}/versions?source=studio,revert`);
  assert.deepEqual(studio.body.items.map((v) => v.version_label), ['v0.18', 'v0.10', 'v0.5']);
  const page = await call('GET', `${P}/versions?limit=5`);
  assert.equal(page.body.items.length, 5);
  assert.equal(page.body.next_cursor, '5');
});

test('V-01：自主构建运行中（stage:l4）只有到 v0.16 的版本；别处的修改出现在列表里，版本号冲突时顺延', async () => {
  const running = mockServer({ stage: 'l4' });
  assert.equal((await running.call('GET', `${P}/versions`)).body.items[0].version_label, 'v0.16');
  const { call, ctx } = mockServer();
  await call('GET', `${P}/versions`);
  ctx.emitProj(F.MAIN_PID, 'workspace.changed', { commit: 'c0ffee99', version_label: 'v0.13', source: 'assistant', changeset_id: 'cs-31', by: 'zhangsan', entries: [F.REFS.op], artifacts: [], paths: [] });
  const r = await call('GET', `${P}/versions`);
  assert.equal(r.body.items[0].version_label, 'v0.20');
  assert.equal(r.body.items[0].source, 'assistant');
  assert.match(r.body.items[0].summary, /^助手修改：/);
});

test('V-02：「改了什么」带复核状态与运行分段', async () => {
  const { call } = mockServer();
  const r = await call('GET', `${P}/versions/8d3f6a10`);
  assert.equal(r.body.version_label, 'v0.15');
  assert.equal(r.body.run.slice_id, 's3');
  assert.deepEqual(r.body.changes.map((c) => [c.target.name || c.target.title, c.change, c.review]), [
    ['陈列量标准', 'mod', 'needs_review'], ['陈列量核定', 'mod', 'needs_review'], ['R-12 区域配额≤150箱', 'add', 'accepted'], ['场景 3', 'mod', null],
  ]);
  assert.ok(r.body.changes.every((c) => !('undo' in c)));
  assert.equal((await call('GET', `${P}/versions/zz`)).body.code, 'invalid_commit');
});

test('V-04 / F-06：历史文件按改动往回推，右侧是当前文件并带 revision', async () => {
  const { call } = mockServer();
  const d = await call('GET', `${P}/versions/diff?from=8d3f6a10&to=WORKTREE&path=${PATH}`);
  assert.equal(d.status, 200);
  assert.equal(d.body.from.version_label, 'v0.15');
  assert.deepEqual(d.body.to, { commit: null, version_label: null });
  assert.equal(d.body.right, DISPLAY);
  assert.equal(d.body.right_revision, 'r1');
  assert.ok(!d.body.left.includes('到货周期'));
  assert.ok(d.body.left.includes('按商品品类确定的陈列量上下限。'));
  assert.ok(d.body.left.includes('生效日期'));
  assert.ok(d.body.hunks.length >= 2);
  // v0.15 之前还没有「生效日期」；按 entry 也能取
  const old = await call('GET', `${P}/versions/diff?from=c0ffee13&to=WORKTREE&entry=object:display_qty_std`);
  assert.ok(!old.body.left.includes('生效日期'));
  // 两个历史版本之间对比：右侧只读
  const between = await call('GET', `${P}/versions/diff?from=c0ffee13&to=9e4d71b3&path=${PATH}`);
  assert.equal(between.body.right_revision, null);
  assert.equal(between.body.to.version_label, 'v0.18');
  // 临期损耗标准 v0.16 才新增
  const p2 = encodeURIComponent('ontology/object/临期损耗标准.md');
  assert.equal((await call('GET', `${P}/files/at?commit=8d3f6a10&path=${p2}`)).status, 404);
  const at = await call('GET', `${P}/files/at?commit=1c5a9e47&path=${p2}`);
  assert.equal(at.status, 200);
  assert.equal(at.body.version_label, 'v0.16');
  assert.equal((await call('GET', `${P}/files/at?commit=HEAD~1&path=${PATH}`)).body.version_label, 'v0.18');
  assert.equal((await call('GET', `${P}/files/at?commit=8d3f6a10&path=../x`)).status, 403);
});

test('撤回一处改动的写法：去行、换行、去链接、去掉 / 补回一节、新增前不存在', () => {
  const c = { change: 'mod', undo: { row: '到货周期', lines: [['按商品品类和到货周期确定的陈列量上下限。', '按商品品类确定的陈列量上下限。']], link: '到货周期' } };
  const before = undoChange(DISPLAY, c);
  assert.ok(!before.includes('| 到货周期 |'));
  assert.ok(before.includes('[[商品品类]]\n'));
  assert.equal(undoChange('x', { change: 'add', undo: { add: true } }), null);
  const cold = ['', '## 冷链', '乳制品到货时核对冷链温度记录，超过 4℃ 拒收。'];
  const withCold = undoChange('# 门店特殊需求', { change: 'mod', undo: { append: cold } });
  assert.ok(withCold.includes('## 冷链'));
  assert.equal(undoChange(withCold, { change: 'mod', undo: { cut: cold } }), '# 门店特殊需求');
  // 没有 undo 的修改：去掉属性表最后一行
  assert.ok(!undoChange('# a\n| 属性 | 英文名 |\n| --- | --- |\n| 甲 | a |\n| 乙 | b |', { change: 'mod' }).includes('| 乙 |'));
});

test('V-05 / V-06：回退预览列出恢复与删除；有未确认修改时 423，自主构建中 409；回退后当前内容变回去', async () => {
  const locked = mockServer({ lock: { state: 'pending_confirm', entries: [], artifacts: [], paths: [] } });
  const r423 = await locked.call('POST', `${P}/versions/revert`, { commit: '8d3f6a10' });
  assert.equal(r423.status, 423);
  assert.equal(r423.body.code, 'write_scope_locked');
  const running = mockServer({ stage: 'l4' });
  assert.equal((await running.call('POST', `${P}/versions/revert`, { commit: '8d3f6a10' })).body.code, 'autobuild_running');

  const { call, events } = mockServer();
  const pre = await call('GET', `${P}/versions/revert-preview?commit=8d3f6a10`);
  assert.equal(pre.status, 200);
  assert.equal(pre.body.next_version_label, 'v0.20');
  assert.deepEqual(pre.body.undone_versions, ['v0.16', 'v0.17', 'v0.18', 'v0.19']);
  const byPath = Object.fromEntries(pre.body.files.map((f) => [f.path, f.change]));
  assert.equal(byPath['ontology/object/陈列量标准.md'], 'restore');
  assert.equal(byPath['ontology/object/临期损耗标准.md'], 'delete');
  assert.equal(byPath['ext/trace/scenarios/s-07.yaml'], 'delete');
  assert.deepEqual(pre.body.packs_lock_changes, []);
  // 回到 v0.10 会撤回 v0.11 的能力升级
  const pre10 = await call('GET', `${P}/versions/revert-preview?commit=5e81a4c3`);
  assert.deepEqual(pre10.body.packs_lock_changes, [{ pack: 'industry-retail', from: '1.0.3', to: '1.0.2' }]);
  assert.equal((await call('GET', `${P}/versions/revert-preview?commit=b2c8f5e0`)).body.code, 'nothing_to_revert');

  const r = await call('POST', `${P}/versions/revert`, { commit: '8d3f6a10' });
  assert.equal(r.status, 200);
  assert.equal(r.body.version_label, 'v0.20');
  assert.deepEqual(r.body.reverted_to, { commit: '8d3f6a10', version_label: 'v0.15' });
  const ws = events.filter((e) => e.type === 'workspace.changed').pop();
  assert.equal(ws.data.source, 'revert');
  assert.ok(ws.data.paths.includes('ontology/object/陈列量标准.md'));
  const list = await call('GET', `${P}/versions`);
  assert.equal(list.body.items[0].version_label, 'v0.20');
  assert.equal(list.body.items[0].source, 'revert');
  assert.match(list.body.items[0].summary, /回退到 v0.15：撤回 v0.16～v0.19 的改动/);
  const now = await call('GET', `${P}/versions/diff?from=b2c8f5e0&to=WORKTREE&path=${PATH}`);
  assert.ok(!now.body.right.includes('| 到货周期 |'));
  assert.equal(now.body.right_revision, 'r1');
  // 回退后再看 v0.19：内容不变
  assert.ok(now.body.left.includes('| 到货周期 |'));
});

test('V-07 / V-08：标记版本与删除标记（发布标签不能删）', async () => {
  const { call } = mockServer();
  assert.equal((await call('POST', `${P}/versions/tags`, { commit: 'b2c8f5e0', label: ' ' })).body.code, 'invalid_argument');
  assert.equal((await call('POST', `${P}/versions/tags`, { commit: 'nope', label: '月底盘点前' })).body.code, 'invalid_commit');
  const t = await call('POST', `${P}/versions/tags`, { commit: 'b2c8f5e0', label: '月底盘点前' });
  assert.equal(t.status, 201);
  assert.match(t.body.name, /^mark-\d+-[0-9a-f]{4}$/);
  assert.equal(t.body.version_label, 'v0.19');
  let list = await call('GET', `${P}/versions`);
  assert.deepEqual(list.body.items[0].tags.map((x) => x.label), ['月底盘点前']);
  assert.equal((await call('DELETE', `${P}/versions/tags/release%2F0.11.0`)).body.code, 'tag_not_found');
  assert.equal((await call('DELETE', `${P}/versions/tags/${t.body.name}`)).status, 204);
  assert.equal((await call('DELETE', `${P}/versions/tags/mark-1761811680-3a7c`)).status, 204);
  list = await call('GET', `${P}/versions`);
  assert.equal(list.body.items[0].tags.length, 0);
  assert.equal(list.body.items.find((v) => v.version_label === 'v0.7').tags.length, 0);
  assert.equal((await call('DELETE', `${P}/versions/tags/${t.body.name}`)).body.code, 'tag_not_found');
});

test('V-03：某版本的文件（v0.15 还没有临期那一段的条目）与 zip 下载；其他项目用通用历史', async () => {
  const { call } = mockServer();
  const files = await call('GET', `${P}/versions/8d3f6a10/files`);
  const paths = files.body.items.map((f) => f.path);
  assert.ok(paths.includes('ontology/object/陈列量标准.md'));
  assert.ok(!paths.includes('ontology/object/临期损耗标准.md'));
  assert.ok(paths.every((p) => p.startsWith('ontology/')));
  assert.equal((await call('GET', `${P}/versions/8d3f6a10/files?download=1`)).status, 200);
  const other = mockServer();
  const lib = await other.call('GET', '/api/v1/projects/library-desk-1e8f6a07/versions');
  assert.equal(lib.body.items[0].version_label, 'v1.3');
  assert.equal(lib.body.total, 3);
});

