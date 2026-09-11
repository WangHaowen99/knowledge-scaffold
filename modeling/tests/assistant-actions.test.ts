import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { ASSISTANT_ACTION_KINDS, assistantActionKind } from '../src/ai-construction/assistant-actions.js';
import {
  ASSISTANT_ACTION_KINDS as FRONTEND_KINDS,
  NAVIGATION_TARGETS,
  canonicalAssistantAction,
  isReadOnlyAction,
} from '../frontend/src/assistantActions.js';
import { packageRepairAvailability } from '../src/ai-construction/repair-availability.js';

const backendSource = ['src/ai-construction/orchestrator.ts', 'src/authoring/routes.ts', 'src/routes.ts']
  .map((path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8'))
  .join('\n');

/**
 * 这条是防「点了没反应」的关键：历史上 open_optimization 前端写好了处理、
 * 后端没人发出，直到用户点击才发现。目录对不上现在会在测试里失败。
 */
test('前后端动作目录逐项一致', () => {
  assert.deepEqual(Object.keys(FRONTEND_KINDS).sort(), Object.keys(ASSISTANT_ACTION_KINDS).sort());
  for (const [id, kind] of Object.entries(ASSISTANT_ACTION_KINDS)) {
    assert.equal(FRONTEND_KINDS[id], kind, `${id} 的 kind 前后端不一致`);
  }
});

test('后端代码里出现的动作 id 都已登记', () => {
  const emitted = new Set(
    [...backendSource.matchAll(/id: '([a-z_]+)', label:[^\n]*(?:style:)/g)].map((match) => match[1]),
  );
  assert.ok(emitted.size > 10, `应扫描到足够多的动作，实际 ${emitted.size}`);
  for (const id of emitted) {
    assert.ok(ASSISTANT_ACTION_KINDS[id], `动作 ${id} 未在目录登记`);
  }
});

test('每个导航动作都有明确的目标', () => {
  const navigateIds = Object.entries(ASSISTANT_ACTION_KINDS)
    .filter(([, kind]) => kind === 'navigate').map(([id]) => id);
  for (const id of navigateIds) {
    assert.ok(NAVIGATION_TARGETS[id], `导航动作 ${id} 缺少目标声明`);
  }
  // 反向：目标表里不应有非导航动作
  for (const id of Object.keys(NAVIGATION_TARGETS)) {
    assert.equal(ASSISTANT_ACTION_KINDS[id], 'navigate', `${id} 不是导航动作却有目标`);
  }
});

test('原先只往输入框预填的三个动作现在是真导航', () => {
  for (const id of ['review_materials', 'review_package', 'view_validation']) {
    assert.equal(assistantActionKind(id), 'navigate');
    assert.ok(NAVIGATION_TARGETS[id], `${id} 应有导航目标`);
  }
  assert.deepEqual(NAVIGATION_TARGETS.view_validation, { openPackageCheck: true });
  assert.deepEqual(NAVIGATION_TARGETS.review_materials, { step: 'ingest' });
});

test('只读判定跟随 kind，流程图有未保存修改时导航仍可执行', () => {
  assert.equal(isReadOnlyAction({ id: 'view_validation' }), true);
  assert.equal(isReadOnlyAction({ id: 'confirm_goal' }), false);
  assert.equal(isReadOnlyAction({ id: 'defer_validation_errors' }), false);
  // 后端下发的 kind 优先于本地表，便于后端先行发布新动作
  assert.equal(isReadOnlyAction({ id: 'unknown_future', kind: 'navigate' }), true);
});

test('历史额度卡的刷新动作恢复为真正的知识生成重试', () => {
  assert.deepEqual(canonicalAssistantAction({
    id: 'refresh_task_graph', label: '额度恢复后重试', style: 'secondary',
  }), {
    id: 'generate_knowledge_structure', label: '额度恢复后重试',
    style: 'secondary', kind: 'command',
  });
  assert.equal(canonicalAssistantAction({
    id: 'refresh_task_graph', label: '刷新查看当前进度',
  }).id, 'refresh_task_graph');
});

// ---- 修复按钮可用性 ----

const graph = (overrides: any = {}) => ({
  milestones: [{ id: 'knowledge_confirmed', status: 'confirmed' }],
  tasks: [],
  ...overrides,
}) as any;

test('无必须修复的错误时不提供自动修复', () => {
  assert.deepEqual(
    packageRepairAvailability({ errorCount: 0, hasActiveDraft: false, graph: graph() }),
    { available: false, reason: '当前内容没有必须修复的错误，无需修复' },
  );
});

test('存在人工编辑草稿时说明原因而不是静默禁用', () => {
  const result = packageRepairAvailability({ errorCount: 3, hasActiveDraft: true, graph: graph() });
  assert.equal(result.available, false);
  assert.match(String(result.reason), /人工编辑草稿/);
});

test('有活动阶段时可修复', () => {
  const result = packageRepairAvailability({
    errorCount: 1, hasActiveDraft: false,
    graph: graph({ milestones: [{ id: 'knowledge_confirmed', status: 'active' }] }),
  });
  assert.deepEqual(result, { available: true });
});

test('无活动阶段但有已完成校验任务时仍可修复（重开该阶段）', () => {
  const result = packageRepairAvailability({
    errorCount: 1, hasActiveDraft: false,
    graph: graph({ tasks: [{ kind: 'package_validation', status: 'completed' }] }),
  });
  assert.deepEqual(result, { available: true });
});

test('既无活动阶段也无可重开的校验任务时说明原因', () => {
  const result = packageRepairAvailability({ errorCount: 1, hasActiveDraft: false, graph: graph() });
  assert.equal(result.available, false);
  assert.match(String(result.reason), /没有可承载修复的建模阶段/);
});
