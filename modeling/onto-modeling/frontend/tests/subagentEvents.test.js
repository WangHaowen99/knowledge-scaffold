// develop tests/subagentEvents.test.js 里子任务卡收尾判定的用例，按原断言移植（develop !568，ADR-0097）。
// develop 在前端拿原始事件现算（resolveSubagentState、endedByStall）；本分支前端收的是 v1 事件，由状态层归约，
// 这里把 develop 的原始事件换成对应的 v1 事件，把状态层给的结果换成 develop 的形状再比。
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyEvents, initialConversation, subagentStatus } from '../src/state/conversation.js';

let n = 0;
const ev = (type, data) => ({ id: String(++n), type, ts: '2026-11-12T03:00:00.000Z', data });
// 派遣（develop 的 Task tool_use 与 subagent_started）→ 时间线上的子任务卡
const dispatch = (id, title) => ev('subagent.updated', { task_id: id, group_id: 'g1', title, status: 'started', progress_text: '' });
const finished = (id, status = 'done') => ev('subagent.updated', { task_id: id, group_id: 'g1', title: '分析资料', status, progress_text: '' });
const state = (value) => ev('session.state', { state: value });
const result = () => ev('turn.completed', { turn_id: 't', duration_ms: 1, is_error: false });
const sessionError = (reason) => ev('session.error', { error_id: `e${n}`, message: '出错', retries: 0, retryable: true, actions: ['retry'], reason });
const interrupted = () => ev('session.interrupted', { by: 'restart', at: null, exec_id: null, position_label: 'x', saved: null });

// 和工作台时间线一样取卡片状态（Timeline：ended = 会话不在运行），换成 develop resolveSubagentState 的形状
function cardState(events, id) {
  n = 0;
  const s = applyEvents(initialConversation(), events());
  const status = subagentStatus(s.items[`sub:${id}`], !['running', 'connecting'].includes(s.session.state));
  const ended = ['interrupted', 'timeout'].includes(status);
  return {
    done: ['done', 'failed'].includes(status) || ended, failed: status === 'failed', stopped: ended, interrupted: ended,
    background: false,
  };
}
// develop endedByStall：派遣之后最先发生的收尾是不是判定超时
const endedByStall = (events, id) => {
  n = 0;
  const s = applyEvents(initialConversation(), [state('running'), ...events()]);
  return subagentStatus(s.items[`sub:${id}`], false) === 'timeout';
};

test('尚无终态而会话已关闭或被打断：显示已中断，不再一直运行中', () => {
  // 空闲回收：引擎关闭（本分支没有后台派遣，原用例的 background 一项恒为 false）
  assert.deepEqual(cardState(() => [state('running'), dispatch('T1', '分析资料'), state('closed')], 'T1'), {
    done: true, failed: false, stopped: true, interrupted: true, background: false,
  });
  // 同步派遣途中服务重启：没有回执也没有终态，补写的中断标记让卡片停止转圈
  assert.equal(cardState(() => [state('running'), dispatch('T1', '分析资料'), interrupted()], 'T1').interrupted, true);
});

test('已有同步结果时会话随后关闭，仍显示完成而不是已中断', () => {
  const s = cardState(() => [state('running'), dispatch('T1', '分析资料'), finished('T1'), state('closed')], 'T1');

  assert.deepEqual({ done: s.done, interrupted: s.interrupted }, { done: true, interrupted: false });
});

test('会话结束发生在派遣之前时不影响之后的派遣', () => {
  const s = cardState(() => [state('closed'), state('running'), dispatch('T1', '分析资料')], 'T1');

  assert.deepEqual({ done: s.done, interrupted: s.interrupted }, { done: false, interrupted: false });
});

test('一轮已收尾却没有结果：显示已中断，老版本遗留的卡片升级后也能恢复', () => {
  // #387 的现场形态：派遣之后直接 result + idle，没有回执也没有生命周期事件
  assert.deepEqual(cardState(() => [state('running'), dispatch('T1', '子任务'), result(), state('idle')], 'T1'), {
    done: true, failed: false, stopped: true, interrupted: true, background: false,
  });
});

test('上一轮的收尾不影响这一轮新派遣的子任务', () => {
  assert.equal(cardState(() => [state('idle'), state('running'), dispatch('T1', '分析资料')], 'T1').done, false);
});

test('只有派遣之后最先发生的收尾是判定超时，才算执行超时', () => {
  const card = () => dispatch('T1', '分析资料');
  const stall = () => sessionError('stall_timeout');

  assert.equal(endedByStall(() => [card(), stall()], 'T1'), true);
  // 普通出错、打断、一轮收尾都不是超时
  assert.equal(endedByStall(() => [card(), sessionError(null)], 'T1'), false);
  assert.equal(endedByStall(() => [card(), interrupted(), stall()], 'T1'), false);
  // 上一轮遗留的卡片不算下一轮的超时
  assert.equal(endedByStall(() => [card(), state('idle'), stall()], 'T1'), false);
  assert.equal(endedByStall(() => [card()], 'T1'), false);
});
