import { test } from 'node:test';
import assert from 'node:assert/strict';

import { aiConstructionTaskStatusTransitions } from '../src/ai-construction/task-graph.js';

const ALL_STATUSES = [
  'pending', 'ready', 'running', 'waiting_user', 'waiting_confirmation',
  'completed', 'failed', 'skipped', 'stale', 'cancelled',
] as const;

test('任务状态迁移表覆盖全部状态变更命令', () => {
  const transitions = aiConstructionTaskStatusTransitions();
  assert.deepEqual(Object.keys(transitions).sort(), [
    'complete_task', 'fail_task', 'reopen_task', 'start_task', 'wait_for_user',
  ]);
  for (const [command, rule] of Object.entries(transitions)) {
    assert.ok(ALL_STATUSES.includes(rule.to as any), `${command} 的目标状态不在状态枚举内`);
    for (const from of rule.from) {
      assert.ok(ALL_STATUSES.includes(from as any), `${command} 的来源状态 ${from} 不在状态枚举内`);
    }
  }
});

test('终态不能作为迁移来源，避免已收敛的任务被改写', () => {
  const transitions = aiConstructionTaskStatusTransitions();
  for (const [command, rule] of Object.entries(transitions)) {
    for (const terminal of ['skipped', 'cancelled', 'stale'] as const) {
      assert.ok(!rule.from.includes(terminal), `${command} 不应允许从 ${terminal} 迁移`);
    }
  }
  // 只有明确的“退回补充”能从已完成状态出发
  assert.deepEqual(transitions.reopen_task.from, ['completed']);
  assert.ok(!transitions.complete_task.from.includes('completed'));
});

/**
 * 这两个状态目前不是任何迁移的目标：任务图自身不会产生它们。
 * 若某天要启用，请在 TASK_STATUS_TRANSITIONS 补迁移并同步更新此断言，
 * 免得它们继续以“看起来会出现、其实不可达”的形式误导读代码的人。
 */
test('ready 与 waiting_confirmation 目前不可达', () => {
  const targets = new Set(Object.values(aiConstructionTaskStatusTransitions()).map((rule) => rule.to));
  assert.equal(targets.has('ready'), false);
  assert.equal(targets.has('waiting_confirmation'), false);
  assert.deepEqual([...targets].sort(), ['completed', 'failed', 'running', 'waiting_user']);
});
