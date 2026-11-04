import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { mockData, register, stages } from '../src/api/mock/routes-materials.js';
import { blueprintPayload, goalPending, goalQuestion, isDirty } from '../src/pages/workbench/materials/model.js';

const bp = () => mockData.blueprintOf();
const editOf = (b) => ({
  terms: Object.fromEntries(b.terms.ambiguous.map((t) => [t.id, t.chosen])),
  answers: Object.fromEntries(b.questions.map((q) => [q.id, null])),
});

test('BP-02 请求体：F8 起不分段，只带改过的叫法和回答，按草案保存', () => {
  const b = bp();
  const edit = editOf(b);
  assert.equal(isDirty(blueprintPayload(b, edit)), false);
  const body = blueprintPayload(b, { terms: { ...edit.terms, 't-expiry': '临期' }, answers: { ...edit.answers, 'bq-ledger': 'A' } });
  assert.equal(body.base_revision, b.revision);
  assert.equal(body.mode, 'draft');
  assert.equal(body.slices, undefined);
  // 「不在范围」已取消（F4b），请求体里不再有这一项
  assert.equal(body.out_of_scope, undefined);
  assert.deepEqual(body.term_choices, [{ term_id: 't-expiry', chosen: '临期' }]);
  assert.deepEqual(body.answers, [{ question_id: 'bq-ledger', option_id: 'A' }]);
  assert.ok(isDirty(body));
  // 老请求体里的分段列表不算改动
  assert.equal(isDirty({ base_revision: 'r', mode: 'draft', slices: [] }), false);
});

test('mock 蓝图：只有一段「全部」，蓝图卡没有确认按钮', () => {
  const b = bp();
  assert.deepEqual(b.slices.map((s) => s.id), ['all']);
  const card = mockData.blueprintCard(b);
  assert.deepEqual(card.confirm_options, []);
  assert.equal(card.status, null);
  assert.equal(card.questions_pending, 3);
  assert.deepEqual(card.terms, { aligned: 13, ambiguous: 2 });
});

// mock 路由：BP-01 / BP-02 / BP-03
function fakeServer(stage) {
  const routes = [];
  const events = [];
  let n = 0;
  const flow = stages[stage].flow(F.flow());
  register({
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => ({ status, body }),
    err: (status, code, message, details) => ({ status, body: { code, message, details } }),
    db: { uploads: new Map() },
    uid: () => String((n += 1)),
    convsOf: () => [{ id: mockData.CID }],
    flowOf: () => flow,
    stageOf: () => stage,
    emitConv: (pid, cid, type, data) => events.push({ type, data }),
    emitProj: (pid, type, data) => events.push({ type, data }),
    runReply: () => {},
  });
  const call = (method, path, body = {}) => {
    const full = `/api/v1/projects/retail-restock-3f9a1c2d${path}`;
    const r = routes.find((x) => x.method === method && x.re.test(full));
    return r.handler({ m: full.match(r.re), body, query: new URLSearchParams(), form: null });
  };
  return { call, events, flow };
}

test('BP-02：版本号冲突 409；保存叫法与回答不改草案号，并更新蓝图卡', () => {
  const s = fakeServer('blueprint');
  const cur = s.call('GET', '/blueprint').body;
  assert.equal(cur.version_label, '草案 v2');
  assert.equal(s.call('PUT', '/blueprint', { base_revision: 'old', mode: 'draft' }).body.code, 'revision_conflict');
  const r = s.call('PUT', '/blueprint', {
    base_revision: cur.revision, mode: 'draft',
    term_choices: [{ term_id: 't-expiry', chosen: '临期' }], answers: [{ question_id: 'bq-ledger', option_id: 'A' }],
  });
  assert.equal(r.status, 200);
  assert.equal(r.body.version_label, '草案 v2');
  assert.equal(r.body.terms.ambiguous.find((t) => t.id === 't-expiry').chosen, '临期');
  const card = s.events.filter((e) => e.type === 'card.updated').at(-1).data;
  assert.equal(card.questions_pending, 2);
  assert.deepEqual(card.confirm_options, []);
});

test('目标问题：不挡建模，没答之前按原目标建；放在目标下面答（F4b、F8）', () => {
  assert.equal(goalPending({ goal_question: { id: 'q1', title: '以哪个为准？', answered: false } }), true);
  assert.equal(goalPending({ goal_question: { id: 'q1', title: '以哪个为准？', answered: true } }), false);
  assert.equal(goalPending({ goal_question: null }), false);
  const goal = { id: 'q2', kind: 'goal', title: '以哪个为准？', options: [{ id: 'A', label: '改成：按盘点生成补货单' }, { id: 'B', label: '保持原目标' }] };
  assert.equal(goalQuestion([{ id: 'q1', title: '台账要不要建成对象？' }, goal]), goal);
  assert.equal(goalQuestion([{ id: 'q1', title: '台账要不要建成对象？' }]), null);
});

test('BP-03（只有 F8 之前缓存的卡片会调）：资料分析标为已确认；L4 的预览只有「全部」', () => {
  const s = fakeServer('blueprint');
  const cur = s.call('GET', '/blueprint').body;
  const l2 = s.call('POST', '/blueprint/confirm', { base_revision: cur.revision, next: 'L2' });
  assert.equal(l2.body.blueprint.state, 'confirmed');
  assert.match(l2.body.command_task.title, /开始提取对象/);
  assert.equal(s.flow.nodes.find((x) => x.id === 'core.research').state, 'confirmed');
  const s2 = fakeServer('blueprint');
  const c2 = s2.call('GET', '/blueprint').body;
  const l4 = s2.call('POST', '/blueprint/confirm', { base_revision: c2.revision, next: 'L4' });
  assert.deepEqual(l4.body.autobuild_preview.slices.map((x) => x.id), ['all']);
  assert.equal(l4.body.command_task, undefined);
});
