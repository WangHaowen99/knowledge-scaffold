import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { StreamHub } from '../src/api/mock/hub.js';
import { register } from '../src/api/mock/routes-reports.js';
import {
  LINK_STATS, PURPOSE_LABEL, fmtDuration, fmtSince, fmtStamp, infoPairs, linkKind, locateTarget, materialCounts, progressView, repairView,
} from '../src/pages/reports/reportModel.js';

const at = (m, d, h, mi) => new Date(2026, m - 1, d, h, mi).toISOString();

test('时间与耗时的写法', () => {
  assert.equal(fmtStamp(at(10, 31, 21, 52)), '10-31 21:52');
  assert.equal(fmtSince(at(10, 31, 21, 31), at(10, 31, 21, 52)), '21:31');
  assert.equal(fmtSince(at(10, 28, 15, 10), at(10, 30, 18, 20)), '10-28');
  assert.equal(fmtDuration(3200), '3.2 秒');
  assert.equal(fmtDuration(800), '0.8 秒');
  assert.equal(fmtDuration(12000), '12 秒');
  assert.equal(fmtDuration(400000), '6 分 40 秒');
  assert.equal(fmtDuration(120000), '2 分钟');
  assert.equal(fmtDuration(null), '—');
});

const RULE = {
  id: 'rp-rule', type: 'rule', type_label: '规则', at: at(10, 31, 21, 52), scope_text: '切片「超量与审批提示」', mode: 'regression', duration_ms: 3200,
  previous: { at: at(10, 31, 21, 31), summary: '错误 2' }, progress: { handled: 2, total: 5, note: 'n' },
};

test('「这份报告」（范围、模式只在这里出，报告头不再重复）、处理进度', () => {
  assert.deepEqual(infoPairs({ ...RULE, scope_text: '全知识包', mode: 'full' }).slice(1, 3), [['范围', '全知识包'], ['模式', '全量']]);
  assert.deepEqual(infoPairs({ type: 'link', scope_text: '全知识包 · 34 个元素', mode: 'full' }).slice(0, 2), [['检查类型', '链接检查'], ['范围', '全知识包 · 34 个元素']]);
  assert.deepEqual(infoPairs(RULE), [['检查类型', '规则检查'], ['范围', '切片「超量与审批提示」'], ['模式', '回归 · 只查改动'], ['耗时', '3.2 秒'], ['上一份', '21:31 · 错误 2']]);
  assert.equal(infoPairs({ ...RULE, type: 'research', mode: 'research', extras: { total: 7 }, previous: null })[1][1], '资料区 7 份');
  // 服务端的 progress.note 是说明槽位，不再带出来
  assert.deepEqual(progressView(RULE), { text: '已处理 2 / 5', pct: 40 });
  assert.equal(progressView({ type: 'research', progress: { handled: 41, total: 41, note: '' } }).text, '候选已审阅 41 / 41');
  assert.equal(progressView(null).pct, 0);
});

test('修复轮次：最多 2 轮，之后那一根是「现在」，没收敛的交给你裁决', () => {
  const items = [{ status: 'expert' }, { status: 'fixing' }, { status: 'fixed' }];
  const v = repairView({ rounds: [{ n: 1, count: 4 }, { n: 2, count: 2 }, { n: 3, count: 1 }], limit: 2, exhausted: true }, items);
  assert.deepEqual(v.bars.map((b) => [b.label, b.count, b.cur]), [['第 1 轮', 4, false], ['第 2 轮', 2, false], ['现在', 1, true]]);
  assert.equal(v.title, '修复轮次 2 / 2');
  assert.equal(v.note, '还剩 1 项没收敛，交给你裁决');
  assert.equal(v.exhausted, true);
  assert.equal(repairView({ rounds: [{ n: 1, count: 4 }, { n: 2, count: 2 }], limit: 2, exhausted: true }, [{ status: 'fixed' }]).note, '');
  const first = repairView({ rounds: [{ n: 1, count: 3 }], limit: 2, exhausted: false }, items);
  assert.equal(first.title, '修复轮次 1 / 2');
  assert.equal(first.bars[0].cur, true);
  assert.equal(first.exhausted, false);
  assert.equal(first.note, '');
  assert.equal(repairView(null, items), null);
});

test('链接问题分类、定位去哪、资料计数', () => {
  assert.deepEqual(LINK_STATS.map(([k]) => k), ['orphans', 'unused', 'dangling']);
  assert.deepEqual(linkKind({ code: 'LINK-DANGLING' }), ['失效链接', 'red']);
  assert.deepEqual(linkKind({ code: 'X', level: 'warning' }), ['警告', 'amber']);
  assert.deepEqual(locateTarget({ target: { kind: 'object', id: 'object:a', key: 'a', name: 'A' }, page: 'studio' }), { page: 'studio', query: { entry: 'object:a' } });
  assert.deepEqual(locateTarget({ target: { type: 'diagram.flowchart', key: 'overage-flow' }, page: 'studio' }), { page: 'studio-table', query: { entry: 'flowchart:overage-flow' } });
  assert.deepEqual(locateTarget({ target: { type: 'trace.scenario', key: 's-04' } }), { page: 'scenarios', query: { key: 's-04' } });
  assert.equal(locateTarget({ target: null }), null);
  assert.equal(materialCounts({ objects: 6, rules: 14, terms: 21 }), '对象 6 · 规则 14 · 术语 21');
  assert.equal(materialCounts({ fields: 8 }), '字段 8');
});

// ---------- mock 路由（只装 routes-reports.js） ----------
function mockCtx() {
  const routes = [];
  const hub = new StreamHub();
  const events = [];
  let seq = 100;
  const convKey = (pid, cid) => `conv:${pid}:${cid}`;
  register({
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(body == null ? null : JSON.stringify(body), { status }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {}, hub, F, uid: () => (++seq).toString(16), convKey,
    convsOf: () => F.conversations(),
    emitConv: (pid, cid, type, data) => { events.push([type, data]); hub.emit(convKey(pid, cid), 'conversation', { type, data }); },
    runReply: () => {},
  });
  const call = async (method, path, body) => {
    const u = new URL(`http://mock.local/api/v1${path}`);
    const r = routes.find((x) => x.method === method && x.re.test(u.pathname));
    assert.ok(r, `没有路由 ${method} ${path}`);
    const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {} });
    return { status: res.status, body: await res.json() };
  };
  return { call, events, hub };
}
const P = `/projects/${F.MAIN_PID}`;

test('mock：8 份报告；规则检查带修复轮次和裁决，裁决后收起、那一条转修复中', async () => {
  const { call, hub } = mockCtx();
  const list = (await call('GET', `${P}/reports`)).body.items;
  assert.equal(list.length, 8);
  assert.deepEqual(list.slice(0, 3).map((r) => r.type), ['rule', 'link', 'research']);
  const rule = (await call('GET', `${P}/reports/rp-rule`)).body;
  assert.equal(rule.repair.exhausted, true);
  assert.equal(rule.arbitration.kind, 'expert');
  assert.equal(rule.arbitration.options.length, 3);
  const items = (await call('GET', `${P}/reports/rp-rule/items`)).body;
  assert.deepEqual(items.counts, { error: 2, warning: 3, fixed: 2 });
  hub.emit(`conv:${F.MAIN_PID}:${rule.arbitration.conversation_id}`, 'conversation', { type: 'question.answered', data: { question_id: rule.arbitration.question_id } });
  const after = (await call('GET', `${P}/reports/rp-rule`)).body;
  assert.equal(after.arbitration, null);
  assert.equal(after.items[0].status, 'fixing');
  const raw = (await call('GET', `${P}/reports/rp-rule/raw`)).body;
  assert.match(raw.content_md, /^# 规则检查报告 · 超量与审批提示/);
  assert.equal((await call('GET', '/projects/bike-fleet-7b20e4a1/reports')).body.items.length, 0);
});

test('mock：发给助手（单条 / 全部）、重新校验（链接同步，其余发命令）', async () => {
  const { call, events } = mockCtx();
  const one = await call('POST', `${P}/reports/rp-rule/fix`, { item_ids: ['ri3'] });
  assert.equal(one.status, 202);
  assert.ok(one.body.conversation_id);
  assert.equal((await call('GET', `${P}/reports/rp-rule/items?status=fixing`)).body.items.some((x) => x.id === 'ri3'), true);
  assert.equal((await call('POST', `${P}/reports/rp-rule/fix`, { item_ids: ['ri4'] })).status, 400);
  assert.equal((await call('POST', `${P}/reports/rp-link/fix`, {})).status, 202);
  assert.ok(events.some(([type, d]) => type === 'message.user' && d.origin.kind === 'report'));
  const link = await call('POST', `${P}/reports/recheck`, { kind: 'link' });
  assert.equal(link.status, 200);
  assert.equal(link.body.report.type, 'link');
  assert.equal((await call('POST', `${P}/reports/recheck`, { kind: 'rule', mode: 'regression' })).status, 202);
  assert.equal((await call('POST', `${P}/reports/recheck`, { kind: 'research' })).status, 400);
});

test('资料用途：由助手判断的不显示成 auto（V 阶段截图）', () => {
  assert.equal(PURPOSE_LABEL.auto, '由助手判断');
});
