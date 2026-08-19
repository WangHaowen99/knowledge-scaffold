import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isPackageChatRunning,
  waitingInteractionRecoveryDelay,
  waitingInteractionTitle,
  waitingPackageChatRun,
  WAITING_INTERACTION_RECOVERY_MS,
} from '../frontend/src/packageChatRun.js';

test('建模助手仅在排队或执行时显示正在分析', () => {
  assert.equal(isPackageChatRunning({
    activeRuns: [{ status: 'queued', state: { purpose: 'package_chat' } }],
  }), true);
  assert.equal(isPackageChatRunning({
    activeRuns: [{ status: 'running', state: { purpose: 'package_chat' } }],
  }), true);
});

test('建模助手等待用户确认时停止显示正在分析', () => {
  assert.equal(isPackageChatRunning({
    activeRuns: [{ status: 'waiting', state: { purpose: 'package_chat' } }],
    pendingInteractions: [{ type: 'change_plan', status: 'pending' }],
  }), false);
});

test('其他用途的运行任务不占用建模助手输入区', () => {
  assert.equal(isPackageChatRunning({
    activeRuns: [{ status: 'running', state: { purpose: 'controlled_repair' } }],
  }), false);
  assert.equal(isPackageChatRunning(undefined), false);
});

test('waiting 的访谈运行用于题卡恢复但不显示为运行中', () => {
  const run = {
    id: 'run-1', intent: '业务流程访谈', status: 'waiting',
    updatedAt: '2026-08-19T00:00:00.000Z', state: { purpose: 'package_chat' },
  };
  const authoring = { activeRuns: [run] };
  assert.equal(isPackageChatRunning(authoring), false);
  assert.deepEqual(waitingPackageChatRun(authoring), run);
  assert.equal(waitingInteractionTitle(run), '访谈题卡加载失败');
  assert.equal(
    waitingInteractionRecoveryDelay(run, Date.parse(run.updatedAt)),
    WAITING_INTERACTION_RECOVERY_MS,
  );
  assert.equal(waitingInteractionRecoveryDelay(
    run, Date.parse(run.updatedAt) + WAITING_INTERACTION_RECOVERY_MS,
  ), 0);
});

test('非访谈 waiting 运行使用通用题卡提示', () => {
  assert.equal(waitingInteractionTitle({ intent: '修改知识包' }), '待办卡片加载失败');
  assert.equal(waitingPackageChatRun({
    activeRuns: [{ status: 'waiting', state: { purpose: 'other' } }],
  }), null);
});
