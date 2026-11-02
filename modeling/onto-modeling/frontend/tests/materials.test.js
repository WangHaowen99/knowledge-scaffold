import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { mockData, projectStages, register, stages } from '../src/api/mock/routes-materials.js';
import { applyEvents, initialConversation, timelineBlocks } from '../src/state/conversation.js';
import { trackNodes } from '../src/state/flow.js';
import { composerState } from '../src/state/readonly.js';
import {
  MATERIAL_DIALOGS, acceptLabel, anchorLabel, anchorTokens, countPairs, miniStatus, parseAnchor, purposeLabel, readState,
  shortPosition, unreadText, verificationNote, verificationRows,
} from '../src/pages/workbench/materials/model.js';

const item = (o) => ({
  path: 'resource/others/x.docx', name: 'x.docx', kind: 'doc', purpose: 'evidence', purpose_auto: true, read_pct: 100, status: 'done',
  position: null, queue_reason: null, contradiction: null, blocked: null, counts: { objects: 3, rules: 0, terms: 4 }, ...o,
});

test('读取状态：读完 / 读取中 / 排队中 / 读了一部分', () => {
  assert.equal(readState(item()), 'done');
  assert.equal(readState(item({ status: 'reading', read_pct: 78 })), 'reading');
  assert.equal(readState(item({ status: 'queued', read_pct: 0 })), 'queued');
  assert.equal(readState(item({ status: 'partial', read_pct: 50 })), 'partial');
});

test('迷你资料卡最后一行：没读完的 Sheet、矛盾优先，其次读到哪', () => {
  const blk = item({ status: 'partial', read_pct: 50, blocked: { reason: '只读了 Sheet「品类」', unread_parts: ['Sheet「到货周期」'] }, contradiction: '与规则 §9.2 有 1 处矛盾' });
  assert.deepEqual(miniStatus(blk), { tone: 'amber', icon: 'alert', text: '有 1 个 Sheet 还没读' });
  assert.equal(miniStatus(item({ contradiction: '与规则 §9.2 有 1 处矛盾' })).text, '与规则有 1 处矛盾');
  assert.deepEqual(miniStatus(item({ status: 'reading', read_pct: 78, position: '读到 §10 · 还剩 §10–§12' })), { tone: 'blue', spin: true, text: '读到 §10' });
  assert.equal(miniStatus(item({ status: 'queued', read_pct: 0 })).text, '排队中');
  assert.equal(miniStatus(item({ counts: { objects: 1, fields: 12, terms: 0 } })).text, '读完 · 对象 1');
  assert.equal(unreadText({ unread_parts: ['§8', '§9'] }), '有 2 处还没读');
  assert.equal(shortPosition('读到 §10 · 还剩 §10–§12'), '读到 §10');
});

test('计数：表格显示字段，文档显示规则；用途中文名', () => {
  assert.deepEqual(countPairs({ objects: 6, rules: 14, terms: 21 }), [['对象', 6], ['规则', 14], ['术语', 21]]);
  assert.deepEqual(countPairs({ objects: 2, fields: 9, terms: 6 }), [['对象', 2], ['字段', 9], ['术语', 6]]);
  assert.equal(purposeLabel('schema'), '数据 Schema');
  assert.equal(purposeLabel('auto'), '由助手判断');
});

test('上传卡的格式说明按类别归并', () => {
  assert.equal(acceptLabel(['.md', '.txt', '.pdf', '.doc', '.docx', '.xlsx', '.csv', '.drawio'], 20 * 1024 * 1024), 'md / txt / pdf / word / excel / drawio · 单个不超过 20 MB');
  assert.equal(acceptLabel([], 0), '');
});

test('出处：锚点拆解、兜底标签、正文里的加粗和角标', () => {
  assert.deepEqual(parseAnchor('file:resource/others/02-门店补货规则.docx#p:9.2'), { scheme: 'file', path: 'resource/others/02-门店补货规则.docx', fragment: 'p:9.2' });
  assert.equal(anchorLabel('file:resource/others/02-门店补货规则.docx#p:9.2'), '门店补货规则 §9.2');
  assert.equal(anchorLabel('db:erp-prod/erp.replenish_std.max_qty'), 'erp.replenish_std.max_qty');
  assert.deepEqual(anchorTokens('按**区域统一配额**执行[^1]，比较[^2]'), [
    { t: 'text', v: '按' }, { t: 'bold', v: '区域统一配额' }, { t: 'text', v: '执行' }, { t: 'cite', n: 1 }, { t: 'text', v: '，比较' }, { t: 'cite', n: 2 },
  ]);
});

test('核验：有问题的排前面，通过的默认只露两条；说明里列出无法核验的原因', () => {
  const items = mockData.verificationItems(true);
  assert.equal(items.length, 46);
  const { rows, hidden } = verificationRows(items);
  assert.deepEqual(rows.slice(0, 2).map((r) => r.status), ['unverifiable', 'unverifiable']);
  assert.equal(rows.filter((r) => r.status === 'passed').length, 2);
  assert.equal(hidden, items.filter((x) => x.status === 'passed').length - 2);
  assert.equal(verificationRows(items, { expanded: true }).hidden, 0);
  assert.match(verificationNote(items), /^「来源无法核验」需要修正读取范围或来源位置：R-07 引用的 §7\.3 在原文里找不到这句话；/);
  assert.equal(verificationNote(mockData.verificationItems(false)), '');
});

test('输入框占位：读资料和切片规划不同；运行中仍是「运行中也能补充说明」', () => {
  assert.equal(composerState({ sessionState: 'idle', researching: 'materials' }).placeholder, '给助手补充说明，或者继续拖入资料…');
  assert.equal(composerState({ sessionState: 'idle', researching: true }).placeholder, '给助手补充说明，或者继续拖入资料…');
  assert.match(composerState({ sessionState: 'idle', researching: 'blueprint' }).placeholder, /^对蓝图有意见直接说/);
  assert.match(composerState({ sessionState: 'running', researching: 'materials' }).placeholder, /运行中也能补充说明/);
});

test('mock 阶段 materials：资料地图卡、资料用途问题卡、建议条（1 个主推 + 2 个次选）、流程条「读资料 5/7」', () => {
  const st = applyEvents(initialConversation(), stages.materials.history());
  const blocks = timelineBlocks(st);
  const cards = blocks.filter((b) => b.type === 'assistant').flatMap((b) => b.items).filter((x) => x.kind === 'card').map((x) => x.card.card_type);
  assert.deepEqual(cards, ['upload_prompt', 'material_map']);
  const q = blocks.find((b) => b.type === 'question');
  assert.equal(q.item.question.kind, 'decision');
  assert.match(q.item.question.title, /05-门店清单\.xlsx/);
  assert.equal(st.suggestions.primary.disabled, true);
  assert.equal(st.suggestions.secondary.length, 2);
  assert.ok(st.suggestions.secondary.length <= 2);
  const first = trackNodes(stages.materials.flow(F.flow()))[0];
  assert.equal(first.id, 'core.research');
  assert.equal(first.display_state, 'running');
  assert.equal(first.progress.label, '读资料 5/7');
  assert.ok(trackNodes(stages.materials.flow(F.flow())).slice(1).every((n) => n.display_state === 'not_ready'));
  assert.deepEqual(stages.materials.flow(F.flow()).slices, []);
});

test('mock 阶段 materials-empty / estate-empty / blueprint', () => {
  const empty = applyEvents(initialConversation(), stages['materials-empty'].history());
  assert.equal(empty.suggestions.primary, null);
  assert.deepEqual(empty.suggestions.secondary.map((s) => s.label), ['先聊聊目标', '连接业务系统']);
  assert.equal(trackNodes(stages['materials-empty'].flow(F.flow()))[0].progress.label, '等资料');
  assert.equal(projectStages['estate-repair-c4d2b915'], 'estate-empty');
  const estate = applyEvents(initialConversation(), stages['estate-empty'].history());
  assert.match(timelineBlocks(estate)[0].item.text, /物业报修工单处理/);
  const bp = applyEvents(initialConversation(), stages.blueprint.history());
  const types = timelineBlocks(bp).filter((b) => b.type === 'assistant').flatMap((b) => b.items).filter((x) => x.kind === 'card').map((x) => x.card.card_type);
  assert.deepEqual(types, ['research_summary', 'blueprint', 'build_choice']);
  assert.equal(bp.suggestions.secondary.length, 2);
  assert.equal(bp.suggestions.primary.action.type, 'confirm_blueprint');
  const node = trackNodes(stages.blueprint.flow(F.flow()))[0];
  assert.equal(node.state, 'pending_confirm');
  assert.equal(node.progress.label, '蓝图待确认');
  assert.equal(stages.blueprint.flow(F.flow()).slices.length, 5);
  assert.ok(MATERIAL_DIALOGS.includes('blueprint'));
});

// 用假的 ctx 跑路由：只看请求、响应和发出的事件
function fakeServer(stage) {
  const routes = [];
  const events = [];
  let n = 0;
  const flow = stages[stage]?.flow ? stages[stage].flow(F.flow()) : F.flow();
  const ctx = {
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
  };
  register(ctx);
  const call = (method, path, { body = {}, query = '' } = {}) => {
    const full = `/api/v1${path}`;
    const r = routes.find((x) => x.method === method && x.re.test(full));
    return r.handler({ m: full.match(r.re), body, query: new URLSearchParams(query), form: null });
  };
  return { call, events };
}
const PID = '/projects/retail-restock-3f9a1c2d';

test('R-07 / R-03 / R-10：读资料中的资料地图、改用途、问题转待答', () => {
  const s = fakeServer('materials');
  const map = s.call('GET', `${PID}/materials/map`).body;
  assert.equal(map.total, 7);
  assert.equal(map.verification.unverifiable, 2);
  assert.equal(map.problems.length, 2);
  const r = s.call('PATCH', `${PID}/resources/purpose`, { body: { path: map.items[5].path, purpose: 'term' } });
  assert.equal(r.status, 200);
  assert.equal(r.body.material.purpose, 'term');
  assert.ok(r.body.reread_task, '分析开始后改用途要重读');
  assert.ok(s.events.some((e) => e.type === 'materials.updated'));
  assert.ok(s.events.some((e) => e.type === 'card.updated' && e.data.card_type === 'material_map'));
  assert.equal(s.call('PATCH', `${PID}/resources/purpose`, { body: { path: 'nope', purpose: 'term' } }).body.code, 'file_not_found');
  const p = s.call('POST', `${PID}/materials/problems/pb-miss-84/promote`, { body: { as: 'ask_me' } });
  assert.match(p.body.inbox_item_id, /^decision:/);
  assert.equal(s.call('GET', `${PID}/materials/map`).body.problems.find((x) => x.id === 'pb-miss-84').state, 'promoted');
  const prev = s.call('GET', `${PID}/resources/preview`, { query: 'path=resource/others/03-样例补货单-B.docx&around=p:4' }).body;
  const blk = prev.blocks.find((b) => b.anchor === 'p:4');
  assert.equal(blk.text.slice(blk.highlights[0].start, blk.highlights[0].end), '区域统一配额 ≤100 箱');
});

test('还没传资料的项目：资料地图为空，没有蓝图', () => {
  const s = fakeServer('materials-empty');
  assert.equal(s.call('GET', `${PID}/materials/map`).body.total, 0);
  assert.equal(s.call('GET', `${PID}/blueprint`).body.code, 'blueprint_not_found');
});
