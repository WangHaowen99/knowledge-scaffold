import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ENDED, createRunStore, history, reportOf, runOf,
} from '../src/api/mock/run3.js';
import {
  canInterrupt, fmtDur, fmtEst, fmtTok, matrixTone, miniMetrics, reportMetrics, reportSub, runFlags, sliceSegments, stripMetrics, stripShown,
  stripTitle,
} from '../src/pages/workbench/autobuildModel.js';
import { previewScope } from '../src/pages/workbench/autobuildModel.js';
import { composerState, readonlyInfo } from '../src/state/readonly.js';

const at = (h, m) => new Date(2026, 10, 1, h, m).toISOString();

test('运行状态分类：运行中、去往安全点、暂停、已结束', () => {
  assert.deepEqual(runFlags({ state: 'running' }), { state: 'running', running: true, settling: false, paused: false, ended: false });
  assert.equal(runFlags({ state: 'stopping' }).settling, true);
  assert.equal(runFlags({ state: 'stopping' }).running, true);
  assert.equal(runFlags({ state: 'paused' }).paused, true);
  ['completed', 'stopped', 'converted', 'budget_exhausted', 'failed'].forEach((s) => assert.equal(runFlags({ state: s }).ended, true));
  assert.equal(runFlags(null).state, null);
});

test('时长、token 的写法同设计稿', () => {
  assert.equal(fmtDur(38 * 60000), '38 分');
  assert.equal(fmtDur(112 * 60000), '1 小时 52 分');
  assert.equal(fmtDur(120 * 60000), '2 小时');
  assert.equal(fmtEst(84 * 60000), '约 84 分');
  assert.equal(fmtTok(2100000), '2.1M');
  assert.equal(fmtTok(6000000), '6M');
  assert.equal(fmtTok(820000), '820k');
});

test('运行条：运行中显示已用时 / 预计，结束后只给用时；token 只给用量，没有上限', () => {
  const run = runOf('running');
  assert.deepEqual(stripMetrics(run), [['已用时', '38 分', '约 84 分', 45], ['token', '2.1M', null, null]]);
  // 还没有预计（刚开始）时不画进度条
  assert.deepEqual(stripMetrics({ ...run, est_total_ms: null })[0], ['已用时', '38 分', null, null]);
  const done = runOf('completed');
  assert.deepEqual(stripMetrics(done), [['用时', '1 小时 52 分', null, null], ['token', '5.1M', null, null]]);
  assert.equal(stripTitle(run).title, '自主构建中 · 运行 #3');
  assert.equal(stripTitle(run).sub, '当前：临期与损耗检查 › 提取规则');
  assert.equal(stripTitle({ ...done, started_at: at(15, 10), ended_at: at(17, 2) }).sub, '15:10 – 17:02 · 全部完成');
  assert.equal(stripTitle({ ...run, state: 'paused' }).title, '已暂停 · 运行 #3');
  // 暂停在两步之间（真实运行：当前步骤为空）不留「›」
  assert.equal(stripTitle({ ...run, state: 'paused', current: { ...run.current, step_label: '' } }).sub, '停在：临期与损耗检查');
  assert.match(stripTitle({ ...run, state: 'stopping' }).sub, /「临期与损耗检查」做完后停下/);
  // 配额用完而暂停：说清怎么继续
  assert.equal(stripTitle({ ...run, state: 'paused', pause_reason: 'quota' }).sub, '配额用完，补足后点续跑');
  // 老运行记录里的「预算用完」照常显示
  assert.equal(stripTitle({ ...done, state: 'budget_exhausted' }).title, '运行 #3 预算用完');
});

test('运行条什么时候收起：没结束一直显示；结束后只在运行对话里留着，点过「确认」收起', () => {
  const running = runOf('running');
  const done = runOf('completed');
  assert.equal(stripShown(running, false), true);
  assert.equal(stripShown(running, true), true);
  assert.equal(stripShown(done, false), false);
  assert.equal(stripShown(done, true), true);
  assert.equal(stripShown({ ...done, acknowledged: true }, true), false);
  // 没结束的运行带着 acknowledged 也不收（只对结束态有意义）
  assert.equal(stripShown({ ...running, acknowledged: true }, true), true);
  assert.equal(stripShown(null, true), false);
});

test('流水线结果列的颜色', () => {
  const row = (cells, result) => ({ cells: Object.fromEntries(['object', 'operation', 'rule', 'check', 'scenario'].map((k, i) => [k, cells[i]])), result });
  assert.equal(matrixTone(row(['auto', 'auto', 'running', 'queued', 'queued'], '进行中')), 'running');
  assert.equal(matrixTone(row(['queued', 'queued', 'queued', 'queued', 'queued'], '排队')), 'gray');
  assert.equal(matrixTone(row(['confirmed', 'confirmed', 'skipped', 'confirmed', 'confirmed'], '运行前已完成')), 'gray');
  assert.equal(matrixTone(row(['auto', 'auto', 'auto', 'auto', 'auto'], '部分完成 · 挂起 2 项')), 'amber');
  assert.equal(matrixTone(row(['confirmed', 'auto', 'auto', 'auto', 'auto'], '已刷新过期 3 项')), 'indigo');
  // 一个节点一行（没有步骤格子）：按单元状态与结果
  const node = (state, result) => ({ slice_id: 'core.object', state, cells: {}, result });
  assert.equal(matrixTone(node('running', '进行中')), 'running');
  assert.equal(matrixTone(node('queued', '排队')), 'gray');
  assert.equal(matrixTone(node('done', '完成 · 挂起 1 项')), 'indigo');
  assert.equal(matrixTone(node('partial', '部分完成')), 'amber');
  assert.equal(matrixTone(node('done', '已回退到运行前 · v0.12')), 'gray');
});

test('按段的进展块：连续步骤合成一段，自动决定、挂起按原顺序穿插', () => {
  const s = (id) => ({ id, text: id, meta: '', state: 'done' });
  const segs = sliceSegments([s('a'), s('b'), { type: 'auto_decision', card: { card_id: 'd1' } }, s('c'), { type: 'suspension', card: { card_id: 'p1' } }, s('e')]);
  assert.deepEqual(segs.map((x) => x.kind), ['steps', 'auto_decision', 'steps', 'suspension', 'steps']);
  assert.deepEqual(segs[0].steps.map((x) => x.id), ['a', 'b']);
  assert.deepEqual(sliceSegments(null), []);
});

test('运行详情六格与报告卡八格', () => {
  assert.deepEqual(miniMetrics(runOf('running')).map((x) => x[0]), ['38 分', '2.1M', '0 / 3', '14 / 15', '1 / 2']);
  assert.equal(miniMetrics(runOf('running'))[0][1], '已用时 · 预计约 84 分');
  assert.deepEqual(miniMetrics(runOf('completed')).slice(0, 2).map((x) => x[1]), ['用时', 'token']);
  const r = reportOf('completed');
  const m = reportMetrics(r);
  assert.equal(m.length, 8);
  assert.deepEqual(m[0], ['用时', '1 小时 52 分', '']);
  assert.deepEqual(m[1], ['步骤', '5 / 5', '临期与损耗检查部分完成']);
  assert.deepEqual(m[5], ['自动决策', '17 项', '高置信 13 · 需看 4']);
  assert.ok(!m.some(([label]) => label === '费用')); // 工作区不显示金额
  // F8 起建模里没有场景这一步：没有数据时不占格
  const none = reportMetrics({ ...r, metrics: { ...r.metrics, scenarios: { passed: 0, total: 0, suspended_failures: 0 } } });
  assert.ok(!none.some(([label]) => label === '场景推演'));
  assert.ok(!miniMetrics({ ...runOf('running'), scenarios: { passed: 0, total: 0 } }).some(([, label]) => label === '场景推演通过'));
  assert.match(reportSub({ ...r, started_at: at(15, 10), ended_at: at(17, 2) }), /^15:10 – 17:02 · 服务重启 1 次，已自动续跑$/);
});

test('运行中的输入框：补充说明不打断运行；正在做的这一段完成前只读', () => {
  const st = composerState({ sessionState: 'running', autobuildRunning: true });
  // 文案由外壳的只读口径定（state/readonly.js），这里只认「补充说明、不打断运行」与送达方式
  assert.match(st.placeholder, /^补充说明，不打断运行/);
  assert.equal(st.delivery, 'deferred');
  const info = readonlyInfo({ state: 'autobuild_slice', slice: { id: 's4', name: '临期与损耗检查' }, entries: [], artifacts: [], paths: [] });
  assert.match(info.text, /^自主构建正在做「临期与损耗检查」/);
  assert.match(info.text, /只读/);
  assert.equal(info.actions[0].kind, 'run');
});

test('mock 运行 #3：运行中停在 15:48，结束后台账 17 项、挂起 3 项', () => {
  const run = createRunStore('running');
  assert.equal(run.run().decisions, 9);
  assert.equal(run.run().suspended, 2);
  assert.equal(run.pendingHigh().length, 6);
  run.state = 'completed';
  assert.equal(run.run().decisions, 17);
  assert.equal(run.pendingHigh().length, 13);
  assert.equal(run.pendingLow().length, 4);
  assert.equal(run.acceptHigh(), 13);
  assert.equal(run.run().needs_review, 4);
  const r = run.revert('s4');
  assert.equal(r.reverted_to_version_label, 'v0.12');
  assert.equal(run.report().slices.find((x) => x.slice_id === 's4').revertible, false);
  assert.ok(ENDED.has('converted'));
});

test('mock 运行对话：开始卡、按段的进展块、结束后的报告卡', () => {
  const types = (h) => h.filter((e) => e.type === 'card').map((e) => e.data.card_type);
  assert.deepEqual(types(history('running')), ['autobuild_start', 'slice_progress', 'slice_progress', 'slice_progress', 'slice_progress']);
  assert.deepEqual(types(history('completed')).slice(-2), ['slice_progress', 'run_report']);
  const sug = history('running').at(-1);
  assert.equal(sug.type, 'suggestions.updated');
  assert.equal(sug.data.mode, 'autobuild_controls');
  assert.deepEqual(sug.data.secondary.map((x) => x.label), ['运行详情', '挂起的问题 2']);
});

test('运行对话在自主构建运行中不给「打断」（S-08 回 409 autobuild_running，用运行条暂停）', () => {
  assert.equal(canInterrupt(true, { running: true }), false);
  assert.equal(canInterrupt(true, { running: false }), true);
  assert.equal(canInterrupt(true, null), true);
  assert.equal(canInterrupt(false, { running: false }), false);
});

test('按节点排：运行条写节点名（老运行的「全部」叫「建模」）；开始前预览的范围列出要做的几步', () => {
  const old = { ...runOf('running'), current: { slice_id: 'all', slice_name: '全部', step_label: '提取操作' } };
  assert.equal(stripTitle(old).sub, '当前：建模 › 提取操作');
  assert.match(stripTitle({ ...old, state: 'stopping' }).sub, /「建模」做完后停下/);
  assert.equal(stripTitle({ ...old, state: 'converting', current: null }).sub, '到下一个安全点转为逐个确认');
  const run = { ...runOf('running'), current: { slice_id: 'core.object', slice_name: '提取对象', step_label: '提取对象' } };
  assert.equal(stripTitle(run).sub, '当前：提取对象');
  assert.match(stripTitle({ ...run, state: 'stopping' }).sub, /「提取对象」做完后停下/);
  const rows = [{ id: 'core.object', name: '提取对象', current: '已确认', included: false },
    { id: 'core.operation', name: '提取操作', current: '有过期', included: true },
    { id: 'core.validate', name: '校验', current: '未开始', included: true }];
  assert.equal(previewScope({ slices: rows }), '提取操作 → 校验');
  assert.equal(previewScope({ slices: [] }), '全部没做完的步骤');
});
