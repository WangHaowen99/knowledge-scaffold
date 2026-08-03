import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import test from 'node:test';

const frontendRequire = createRequire(join(process.cwd(), 'frontend/package.json'));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const { default: AiConstructionMessageCard } = await import(
  '../frontend/src/AiConstructionMessageCard.jsx'
);

test('结构化建模消息按状态、说明和下一步动作分别展示', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'message-1',
      role: 'assistant',
      blocks: [
        {
          type: 'status', title: 'AI 辅助构建已就绪', summary: '当前处于资料就绪',
          progress: 32, eta: { minMinutes: 8, maxMinutes: 18, confidence: 'low' },
        },
        { type: 'text', text: '我会先分析资料，再提出访谈问题。' },
        {
          type: 'next_actions', title: '下一步', actions: [
            { id: 'upload_materials', label: '上传建模资料', style: 'primary' },
            { id: 'explain_goal', label: '说明建模目标', style: 'secondary' },
          ],
        },
      ],
    },
  }));

  assert.match(html, /AI 辅助构建已就绪/);
  assert.match(html, /当前处于资料就绪/);
  assert.match(html, /32%/);
  assert.match(html, /预计 8–18 分钟/);
  assert.match(html, /我会先分析资料/);
  assert.match(html, /上传建模资料/);
  assert.match(html, /说明建模目标/);
  assert.match(html, /ac-message-status/);
  assert.match(html, /ac-message-actions/);
  assert.match(html, /data-action="upload_materials"/);
  assert.match(html, /data-action="explain_goal"/);
});

test('结构化对话消息保留用户和建模助手的角色样式类', () => {
  const userHtml = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'user-message', role: 'user',
      blocks: [{ type: 'text', text: '请补充审批流程' }],
    },
  }));
  const assistantHtml = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'assistant-message', role: 'assistant',
      blocks: [{ type: 'text', text: '我会补充审批流程。' }],
    },
  }));

  assert.match(userHtml, /class="ac-message-card user"/);
  assert.match(assistantHtml, /class="ac-message-card assistant"/);
});

test('历史消息保留动作记录但不允许再次触发', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'historical-action', role: 'assistant', blocks: [{
        type: 'next_actions', title: '下一步', actions: [
          { id: 'analyze_materials', label: '开始分析资料', style: 'primary' },
        ],
      }],
    },
    actionsEnabled: false,
    onAction: () => {},
  }));

  assert.match(html, /历史步骤/);
  assert.match(html, /data-action="analyze_materials"/);
  assert.match(html, /disabled=""/);
});

test('运行中的资料分析只禁用分析动作并保留继续上传', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'material-actions', role: 'assistant', blocks: [{
        type: 'next_actions', title: '下一步', actions: [
          { id: 'upload_materials', label: '继续上传资料', style: 'secondary' },
          { id: 'analyze_materials', label: '开始分析资料', style: 'primary' },
        ],
      }],
    },
    disabledActionIds: ['analyze_materials'],
    onAction: () => {},
  }));

  assert.doesNotMatch(html, /data-action="upload_materials"[^>]*disabled/u);
  assert.match(html, /data-action="analyze_materials"[^>]*disabled/u);
});

test('助手文字块按 Markdown 渲染且原始 HTML 不会直接注入', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'markdown-message', role: 'assistant', blocks: [{
        type: 'text',
        text: '## 领域概览\n\n- **步骤一**\n- `步骤二`\n\n<script>alert(1)</script>',
      }],
    },
  }));

  assert.match(html, /<h2>领域概览<\/h2>/);
  assert.match(html, /<ul>/);
  assert.match(html, /<strong>步骤一<\/strong>/);
  assert.match(html, /<code>步骤二<\/code>/);
  assert.doesNotMatch(html, /<script>/);
});

test('未知消息块降级为普通提示且不渲染注入 HTML', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'message-2', role: 'assistant',
      blocks: [{ type: 'future_widget', html: '<script>alert(1)</script>' }],
    },
  }));

  assert.match(html, /暂不支持的消息内容/);
  assert.doesNotMatch(html, /<script>/);
});

test('历史候选复核消息隐藏内部术语并告诉专家下一步怎么做', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'legacy-candidate-review', role: 'assistant', blocks: [
        {
          type: 'status', title: '正在准备专家复核',
          summary: '自动修复仍有语义争议，正在准备专家复核', progress: 88,
        },
        {
          type: 'text',
          text: '候选修订的阻断错误从 1 个变为 1 个。候选修订引入了新的必须修复的错误，需要专家判断。',
        },
      ],
    },
  }));

  assert.match(html, /请确认这份修改/);
  assert.match(html, /需要你确认的修改/);
  assert.match(html, /确认使用、告诉助手怎么改，或者不使用/);
  assert.doesNotMatch(html, /语义争议|候选修订|阻断错误|专家复核/);
});

test('证据、校验和动态任务计划均以专用卡片展示', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message: {
    id: 'message-rich', role: 'assistant', blocks: [
      { type: 'evidence', title: '已找到的建模依据', items: [{ source: '业务规范.md', locator: '第 2 章', summary: '定义申请和复核流程' }] },
      { type: 'validation', title: '知识结构校验', scope: 'milestone', counts: { errors: 1, warnings: 2, hints: 3 },
        findings: [{ severity: 'error', title: '引用断裂', summary: '操作引用的规则不存在', target: 'operations/review.md' }] },
      { type: 'task_plan', title: '当前建模计划', note: '新增冲突澄清，预计增加 3–5 分钟', tasks: [
        { id: 'task_interview', title: '确认业务流程', status: 'running', required: true },
        { id: 'task_conflict', title: '澄清资料冲突', status: 'pending', required: false },
      ] },
    ],
  } }));
  assert.match(html, /ac-message-evidence/);
  assert.match(html, /ac-message-validation/);
  assert.match(html, /ac-message-task-plan/);
  assert.match(html, /错误 1/);
  assert.match(html, /进行中/);
});

test('证据位置和校验目标提供可定位到知识包文件的动作', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message: {
    id: 'message-open-targets', role: 'assistant', blocks: [
      { type: 'evidence', title: '建模依据', items: [{
        source: '已确认流程', locator: '流程节点 A3', path: 'diagrams/order-review.mmd', summary: '定义复核分支',
      }] },
      { type: 'validation', title: '增量校验', scope: 'incremental', counts: { errors: 1, warnings: 0, hints: 0 },
        findings: [{ severity: 'error', title: '引用断裂', summary: '引用的规则不存在', target: 'operations/review.md' }] },
    ],
  }, onAction: () => {} }));

  assert.match(html, /data-action="open_package_file"/);
  assert.match(html, /查看依据/);
  assert.match(html, /定位问题/);
  assert.match(html, /diagrams\/order-review\.mmd/);
  assert.match(html, /operations\/review\.md/);
});

test('建模调整方案区分资源类型、动作和当前决策状态', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message: {
    id: 'message-plan', role: 'assistant', blocks: [{
      type: 'change_plan', interactionId: 'interaction_1', title: '确认建模调整',
      summary: '补充异常处理路径', status: 'pending',
      changes: [{ id: 'change_1', targetKind: 'operation', targetKey: 'review_request', action: 'modify', description: '补充退回分支' }],
      risks: ['下游规则需重新校验'],
    }],
  } }));

  assert.match(html, /ac-message-change-plan/);
  assert.match(html, /待确认/);
  assert.match(html, /修改/);
  assert.match(html, /操作 review_request/);
  assert.match(html, /下游规则需重新校验/);
});

/**
 * 任务计划改为只读：暂缓/跳过/重做/恢复/前移/后移/拆分 这些任务图微操
 * 在建模对话里没有实际用途，且出现在必选任务上语义可疑。
 */
test('任务计划只展示进度，不再提供任务图微操作', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message: {
    id: 'message-task-actions', role: 'assistant', blocks: [{
      type: 'task_plan', title: '当前任务', tasks: [
        { id: 'optional', title: '补充说明', status: 'pending', required: false },
        { id: 'done', title: '确认目标', status: 'completed', required: true },
        { id: 'stale', title: '复核流程', status: 'stale', required: true },
      ],
    }],
  }, onAction: () => {} }));

  assert.match(html, /补充说明/);
  assert.match(html, /确认目标/);
  for (const label of ['暂缓', '跳过', '重做', '恢复', '前移', '后移', '拆分', '合并所选']) {
    assert.doesNotMatch(html, new RegExp(label), `不应再出现「${label}」`);
  }
  assert.doesNotMatch(html, /type="checkbox"/);
});

test('对话流里不再重复渲染任务计划，概览只留在助手栏顶部', () => {
  const message = {
    id: 'message-hidden-plan', role: 'assistant', blocks: [
      { type: 'text', text: '正在推进当前任务' },
      { type: 'task_plan', title: '当前任务', tasks: [{ id: 't', title: '确认目标', status: 'pending', required: true }] },
    ],
  };
  const inline = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message, hideTaskPlan: true }));
  assert.match(inline, /正在推进当前任务/);
  assert.doesNotMatch(inline, /ac-message-task-plan/);

  const popover = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message }));
  assert.match(popover, /ac-message-task-plan/);
});

test('动态任务卡不再展示已被当前完成任务替代的历史重做项', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, { message: {
    id: 'message-completion-replacement', role: 'assistant', blocks: [{
      type: 'task_plan', title: '当前动态任务', tasks: [
        { id: 'old', kind: 'completion_review', milestoneId: 'construction_completed', title: '检查 AI 辅助构建成果', status: 'stale', required: true },
        { id: 'current', kind: 'completion_review', milestoneId: 'construction_completed', title: '检查 AI 辅助构建成果', status: 'completed', required: true },
      ],
    }],
  } }));

  assert.equal((html.match(/检查 AI 辅助构建成果/g) ?? []).length, 1);
  assert.match(html, /已完成/);
  assert.doesNotMatch(html, /需重做|需要重做|已过时/);
});


