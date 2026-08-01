import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  blockingPendingInteractions,
  evidenceProblemActions,
  isStaleRepairInteraction,
  pendingInteractionActions,
} from '../src/authoring/construction-completion.js';
import { completionBlockerMessage } from '../frontend/src/completionBlocker.js';

const interaction = (overrides: Record<string, unknown> = {}) => ({
  id: 'ai-1', projectId: 'pj', runId: 'ar-1', type: 'question_batch',
  status: 'pending', payload: {}, createdAt: '2026-07-31T00:00:00.000Z', ...overrides,
}) as any;

test('缺证据问题按资源种类去重成文件跳转动作', () => {
  const actions = evidenceProblemActions([
    'object:order 缺少资料证据',
    'relation:order.belongs_to 缺少关系证据',
    'operation:plan 缺少流程或访谈证据',
    'operation:ship 缺少流程或访谈证据',
  ]);
  // object 与 relation 同属 objects/index.md，只给一个入口
  assert.deepEqual(actions.map((item) => item.path), ['objects/index.md', 'operations/index.md']);
  assert.equal(actions[0].id, 'open_package_file');
  assert.equal(actions[0].style, 'primary');
  assert.equal(actions[1].style, 'secondary');
});

test('无法识别的问题项不产生动作', () => {
  assert.deepEqual(evidenceProblemActions(['莫名其妙的问题', '']), []);
});

test('修订变化后的受控修复待办被判定为失效', () => {
  const stale = interaction({
    payload: { purpose: 'controlled_repair' }, baseRevision: 'rev-old',
  });
  assert.equal(isStaleRepairInteraction(stale, 'rev-new'), true);
  assert.equal(isStaleRepairInteraction(stale, 'rev-old'), false);
  // 非受控修复类待办不受修订影响
  assert.equal(isStaleRepairInteraction(
    interaction({ payload: { purpose: 'modeling_brief' }, baseRevision: 'rev-old' }),
    'rev-new',
  ), false);
});

/**
 * 后端计数与助手栏渲染必须用同一套规则：
 * 否则会出现「被拦住，但界面上找不到任何要处理的事项」的死路。
 */
test('失效的受控修复待办不再阻塞建模完成', () => {
  const blocking = blockingPendingInteractions([
    interaction({ id: 'stale', payload: { purpose: 'controlled_repair' }, baseRevision: 'rev-old' }),
    interaction({ id: 'live', payload: { purpose: 'modeling_brief', title: '请确认建模目标' } }),
  ], 'rev-new');
  assert.deepEqual(blocking.map((item) => item.id), ['live']);
  assert.equal(blocking[0].title, '请确认建模目标');
  assert.equal(blocking[0].location, 'assistant');
});

test('场景推演待办标出所在页签并给出跳转动作', () => {
  const blocking = blockingPendingInteractions([
    interaction({ id: 'sc', type: 'scenario_review', payload: { purpose: 'scenario' } }),
  ], 'rev-new');
  assert.equal(blocking[0].location, 'scenario');
  assert.deepEqual(pendingInteractionActions(blocking).map((item) => item.id), ['open_scenario_review']);
  // 只在助手栏的待办不需要跳转按钮
  assert.deepEqual(pendingInteractionActions([
    { id: 'a', type: 'question_batch', purpose: 'modeling_brief', title: 't', location: 'assistant' },
  ]), []);
});

test('没有标题时退回 purpose，避免卡片上出现空行', () => {
  const [item] = blockingPendingInteractions([
    interaction({ payload: { purpose: 'script_contract' } }),
  ], 'rev');
  assert.equal(item.title, 'script_contract');
});

// ---- 前端渲染侧 ----

test('带 nextActions 的 409 渲染成可操作卡片并展示明细', () => {
  const message = completionBlockerMessage({
    message: '还有 2 项确认知识缺少可追溯证据',
    data: {
      code: 'KNOWLEDGE_EVIDENCE_REQUIRED',
      problems: ['object:order 缺少资料证据', 'operation:plan 缺少流程或访谈证据'],
      nextActions: [{ id: 'open_package_file', label: '查看对象文件', style: 'primary', path: 'objects/index.md' }],
    },
  });
  assert.ok(message);
  assert.equal(message.blocks[0].title, '有确认知识缺少可追溯证据');
  assert.match(message.blocks[1].text, /object:order 缺少资料证据/);
  assert.equal(message.blocks[2].type, 'next_actions');
  assert.deepEqual(message.blocks[2].actions.map((a: any) => a.id), ['open_package_file']);
});

test('明细过长时截断并说明剩余数量', () => {
  const problems = Array.from({ length: 12 }, (_, index) => `object:o${index} 缺少资料证据`);
  const message = completionBlockerMessage({
    message: 'x', data: { code: 'KNOWLEDGE_EVIDENCE_REQUIRED', problems, nextActions: [{ id: 'a', label: 'b', style: 'primary' }] },
  });
  assert.match(message!.blocks[1].text, /还有 4 项…/);
});

test('待确认事项列出所在位置', () => {
  const message = completionBlockerMessage({
    message: '还有 1 个待确认事项未处理',
    data: {
      code: 'PENDING_INTERACTIONS',
      pendingInteractions: [{ id: 'sc', title: '场景复核', location: 'scenario' }],
      nextActions: [{ id: 'open_scenario_review', label: '去场景推演处理', style: 'primary' }],
    },
  });
  assert.match(message!.blocks[1].text, /场景复核（在「场景推演」页签处理）/);
});

test('没有 nextActions 的错误不生成卡片，仍走原有文本提示', () => {
  assert.equal(completionBlockerMessage({ message: '网络错误' }), null);
  assert.equal(completionBlockerMessage({ message: 'x', data: { code: 'PROJECT_BUSY' } }), null);
});
