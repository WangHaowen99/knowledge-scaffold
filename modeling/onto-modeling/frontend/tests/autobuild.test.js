import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ENDED, createRunStore, history, reportOf, runOf,
} from '../src/api/mock/run3.js';
import {
  fmtDur, fmtEst, fmtTok, matrixTone, miniMetrics, money, reportMetrics, reportSub, runFlags, sliceSegments, stripMetrics, stripTitle,
} from '../src/pages/workbench/autobuildModel.js';
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

test('时长、token、费用的写法同设计稿', () => {
  assert.equal(fmtDur(38 * 60000), '38 分');
  assert.equal(fmtDur(112 * 60000), '1 小时 52 分');
  assert.equal(fmtDur(120 * 60000), '2 小时');
  assert.equal(fmtEst(84 * 60000), '约 84 分');
  assert.equal(fmtTok(2100000), '2.1M');
  assert.equal(fmtTok(6000000), '6M');
  assert.equal(fmtTok(820000), '820k');
  assert.equal(money({ amount: 31, currency: 'CNY' }), '¥31');
  assert.equal(money({ amount: 2.6, currency: 'USD' }), '$3');
  assert.equal(money(null), '—');
});

test('运行条：运行中显示已用时 / 预计，结束后显示用时 / 预算', () => {
  const run = runOf('running');
  assert.deepEqual(stripMetrics(run).map((m) => m.slice(0, 3)), [['已用时', '38 分', '约 84 分'], ['token', '2.1M', '6M'], ['费用', '¥31', '¥150']]);
  assert.deepEqual(stripMetrics(run).map((m) => m[3]), [45, 35, 21]);
  const done = runOf('completed');
  assert.deepEqual(stripMetrics(done)[0], ['用时', '1 小时 52 分', '2 小时', 93]);
  assert.equal(stripTitle(run).title, '自主构建中 · 运行 #3');
  assert.equal(stripTitle(run).sub, '当前：临期与损耗检查 › 提取规则');
  assert.equal(stripTitle({ ...done, started_at: at(15, 10), ended_at: at(17, 2) }).sub, '15:10 – 17:02 · 全部切片完成');
  assert.equal(stripTitle({ ...run, state: 'paused' }).title, '已暂停 · 运行 #3');
  assert.match(stripTitle({ ...run, state: 'stopping' }).sub, /「临期与损耗检查」做完后停下/);
});

test('流水线结果列的颜色', () => {
  const row = (cells, result) => ({ cells: Object.fromEntries(['object', 'operation', 'rule', 'check', 'scenario'].map((k, i) => [k, cells[i]])), result });
  assert.equal(matrixTone(row(['auto', 'auto', 'running', 'queued', 'queued'], '进行中')), 'running');
  assert.equal(matrixTone(row(['queued', 'queued', 'queued', 'queued', 'queued'], '排队')), 'gray');
  assert.equal(matrixTone(row(['confirmed', 'confirmed', 'skipped', 'confirmed', 'confirmed'], '运行前已完成')), 'gray');
  assert.equal(matrixTone(row(['auto', 'auto', 'auto', 'auto', 'auto'], '部分完成 · 挂起 2 项')), 'amber');
  assert.equal(matrixTone(row(['confirmed', 'auto', 'auto', 'auto', 'auto'], '已刷新过期 3 项')), 'indigo');
});

test('按切片的进展块：连续步骤合成一段，自动决定、挂起按原顺序穿插', () => {
  const s = (id) => ({ id, text: id, meta: '', state: 'done' });
  const segs = sliceSegments([s('a'), s('b'), { type: 'auto_decision', card: { card_id: 'd1' } }, s('c'), { type: 'suspension', card: { card_id: 'p1' } }, s('e')]);
  assert.deepEqual(segs.map((x) => x.kind), ['steps', 'auto_decision', 'steps', 'suspension', 'steps']);
  assert.deepEqual(segs[0].steps.map((x) => x.id), ['a', 'b']);
  assert.deepEqual(sliceSegments(null), []);
});

test('运行详情六格与报告卡八格', () => {
  assert.deepEqual(miniMetrics(runOf('running')).map((x) => x[0]), ['38 分', '2.1M', '¥31', '0 / 3', '14 / 15', '1 / 2']);
  assert.equal(miniMetrics(runOf('running'))[0][1], '已用时 · 预计约 84 分');
  const r = reportOf('completed');
  const m = reportMetrics(r);
  assert.equal(m.length, 8);
  assert.deepEqual(m[1], ['切片', '5 / 5', '临期与损耗检查部分完成']);
  assert.deepEqual(m[5], ['自动决策', '17 项', '高置信 13 · 需看 4']);
  assert.match(reportSub({ ...r, started_at: at(15, 10), ended_at: at(17, 2) }), /^15:10 – 17:02 · 中途服务重启 1 次/);
});

test('运行中的输入框：补充说明不打断运行；正在做的这一片只读到自动合入为止', () => {
  const st = composerState({ sessionState: 'running', autobuildRunning: true });
  assert.equal(st.placeholder, '补充说明，不打断运行；下一步开始前读取');
  assert.equal(st.delivery, 'deferred');
  const info = readonlyInfo({ state: 'autobuild_slice', slice: { id: 's4', name: '临期与损耗检查' }, entries: [], artifacts: [], paths: [] });
  assert.match(info.text, /自主构建正在做「临期与损耗检查」，到这一片自动合入为止只读/);
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

test('mock 运行对话：开始卡、按切片的进展块、结束后的报告卡', () => {
  const types = (h) => h.filter((e) => e.type === 'card').map((e) => e.data.card_type);
  assert.deepEqual(types(history('running')), ['autobuild_start', 'slice_progress', 'slice_progress', 'slice_progress', 'slice_progress']);
  assert.deepEqual(types(history('completed')).slice(-2), ['slice_progress', 'run_report']);
  const sug = history('running').at(-1);
  assert.equal(sug.type, 'suggestions.updated');
  assert.equal(sug.data.mode, 'autobuild_controls');
  assert.deepEqual(sug.data.secondary.map((x) => x.label), ['运行详情', '挂起的问题 2']);
});
