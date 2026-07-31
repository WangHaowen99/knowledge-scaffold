import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildPackageChatHistory,
  canUseStructuredMessageActions,
  confirmedMilestoneActionIds,
  isMaterialAnalysisRunning,
  latestStructuredActionMessageId,
  mergePackageChatTimeline,
} from '../frontend/src/packageChatTimeline.js';

test('已经确认的里程碑动作不再作为可点击按钮展示', () => {
  assert.deepEqual(confirmedMilestoneActionIds({
    milestones: [
      { id: 'knowledge_confirmed', status: 'confirmed' },
      { id: 'deterministic_confirmed', status: 'confirmed' },
      { id: 'construction_completed', status: 'active' },
    ],
  }), ['confirm_knowledge', 'confirm_deterministic']);
});

test('AI 辅助构建完成后历史动作卡全部只读', () => {
  assert.equal(canUseStructuredMessageActions({
    busy: false,
    constructionStatus: 'completed',
    messageId: 'confirm-card',
    latestActionMessageId: 'confirm-card',
  }), false);
  assert.equal(canUseStructuredMessageActions({
    busy: false,
    constructionStatus: 'active',
    messageId: 'confirm-card',
    latestActionMessageId: 'confirm-card',
  }), true);
});

test('建模助手把普通消息和结构化卡片按发生时间升序展示', () => {
  const timeline = mergePackageChatTimeline({
    turns: [
      { role: 'assistant', text: '已上传建模资料', createdAt: '2026-07-23T10:00:00.000Z' },
      { role: 'user', text: '确认', createdAt: '2026-07-23T10:00:20.000Z' },
    ],
    structuredMessages: [
      { id: 'analysis', createdAt: '2026-07-23T10:00:10.000Z', blocks: [] },
    ],
  });

  assert.deepEqual(timeline.map((item) => item.kind), ['turn', 'structured', 'turn']);
  assert.deepEqual(timeline.map((item) => item.item.text ?? item.item.id), [
    '已上传建模资料', 'analysis', '确认',
  ]);
});

test('没有时间戳的历史普通消息放在服务端结构化卡片之前', () => {
  const timeline = mergePackageChatTimeline({
    turns: [{ role: 'assistant', text: '历史上传说明' }],
    structuredMessages: [{ id: 'later-card', createdAt: '2026-07-23T10:00:10.000Z', blocks: [] }],
  });

  assert.deepEqual(timeline.map((item) => item.kind), ['turn', 'structured']);
});

test('只有时间线中最新的下一步动作卡可以继续操作', () => {
  const timeline = mergePackageChatTimeline({
    turns: [],
    structuredMessages: [
      { id: 'old', createdAt: '2026-07-23T10:00:00.000Z', blocks: [{ type: 'next_actions', actions: [] }] },
      { id: 'status', createdAt: '2026-07-23T10:00:10.000Z', blocks: [{ type: 'status', title: '处理中' }] },
      { id: 'current', createdAt: '2026-07-23T10:00:20.000Z', blocks: [{ type: 'next_actions', actions: [] }] },
    ],
  });

  assert.equal(latestStructuredActionMessageId(timeline), 'current');
});

test('给建模助手的上下文从服务端消息恢复且忽略状态卡', () => {
  const history = buildPackageChatHistory({
    turns: [{ role: 'user', text: '本页尚未刷新的追问', createdAt: '2026-07-23T10:00:30.000Z' }],
    structuredMessages: [
      { id: 'status', role: 'assistant', createdAt: '2026-07-23T10:00:00.000Z', blocks: [{ type: 'status', title: '正在分析' }] },
      { id: 'user', role: 'user', createdAt: '2026-07-23T10:00:10.000Z', blocks: [{ type: 'text', text: '请解释当前审批边界' }] },
      { id: 'assistant', role: 'assistant', createdAt: '2026-07-23T10:00:20.000Z', blocks: [{ type: 'text', text: '当前边界以提交为起点' }] },
    ],
  });

  assert.deepEqual(history, [
    { role: 'user', text: '请解释当前审批边界' },
    { role: 'assistant', text: '当前边界以提交为起点' },
    { role: 'user', text: '本页尚未刷新的追问' },
  ]);
});

test('资料分析运行状态来自服务端任务图而不是浏览器本地 busy', () => {
  assert.equal(isMaterialAnalysisRunning({
    tasks: [{ kind: 'material_analysis', status: 'running' }],
  }), true);
  assert.equal(isMaterialAnalysisRunning({
    tasks: [{ kind: 'package_baseline_review', status: 'running' }],
  }), true);
  assert.equal(isMaterialAnalysisRunning({
    tasks: [{ kind: 'material_analysis', status: 'completed' }],
  }), false);
});
