import assert from 'node:assert/strict';
import test from 'node:test';

import { isPackageChatRunning } from '../frontend/src/packageChatRun.js';

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
