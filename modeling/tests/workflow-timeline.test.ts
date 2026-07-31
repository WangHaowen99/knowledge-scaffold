import { createRequire } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import WorkflowTimeline, {
  canSelectWorkflowStep,
  groupWorkflowSteps,
  selectWorkflowStep,
  workflowRequirementLabel,
  workflowStatusLabel,
} from '../frontend/src/WorkflowTimeline.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

const definitions = [
  ['project_setup', '项目创建', 'prepare'],
  ['ingest', '建模资料上传', 'prepare'],
  ['ai_construction', 'AI辅助构建', 'model'],
  ['scenario', '场景推演', 'verify'],
  ['data_source', '数据源绑定', 'verify'],
  ['evaluate', '效果评测', 'verify'],
  ['diagnose', '失败诊断', 'improve'],
  ['optimize', '优化更新', 'improve'],
  ['release', '发布交付', 'deliver'],
] as const;

const statuses = [
  'not_started', 'ready', 'running', 'waiting_answer', 'waiting_review',
  'completed', 'skipped', 'stale', 'failed',
];

function steps() {
  let visibleIndex = 0;
  return definitions.map(([id, label, group]) => {
    const linear = !['diagnose', 'optimize'].includes(id);
    return {
      id,
      label,
      group,
      linear,
      focus: id,
      status: linear ? statuses[visibleIndex++ % statuses.length] : 'completed',
      requirement: id === 'data_source' || id === 'diagnose' ? 'conditional'
        : id === 'optimize' ? 'optional'
        : id === 'release' ? 'release'
          : 'required',
    };
  });
}

const render = (props: Record<string, unknown>) => renderToStaticMarkup(
  React.createElement(WorkflowTimeline, props),
);

test('左栏按唯一九步数据渲染线性阶段', () => {
  const html = render({ steps: steps(), activeId: 'ai_construction' });

  assert.equal((html.match(/data-step-id=/g) || []).length, 7);
  for (const group of ['准备', '建模', '验证', '交付']) {
    assert.match(html, new RegExp(`>${group}<`));
  }
  for (const [id, label] of definitions) {
    if (['diagnose', 'optimize'].includes(id)) assert.ok(!html.includes(label));
    else assert.ok(html.includes(label));
  }
  assert.match(html, /data-step-id="ai_construction"[^>]*active/);
});

test('服务端 name 字段可直接显示', () => {
  const html = render({
    steps: [{
      id: 'ai_construction', name: 'AI辅助构建', group: 'model',
      focus: 'ai_construction', status: 'ready',
    }],
  });
  assert.match(html, />AI辅助构建</);
  assert.match(html, /data-tour="node-ai_construction"/);
});

test('状态使用统一文案且失败和过时态面向用户显示为待确认', () => {
  const html = render({
    steps: steps().map((step) => (step.id === 'release' ? { ...step, status: 'failed' } : step)),
  });

  for (const label of ['未开始', '可开始', '进行中', '待回答', '待审阅', '完成', '待确认']) {
    assert.match(html, new RegExp(`>${label}<`));
  }
  assert.doesNotMatch(html, />失败</);
  assert.doesNotMatch(html, />已过时</);
  for (const label of ['条件', '可选', '发布']) assert.doesNotMatch(html, new RegExp(`>${label}<`));
  assert.doesNotMatch(html, />必选</);
  assert.equal(workflowStatusLabel('waiting_user'), '待回答');
  assert.equal(workflowStatusLabel('failed'), '待确认');
  assert.equal(workflowStatusLabel('stale'), '待确认');
  assert.equal(workflowRequirementLabel('required'), '');
});

test('未开始步骤禁用，其他状态可以选择', () => {
  const html = render({ steps: steps() });
  assert.match(html, /data-step-id="project_setup"[^>]*disabled=""/);
  assert.doesNotMatch(html, /data-step-id="ingest"[^>]*disabled=""/);

  assert.equal(canSelectWorkflowStep({ status: 'not_started' }), false);
  for (const status of statuses.slice(1)) assert.equal(canSelectWorkflowStep({ status }), true);
});

test('选择步骤时原样回传，禁用项不触发', () => {
  const selected: unknown[] = [];
  const ready = { id: 'ai_construction', status: 'ready', focus: 'ai_construction', group: 'model' };
  const locked = { id: 'release', status: 'not_started', focus: 'release', group: 'deliver' };

  assert.equal(selectWorkflowStep(ready, (step) => selected.push(step)), true);
  assert.equal(selectWorkflowStep(locked, (step) => selected.push(step)), false);
  assert.deepEqual(selected, [ready]);
  assert.equal(selected[0], ready);
});

test('分组保持固定顺序并在缺少 group 时按九步位置兜底', () => {
  const input = steps().map((step) => ({ ...step, group: undefined }));
  const grouped = groupWorkflowSteps(input);
  assert.deepEqual(Object.keys(grouped), ['prepare', 'model', 'verify', 'improve', 'deliver']);
  assert.deepEqual(Object.values(grouped).map((items) => items.length), [2, 1, 3, 0, 1]);
});
