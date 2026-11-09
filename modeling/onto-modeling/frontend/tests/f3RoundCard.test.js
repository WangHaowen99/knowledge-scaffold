// F3：去掉「确认生效」——一轮结束就合入成版本，一轮一张卡，只留撤回这一轮。
import assert from 'node:assert/strict';
import test from 'node:test';
import { applyEvents, initialConversation } from '../src/state/conversation.js';
import { roundState, savedText } from '../src/pages/workbench/roundState.js';

let n = 0;
const ev = (type, data) => ({ id: String(++n), type, ts: '2026-11-03T07:21:00.000Z', data });
const round = (cardId, changesetId, state, title = '改了 3 处') => ({ card_id: cardId, card_type: 'round_changes', changeset_id: changesetId, state, title });

test('同一个变更集只画一张「这一轮」卡：升级前出过两张的留最新那张，旧卡的更新也不再画', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    ev('card', round('rc-cs-1-1', 'cs-1', 'pending_confirm', '改了 8 处')),
    ev('message.user', { message_id: 'm1', text: '继续' }),
    ev('card', round('rc-cs-1-2', 'cs-1', 'pending_confirm', '改了 9 处')),
    ev('card.updated', round('rc-cs-1-1', 'cs-1', 'pending_confirm', '改了 9 处')),
    ev('card', round('rc-cs-2-1', 'cs-2', 'merged')),
  ]);
  const cards = s.order.map((k) => s.items[k]).filter((x) => x.kind === 'card');
  assert.deepEqual(cards.map((c) => c.card.card_id), ['rc-cs-1-2', 'rc-cs-2-1']);
  assert.equal(cards[0].card.title, '改了 9 处');
});

test('卡片状态：已存成版本才能撤回这一轮；撤回后、冲突时、升级前的卡都没有确认按钮', () => {
  assert.equal(roundState({ state: 'merged' }), 'merged');
  assert.equal(roundState({ state: 'reverted' }), 'reverted');
  assert.equal(roundState({ state: 'discarded' }), 'reverted');
  assert.equal(roundState({ state: 'conflict' }), 'conflict');
  assert.equal(roundState({ state: 'pending_confirm' }), 'pending');
});

test('mock：回答决策卡后这一轮直接存成版本，建议条没有确认生效；撤回这一轮另存一个版本', async () => {
  const PID = 'retail-restock-3f9a1c2d';
  globalThis.window = { location: { search: '' }, localStorage: { getItem: () => null } };
  const { createMockServer } = await import('../src/api/mock/index.js');
  const { CHANGESET_ID, QUESTION_ID } = await import('../src/api/mock/fixtures.js');
  const s = createMockServer();
  delete globalThis.window;
  const call = async (method, path, body) => {
    const res = await s.fetch(`/api/v1/projects/${PID}${path}`, {
      method, headers: { Authorization: 'Bearer mock.zhangsan.1' }, body: body ? JSON.stringify(body) : undefined,
    });
    return { status: res.status, body: res.status === 204 ? null : await res.json() };
  };
  const cid = (await call('GET', '/conversations')).body.items[0].id;
  const answered = await call('POST', `/conversations/${cid}/questions/${QUESTION_ID}/answer`, { option_id: 'A' });
  assert.ok(answered.status < 300, JSON.stringify(answered.body));
  await new Promise((r) => setTimeout(r, 1800));
  const events = s.hub.history(`conv:${PID}:${cid}`);
  const card = events.filter((e) => e.type === 'card' && e.data.card_type === 'round_changes').at(-1).data;
  assert.equal(card.state, 'merged');
  assert.equal(card.merged_version_label, 'v0.13');
  assert.ok(!('next_version_label' in card));
  const bars = events.filter((e) => e.type === 'suggestions.updated').map((e) => e.data);
  assert.ok(bars.every((b) => b.mode !== 'pending_confirm'));
  assert.ok(!bars.some((b) => [b.primary, ...(b.secondary || [])].some((x) => x?.action?.type === 'merge_changeset')));
  assert.equal((await call('GET', '/scope')).body.state, 'none');
  const undone = await call('POST', `/changesets/${CHANGESET_ID}/discard`);
  assert.equal(undone.body.state, 'reverted');
  assert.equal(undone.body.version_label, 'v0.14');
  const after = s.hub.history(`conv:${PID}:${cid}`).filter((e) => e.type === 'card.updated' && e.data.card_type === 'round_changes').at(-1).data;
  assert.equal(roundState(after), 'reverted');
});

test('中断卡的「已保存」：没存下文件说「没有」，没版本号不写版本', () => {
  assert.equal(savedText({ files: 0, version_label: null }), '没有');
  assert.equal(savedText(null), '没有');
  assert.equal(savedText({ files: 2, version_label: null }), '2 个文件');
  assert.equal(savedText({ files: 2, version_label: 'v0.7' }), '2 个文件，版本 v0.7');
});
