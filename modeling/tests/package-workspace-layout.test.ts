import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import PackageChat, {
  interactionBusyState,
  interactionResponseChangedPackage,
  repairRuntimeMessageId,
  shouldOpenWarningReview,
  taskRuntimeMessageId,
} from '../frontend/src/PackageChat.jsx';
import PendingInteraction, { toggleQuestionSelection } from '../frontend/src/PendingInteraction.jsx';
import PendingInteractionQueue from '../frontend/src/PendingInteractionQueue.jsx';
import PaneResizeHandle from '../frontend/src/PaneResizeHandle.jsx';
import { resizePaneWidth } from '../frontend/src/paneResize.js';
import {
  constructionInteractionQueue,
  isConstructionInteraction,
  isCurrentConstructionInteraction,
  selectCurrentConstructionInteraction,
} from '../frontend/src/constructionInteraction.js';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

const packageWorkspaceSource = readFileSync(
  new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url),
  'utf8',
);
const aiConstructionWorkbenchSource = readFileSync(
  new URL('../frontend/src/AiConstructionWorkbench.jsx', import.meta.url),
  'utf8',
);
const reviewPanelSource = readFileSync(
  new URL('../frontend/src/M2Panels.jsx', import.meta.url),
  'utf8',
);
const packageChatSource = readFileSync(
  new URL('../frontend/src/PackageChat.jsx', import.meta.url),
  'utf8',
);
const pendingInteractionSource = readFileSync(
  new URL('../frontend/src/PendingInteraction.jsx', import.meta.url),
  'utf8',
);
const appSource = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
const candidateRevisionSource = readFileSync(
  new URL('../frontend/src/CandidateRevisionPanel.jsx', import.meta.url),
  'utf8',
);
const optimizationPanelSource = readFileSync(
  new URL('../frontend/src/M4Panel.jsx', import.meta.url),
  'utf8',
);

test('确认知识包修复方案后显示明确的修复进度而不是普通提交提示', () => {
  assert.equal(interactionBusyState('controlled_repair'), 'repair_knowledge');
  assert.equal(interactionBusyState('script_contract'), 'script_generation');
  assert.equal(interactionBusyState('script_generation'), 'script_apply');
  assert.equal(interactionBusyState('modeling_brief'), 'interaction');
});

test('受控修复方案应用期间建立统一 Job 并持续更新心跳', () => {
  const controllerSource = readFileSync(
    new URL('../src/authoring/controlled-repair-controller.ts', import.meta.url),
    'utf8',
  );
  assert.match(controllerSource, /purpose: 'controlled_repair_apply'/);
  assert.match(controllerSource, /startJobHeartbeat\(/);
  assert.match(controllerSource, /currentStep: '按确认方案修改知识文件'/);
  assert.match(controllerSource, /currentStep: '重新执行知识包格式、引用、来源映射和规则检查'/);
  assert.match(controllerSource, /CONTROLLED_REPAIR_APPLY_FAILED/);
});

test('脚本候选允许多选具体流程，但暂不生成与具体流程互斥', () => {
  assert.deepEqual(toggleQuestionSelection(['CalculateScore'], 'AllocateVolume', true), [
    'CalculateScore', 'AllocateVolume',
  ]);
  assert.deepEqual(toggleQuestionSelection(['CalculateScore', 'AllocateVolume'], '__none__', true), [
    '__none__',
  ]);
  assert.deepEqual(toggleQuestionSelection(['__none__'], 'CalculateScore', true), ['CalculateScore']);
  assert.deepEqual(toggleQuestionSelection(['__none__'], '__none__', true), []);
});

test('知识包建模助手支持继续加载更早的会话消息', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-message-history',
    hasOlderMessages: true,
    structuredMessages: [{
      id: 'message-200', seq: 200, role: 'assistant', blocks: [{ type: 'text', text: '最近消息' }],
    }],
  }));
  assert.match(html, /加载更早消息/);
  assert.match(packageWorkspaceSource, /listAiConstructionMessages/);
  assert.match(packageWorkspaceSource, /mergeAiConstructionSession/);
  assert.match(packageWorkspaceSource, /prependAiConstructionMessagePage/);
});

test('已确认确定性能力后旧消息不再展示重复确认按钮', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'confirmed-deterministic',
    aiConstruction: {
      status: 'active', revision: 171,
      milestones: [{ id: 'deterministic_confirmed', status: 'confirmed' }],
      tasks: [], progress: {},
    },
    structuredMessages: [{
      id: 'deterministic-ready', role: 'assistant', createdAt: '2026-07-26T11:45:33.000Z',
      blocks: [{ type: 'next_actions', actions: [
        { id: 'confirm_deterministic', label: '确认确定性能力', style: 'primary' },
        { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
      ] }],
    }],
  }));
  assert.doesNotMatch(html, /data-action="confirm_deterministic"/);
  assert.match(html, /data-action="review_knowledge_files"/);
});

const packageCheckSource = readFileSync(
  new URL('../frontend/src/PackageCheck.jsx', import.meta.url),
  'utf8',
);
const stylesSource = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');
const runObservabilitySource = readFileSync(
  new URL('../frontend/src/RunObservabilityPanel.jsx', import.meta.url),
  'utf8',
);
const workflowM2Source = readFileSync(new URL('../src/workflow/m2.ts', import.meta.url), 'utf8');

test('统一 Job 运行卡展示耗时、剩余时间和可恢复出口', () => {
  assert.match(packageChatSource, /activeRunObservability/);
  assert.match(runObservabilitySource, /预计剩余/);
  assert.match(runObservabilitySource, /任务长时间没有更新/);
  assert.doesNotMatch(runObservabilitySource, /Job \{job\.jobId\}|最后心跳/);
  assert.match(stylesSource, /\.pchat-job-observability\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(stylesSource, /\.pchat-job-observability > span[\s\S]*overflow-wrap:\s*anywhere/);
});

test('知识包修复运行信息合并到语义方案卡且不再显示下方独立运行区', () => {
  const messages = [{
    id: 'repair-status', role: 'assistant', createdAt: '2026-08-19T08:00:00.000Z',
    blocks: [{
      type: 'status', title: '知识包语义修复方案等待确认',
      summary: '确认方案前不会修改当前知识包', milestoneId: 'knowledge_confirmed',
      progress: 61,
    }, { type: 'text', text: '无证据知识 · operation:RecommendProduct' }],
  }];
  assert.equal(repairRuntimeMessageId(messages), 'repair-status');
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-repair-runtime',
    structuredMessages: messages,
    authoring: { activeRuns: [{
      id: 'ar-repair', intent: '应用知识包修复方案', status: 'running',
      createdAt: '2026-08-19T08:00:00.000Z', updatedAt: '2026-08-19T08:01:00.000Z',
      state: {
        purpose: 'controlled_repair_apply', jobId: 'ar-repair', phase: '方案应用',
        currentStep: '按确认方案修改知识文件', progress: 10,
        lastHeartbeatAt: new Date().toISOString(),
        eta: { minMinutes: 5, maxMinutes: 30, confidence: 'low' },
      },
    }] },
  }));
  assert.match(html, /has-job-runtime/);
  assert.match(html, /正在应用知识包修复方案/);
  assert.doesNotMatch(html, /Job ar-repair/);
  assert.match(html, /任务 10%/);
  assert.match(html, /已运行/);
  assert.doesNotMatch(html, /最后心跳/);
  assert.match(html, />取消</);
  assert.doesNotMatch(html, /pchat-standalone-run/);
});

test('建模总进度展示在助手头部，运行卡只展示当前任务进度', () => {
  const messages = [{
    id: 'task-progress', key: 'progress.task.task-knowledge', role: 'assistant', taskId: 'task-knowledge',
    blocks: [{ type: 'status', title: '正在生成第 1/2 批规则和指标', progress: 33 }],
  }];
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-progress-layout',
    structuredMessages: messages,
    aiConstruction: {
      progress: {
        percent: 50, currentMilestoneId: 'knowledge_confirmed', currentTaskId: 'task-knowledge',
        totalMilestones: 6,
      },
      tasks: [{
        id: 'task-knowledge', milestoneId: 'knowledge_confirmed', title: '生成知识结构', status: 'running',
      }],
    },
    authoring: { activeRuns: [{
      id: 'run-knowledge', intent: '生成知识结构', status: 'running',
      createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
      state: {
        taskId: 'task-knowledge', currentStep: '正在生成第 1/2 批规则和指标', progress: 33,
        lastHeartbeatAt: new Date().toISOString(), eta: { minMinutes: 4, maxMinutes: 8, confidence: 'medium' },
      },
    }] },
  }));

  assert.match(html, /pchat-overall-progress/);
  assert.match(html, /AI 辅助建模总进度/);
  assert.match(html, /阶段 4\/6 · 50%/);
  assert.match(html, /任务 33%/);
  assert.match(html, /已运行/);
  assert.match(html, /预计剩余：4–8 分钟/);
  assert.doesNotMatch(html, /pchat-standalone-run|Job run-knowledge|最后心跳/);
});

test('重试时把统一 Job 绑定到最新运行的进度卡', () => {
  const messages = [
    {
      id: 'old-progress', key: 'progress.task.task-knowledge.run.run-old',
      taskId: 'task-knowledge', blocks: [{ type: 'status', title: '旧运行进度' }],
    },
    {
      id: 'new-start', key: 'knowledge.start.run-new',
      taskId: 'task-knowledge', blocks: [{ type: 'status', title: '重新开始知识生成' }],
    },
    {
      id: 'new-progress', key: 'progress.task.task-knowledge.run.run-new',
      taskId: 'task-knowledge', blocks: [{ type: 'status', title: '当前运行进度' }],
    },
  ];
  assert.equal(taskRuntimeMessageId(messages, 'task-knowledge'), 'new-progress');
});

test('知识结构生成在模型批次内部持续维护统一 Job 心跳', () => {
  assert.match(workflowM2Source, /startJobHeartbeat\(/);
  assert.match(workflowM2Source, /finally\s*\{\s*stopJobHeartbeat\(\)/);
});

test('建模完成被可暂缓的提醒拦截时打开影响复核区', () => {
  assert.equal(shouldOpenWarningReview({ data: { code: 'PACKAGE_WARNINGS_UNREVIEWED' } }), true);
  assert.equal(shouldOpenWarningReview({ data: { code: 'PACKAGE_CHECK_FAILED' } }), false);
  assert.match(packageWorkspaceSource, /PackageCheck/);
  assert.match(packageChatSource, /mw-package-warning-review/);
});

test('嵌入发布页在会话加载前不会解引用空会话', () => {
  assert.match(packageWorkspaceSource, /release=\{session\?\.release\}/);
});

test('拖拽宽度遵守方向、最小值和最大值', () => {
  assert.equal(resizePaneWidth(280, 70, 1, 220, 420), 350);
  assert.equal(resizePaneWidth(280, 300, 1, 220, 420), 420);
  assert.equal(resizePaneWidth(360, 100, -1, 300, 520), 300);
  assert.equal(resizePaneWidth(360, -300, -1, 300, 520), 520);
});

test('拖拽分隔线暴露可访问宽度范围', () => {
  const html = renderToStaticMarkup(React.createElement(PaneResizeHandle, {
    label: '调整知识包结构栏宽度',
    value: 280,
    min: 220,
    max: 420,
    onChange: () => {},
  }));
  assert.match(html, /role="separator"/);
  assert.match(html, /aria-valuemin="220"/);
  assert.match(html, /aria-valuemax="420"/);
  assert.match(html, /aria-valuenow="280"/);
});

test('右栏使用知识包建模助手定位并提供固定输入区', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, { projectId: 'project-layout' }));
  assert.match(html, /知识包建模助手/);
  assert.match(html, /pchat-head/);
  assert.match(html, /pchat-input/);
  assert.match(html, /咨询知识包/);
});

test('知识包建模助手优先展示服务端结构化建模消息', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-layout',
    structuredMessages: [{
      id: 'welcome', role: 'assistant', blocks: [
        { type: 'status', title: 'AI 辅助构建已就绪', progress: 0 },
        { type: 'text', text: '知识包草稿目录已经创建。' },
      ],
    }],
  }));
  assert.match(html, /AI 辅助构建已就绪/);
  assert.match(html, /知识包草稿目录已经创建/);
  assert.doesNotMatch(html, /你好，我是知识包建模助手/);
});

test('用户消息与建模助手消息使用不同颜色', () => {
  assert.match(
    stylesSource,
    /\.pchat-body > \.ac-message-card\.user\s*\{[^}]*background:\s*var\(--grad-p\)/s,
  );
  assert.match(
    stylesSource,
    /\.pchat-body > \.ac-message-card\.assistant\s*\{[^}]*background:\s*#f4f6f9/s,
  );
  assert.match(
    stylesSource,
    /\.ac-message-card\.user \.ac-message-time\s*\{[^}]*color:\s*rgba\(255, 255, 255, \.74\)/s,
  );
});

test('自由对话由服务端持久化而不是保存在浏览器标签页', () => {
  assert.doesNotMatch(packageChatSource, /sessionStorage/);
  assert.doesNotMatch(packageChatSource, /historyForApi/);
  assert.match(packageChatSource, /requestId/);
  assert.match(packageChatSource, /await refreshConstruction\(\)/);
  assert.match(packageWorkspaceSource, /constructionInteractionQueue\(/);
  assert.match(packageChatSource, /<PendingInteractionQueue/);
});

test('知识包建模助手内置建模资料多文件上传入口', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-upload',
    structuredMessages: [{
      id: 'upload-action', role: 'assistant', blocks: [{
        type: 'next_actions', actions: [
          { id: 'upload_materials', label: '上传建模资料', style: 'primary' },
        ],
      }],
    }],
  }));

  assert.match(html, /type="file"/);
  assert.match(html, /multiple=""/);
  assert.match(html, /aria-label="上传建模资料"/);
  assert.match(html, /\.xlsx,\.xls,\.csv,\.md,\.markdown,\.txt,\.docx,\.doc,\.pdf/);
});

test('长任务进度以事件流为主，轮询只作连接中断的兜底', () => {
  assert.match(packageChatSource, /\['analyze', 'generate_knowledge'/);
  assert.match(packageChatSource, /LONG_TASK_FALLBACK_REFRESH_MS = 15_000/);
  assert.match(packageChatSource, /window\.setInterval\([\s\S]*?LONG_TASK_FALLBACK_REFRESH_MS\)/);
  assert.match(packageChatSource, /return \(\) => window\.clearInterval\(timer\)/);
  assert.doesNotMatch(packageChatSource, /\? 4_000 : 2_000/);
});

test('助手忙碌态以服务端运行记录为准，刷新页面后可恢复', () => {
  assert.match(packageChatSource, /isPackageChatRunning\(authoring\)/);
  assert.match(packageChatSource, /const busy = localBusy \|\| \(chatRunning \? 'plan' : ''\)/);
});

test('知识包建模助手在对话流中展示可恢复的建模目标问题', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-goal',
    pendingInteraction: {
      id: 'interaction-goal',
      type: 'question_batch',
      status: 'pending',
      payload: {
        purpose: 'modeling_brief',
        title: '请说明知识包的建模目标',
        questions: [
          { id: 'goal_text', kind: 'text', q: '这个知识包主要要解决什么问题？', required: true },
        ],
      },
    },
  }));

  assert.match(html, /请说明知识包的建模目标/);
  assert.match(html, /这个知识包主要要解决什么问题/);
  assert.match(html, /提交答案/);
  assert.match(html, /ac-message-interaction/);
  assert.match(html, /rows="5"/);
  assert.match(html, /请描述要解决的问题、使用者、典型场景和期望结果/);
});

test('知识包与脚本修改方案都支持填写意见后原位调整', () => {
  const packagePlan = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'package-plan', type: 'change_plan', status: 'pending',
      payload: {
        purpose: 'package_chat', summary: '新增跨境业务事件',
        changes: [{ id: 'change-1', targetKind: 'object', targetKey: 'cross_border_event', action: 'add', description: '新增事件对象' }],
      },
    },
    onSubmit: () => {},
  }));
  const scriptPlan = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'script-plan', type: 'change_plan', status: 'pending',
      payload: {
        purpose: 'script_generation', summary: '生成脚本',
        changes: [{ id: 'change-1', targetKind: 'script', targetKey: 'calculate', action: 'add', description: '生成计算脚本' }],
      },
    },
    onSubmit: () => {},
  }));

  assert.match(packagePlan, /调整方案/);
  assert.match(packagePlan, /调整方案时请填写修改意见/);
  assert.match(scriptPlan, /调整方案/);
  assert.match(scriptPlan, /调整方案时请填写修改意见/);
});

test('流程选择的其他输入明确表示补充说明而不是标题', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-process-other',
    pendingInteraction: {
      id: 'interaction-process-other',
      type: 'question_batch',
      status: 'pending',
      payload: {
        purpose: 'process_interview',
        questions: [{
          id: 'selected_processes', kind: 'multi', q: '选择核心业务流程', required: true,
          allowOther: true,
          otherLabel: '补充其他流程',
          otherPlaceholder: '请输入补充说明，下一步再确认流程名称和细节',
          defaultValue: ['__other__'],
          options: [{ value: '已识别流程', label: '已识别流程' }],
        }],
      },
    },
  }));

  assert.match(html, /补充其他流程/);
  assert.match(html, /请输入补充说明/);
  assert.doesNotMatch(html, /placeholder="请输入"/);
});

test('AI 构建中的问题卡不会被统一工作区过滤掉', () => {
  for (const purpose of ['script_decision', 'script_contract', 'controlled_repair_expert']) {
    assert.equal(isConstructionInteraction({
      type: 'question_batch',
      payload: { purpose },
    }), true);
  }
  assert.equal(isConstructionInteraction({
    type: 'scenario_review',
    payload: { purpose: 'scenario' },
  }), false);
});

test('旧修订的受控修复待办不会覆盖当前版本的继续修复动作', () => {
  assert.equal(isCurrentConstructionInteraction({
    type: 'change_plan', baseRevision: 'rev-current',
    payload: { purpose: 'controlled_repair' },
  }, 'rev-current'), true);
  assert.equal(isCurrentConstructionInteraction({
    type: 'change_plan', baseRevision: 'rev-old',
    payload: { purpose: 'controlled_repair' },
  }, 'rev-current'), false);
  assert.equal(isCurrentConstructionInteraction({
    type: 'question_batch', baseRevision: 'rev-old',
    payload: { purpose: 'modeling_brief' },
  }, 'rev-current'), true);
});

test('旧修订的告警放行待办刷新后不再重复展示', () => {
  assert.equal(isCurrentConstructionInteraction({
    type: 'question_batch', baseRevision: 'rev-old',
    payload: { purpose: 'check_finding_ignore' },
  }, 'rev-current'), false);
  assert.equal(isCurrentConstructionInteraction({
    type: 'question_batch', baseRevision: 'rev-current',
    payload: { purpose: 'check_finding_ignore' },
  }, 'rev-current'), true);
  const selected = selectCurrentConstructionInteraction([
    {
      id: 'ignore-old', type: 'question_batch', baseRevision: 'rev-old',
      payload: { purpose: 'check_finding_ignore' },
    },
    {
      id: 'goal-current', type: 'question_batch', baseRevision: 'rev-current',
      payload: { purpose: 'modeling_brief' },
    },
  ], 'rev-current');
  assert.equal(selected?.id, 'goal-current');
});

test('受控修复确认优先于更早创建的暂缓理由表单', () => {
  const selected = selectCurrentConstructionInteraction([
    {
      id: 'ignore-old', type: 'question_batch', baseRevision: 'rev-current',
      payload: { purpose: 'check_finding_ignore' },
    },
    {
      id: 'repair-current', type: 'change_plan', baseRevision: 'rev-current',
      payload: { purpose: 'controlled_repair' },
    },
  ], 'rev-current');
  assert.equal(selected?.id, 'repair-current');
});

test('待办队列展示全部当前事项并服从显式顺序', () => {
  const queue = constructionInteractionQueue([
    {
      id: 'expert-second', type: 'question_batch', baseRevision: 'rev-current',
      payload: { purpose: 'controlled_repair_expert', queue: { order: 2 } },
    },
    {
      id: 'scenario-first', type: 'scenario_review',
      payload: { purpose: 'scenario_review', queue: { order: 1 } },
    },
    {
      id: 'stale-repair', type: 'change_plan', baseRevision: 'rev-old',
      payload: { purpose: 'controlled_repair', queue: { order: 0 } },
    },
  ], 'rev-current');
  assert.deepEqual(queue.map((interaction) => interaction.id), [
    'scenario-first', 'expert-second',
  ]);
});

test('待办队列把当前项、依赖排队项和真实数量同时展示', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteractionQueue, {
    interactions: [{
      id: 'current', type: 'question_batch',
      payload: { purpose: 'controlled_repair_expert', title: '确认指标来源' },
    }, {
      id: 'queued', type: 'change_plan',
      payload: {
        purpose: 'controlled_repair', title: '确认修复方案',
        queue: { dependsOn: ['current'], independent: false },
      },
    }],
    activeInteractionId: 'current',
  }));
  assert.match(html, /待处理事项/);
  assert.match(html, /确认指标来源/);
  assert.match(html, /确认修复方案/);
  assert.match(html, /排队/);
  assert.match(html, /disabled=""/);
  assert.match(html, /共 2 项/);
});

test('升级前创建的受控修复自由输入待办按互斥建议恢复为选择卡片', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-legacy-repair',
    pendingInteraction: {
      id: 'interaction-legacy-repair',
      type: 'question_batch',
      status: 'pending',
      payload: {
        purpose: 'controlled_repair_expert',
        finding: {
          suggestion: '若阈值表示库存上限，则规则改为当前库存大于等于阈值时预警；若阈值表示库存下限，则字段描述改为低于阈值时预警。',
        },
        questions: [{
          id: 'expert_decision', kind: 'text', q: '请手工填写业务口径', required: true,
        }],
      },
    },
  }));

  assert.match(html, /type="radio"/);
  assert.match(html, /阈值表示库存上限/);
  assert.match(html, /阈值表示库存下限/);
  assert.doesNotMatch(html, /请手工填写业务口径/);
});

test('只有一个明确修复建议时优先展示采用建议选项', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-repair-recommendation',
    pendingInteraction: {
      id: 'interaction-repair-recommendation',
      type: 'question_batch',
      status: 'pending',
      payload: {
        purpose: 'controlled_repair_expert',
        finding: { suggestion: '将字段来源改为数据字典中的真实列，并保持字段 key 不变。' },
        questions: [{ id: 'expert_decision', kind: 'text', q: '请填写处理决定', required: true }],
      },
    },
  }));

  assert.match(html, /采用校验建议/);
  assert.match(html, /type="radio"/);
  assert.doesNotMatch(html, /请填写处理决定/);
});

test('专家业务建议不会覆盖修复目标选项', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'interaction-repair-target',
      type: 'question_batch',
      status: 'pending',
      payload: {
        purpose: 'controlled_repair_expert',
        finding: {
          userSuggestion: '请确认应使用哪个金额字段。',
          expertOptions: [{
            label: '改引用 hit_bid_amt',
            description: '使用招投标明细的中标金额字段',
            decision: 'step_8_field_fix:replace proj_hit_bid_tx_cap with hit_bid_amt',
          }],
        },
        questions: [
          {
            id: 'expert_decision',
            kind: 'text',
            q: '请给出业务决策',
            required: true,
          },
          {
            id: 'repair_target',
            kind: 'single',
            q: '请选择本次要修改哪一个资源',
            required: true,
            options: [
              { label: '营销线索事件', value: 'object:营销线索事件' },
              { label: '招投标线索事件', value: 'object:招投标线索事件' },
              { label: '金融需求识别与产品推荐', value: 'operation:金融需求识别与产品推荐' },
            ],
          },
        ],
      },
    },
    onSubmit: () => {},
  }));

  assert.match(html, /处理方式 1/);
  assert.match(html, /招投标线索事件/);
  assert.match(html, /金融需求识别与产品推荐/);
  assert.doesNotMatch(html, /step_8_field_fix:replace/);
});

test('知识生成批次失败时先展示具体校验问题再让专家决策', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'generation-gate',
      type: 'decision',
      status: 'pending',
      payload: {
        purpose: 'generation_gate_skip',
        prompt: '请选择重新生成或跳过该批次。',
        issues: ['指标 route_profit 引用了不存在的规则 route_mapping。'],
        options: [
          { value: 'retry', label: '重新生成' },
          { value: 'skip', label: '跳过该批继续' },
        ],
      },
    },
    onSubmit: () => {},
  }));

  assert.match(html, /未通过原因（1）/);
  assert.match(html, /指标 route_profit 引用了不存在的规则 route_mapping/);
  assert.match(html, /重新生成/);
  assert.match(html, /跳过该批继续/);
});

test('流程图待确认时中栏展示可编辑草稿且右栏提交已保存修订', () => {
  assert.match(packageWorkspaceSource, /processReview/);
  assert.match(packageWorkspaceSource, /<div className="rv-flowchart">\s*<FlowchartEditor/);
  assert.match(packageWorkspaceSource, /getFlowchartDraft/);
  assert.match(packageWorkspaceSource, /saveCurrentFlowchartDraft/);
  assert.match(packageWorkspaceSource, /addFlowchart/);
  assert.equal(isConstructionInteraction({
    type: 'question_batch', payload: { purpose: 'process_diagram_revision' },
  }), true);
  assert.match(packageWorkspaceSource, /pendingInteraction=\{constructionInteraction\}/);

  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'flowchart-review', type: 'flowchart_review', status: 'pending',
      payload: { purpose: 'process_review', title: '确认业务流程图' },
    },
    flowchartDraftState: {
      draftRevision: 4, diagramCount: 1, dirty: false, saveStatus: 'saved', warnings: [],
    },
    onSubmit: () => {},
  }));
  assert.match(html, /流程图草稿 v4 已保存/);
  assert.match(html, /提交全部已保存流程图/);
});

test('流程图确认返回知识包修订后刷新文件面板', () => {
  assert.equal(interactionResponseChangedPackage({ packageRevision: 'pkg-flowchart' }), true);
  assert.equal(interactionResponseChangedPackage({ revision: 'pkg-repair' }), true);
  assert.equal(interactionResponseChangedPackage({ result: { version: '1.0.0' } }), true);
  assert.equal(interactionResponseChangedPackage({}), false);
});

test('流程图未保存修改由顶层、候选修订和助手动作共用离开保护', () => {
  assert.match(appSource, /confirmUnsavedNavigation\(workspaceDirty\)/);
  assert.match(appSource, /onUnsavedChange=\{setWorkspaceDirty\}/);
  assert.match(candidateRevisionSource, /confirmAction && !confirmAction\(kind\)/);
  assert.match(packageWorkspaceSource, /confirmAction=\{confirmDiscardFlowchartChanges\}/);
  assert.match(packageChatSource, /confirmFlowchartMutation\(\)/);
  assert.match(packageWorkspaceSource, /onPendingInteractionBusyChange=\{setFlowchartSubmitBusy\}/);
  assert.match(packageWorkspaceSource, /flowchartAddBusy \|\| flowchartSubmitBusy/);
});

test('流程图有未保存修改时事件流刷新不会卸载当前编辑器', () => {
  assert.match(packageWorkspaceSource, /const workspaceDirtyRef = useRef\(false\)/);
  assert.match(packageWorkspaceSource, /deferredRefreshRef\.current = true/);
  assert.match(packageWorkspaceSource, /workspaceDirtyRef\.current = workspaceDraftDirty/);
  assert.match(
    packageWorkspaceSource,
    /if \(workspaceDraftDirty \|\| !deferredRefreshRef\.current\) return;[\s\S]*?void load\(\)/,
  );
});

test('流程图仍有未保存修改时建模助手禁止提交', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'flowchart-dirty', type: 'flowchart_review', status: 'pending',
      payload: { purpose: 'process_review', title: '确认业务流程图' },
    },
    flowchartDraftState: {
      draftRevision: 5, diagramCount: 1, dirty: true, saveStatus: 'dirty', warnings: [],
    },
    onSubmit: () => {},
  }));
  assert.match(html, /还有未保存修改/);
  assert.match(html, /disabled=""[^>]*>提交全部已保存流程图/);
});

test('流程图警告逐条展示并要求明确确认后提交', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'flowchart-warning', type: 'flowchart_review', status: 'pending',
      payload: { purpose: 'process_review', title: '确认业务流程图' },
    },
    flowchartDraftState: {
      draftRevision: 6,
      diagramCount: 1,
      dirty: false,
      saveStatus: 'saved',
      warnings: [
        { code: 'missing_edge_label', message: '判断分支需要补充逻辑文字' },
        { code: 'isolated_node', message: '存在未连接节点' },
      ],
    },
    onSubmit: () => {},
  }));

  assert.match(html, /判断分支需要补充逻辑文字/);
  assert.match(html, /存在未连接节点/);
  assert.match(html, /type="checkbox"/);
  assert.match(html, /disabled=""[^>]*>提交全部已保存流程图/);
  assert.doesNotMatch(html, /请 AI 调整|退回重做/);
  assert.match(pendingInteractionSource, /acknowledgeWarnings/);
});

test('删除最后一个业务流程并保存后禁止提交空流程列表', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'flowchart-empty', type: 'flowchart_review', status: 'pending',
      payload: { purpose: 'process_review', title: '确认业务流程图' },
    },
    flowchartDraftState: {
      draftRevision: 7, diagramCount: 0, dirty: false, saveStatus: 'saved', warnings: [],
    },
    onSubmit: () => {},
  }));

  assert.match(html, /当前没有业务流程/);
  assert.match(html, /disabled=""[^>]*>提交全部已保存流程图/);
});

test('切换流程图确认交互时先清空旧草稿并重新挂载编辑器', () => {
  assert.match(
    packageWorkspaceSource,
    /if \(!processReview\)[\s\S]*?setFlowchartDraft\(null\)[\s\S]*?setFlowchartEditorState\(null\)/,
  );
  assert.match(
    packageWorkspaceSource,
    /setFlowchartDraftLoading\(true\);[\s\S]*?setFlowchartDraft\(null\);[\s\S]*?getFlowchartDraft\(projectId, processReview\.id\)/,
  );
  assert.match(packageWorkspaceSource, /<FlowchartEditor\s+key=\{`\$\{projectId\}:\$\{processReview\.id\}`\}/);
});

test('流程图未保存时阻止浏览器卸载并在工作台切步骤前确认', () => {
  assert.match(packageWorkspaceSource, /window\.addEventListener\('beforeunload'/);
  assert.match(packageWorkspaceSource, /onUnsavedChange\?\.\(workspaceDraftDirty\)/);
  assert.match(packageWorkspaceSource, /confirmUnsavedNavigation\(workspaceDraftDirty\)/);
  assert.match(aiConstructionWorkbenchSource, /onUnsavedChange=\{reportWorkspaceDirty\}/);
});

test('一个项目只开一条事件流，且不随游标变化重连', () => {
  const routerSource = readFileSync(
    new URL('../frontend/src/ProjectWorkbenchRouter.jsx', import.meta.url),
    'utf8',
  );
  assert.match(routerSource, /<ProjectEventStreamProvider/);
  assert.doesNotMatch(routerSource, /subscribeProjectEvents/);
  assert.doesNotMatch(routerSource, /session\?\.cursor, load\]/);
  assert.doesNotMatch(packageWorkspaceSource, /subscribeProjectEvents/);
  assert.match(packageWorkspaceSource, /useProjectEvents\(projectId, handleProjectEvent/);
});

test('知识包编辑中栏不再提供数据源配置跳转入口', () => {
  assert.doesNotMatch(reviewPanelSource, /进入数据源配置/);
  assert.doesNotMatch(packageWorkspaceSource, /<ReviewPanel[^>]*onAdvance=/);
  assert.doesNotMatch(stylesSource, /pkw-review-advance/);
  assert.match(packageWorkspaceSource, /\['data', '数据源'\]/);
});

test('知识包校验存在错误时按处置类别逐项处理', () => {
  assert.match(packageCheckSource, /repairAiConstructionKnowledge/);
  assert.match(packageCheckSource, /counts\?\.错误 > 0/);
  assert.match(packageCheckSource, /逐项处理错误/);
  assert.match(packageCheckSource, /DISPOSITION_ACTION/);
  assert.match(packageCheckSource, /候选通过复检后应用/);
});

test('知识包检查栏始终提供当前可操作的重新检查入口', () => {
  assert.match(packageCheckSource, /onRecheck/);
  assert.match(packageCheckSource, /check \? '重新检查' : '执行检查'/);
  assert.match(reviewPanelSource, /onRecheck=\{onCheckRecheck\}/);
  assert.match(packageWorkspaceSource, /const recheckPackage = useCallback/);
  assert.match(packageWorkspaceSource, /onCheckRecheck=\{recheckPackage\}/);
});

test('确定性检查通过后提供可逐项选择的警告复核入口', () => {
  assert.match(packageCheckSource, /pcheck-warning-acceptance/);
  assert.match(packageCheckSource, /pendingWarnings\.map/);
  assert.match(packageCheckSource, /selectedWarnings\.includes/);
  assert.match(packageCheckSource, /说明所选警告为什么不影响本轮建模结论/);
  assert.match(packageCheckSource, /acceptPackageWarnings/);
  assert.match(packageCheckSource, /'validation'/);
});

test('动态任务弹层压平为单层卡片容器', () => {
  assert.match(stylesSource, /pchat-task-status__popover[^}]*padding:\s*0/);
  assert.match(stylesSource, /pchat-task-status__popover \.ac-message-card[^}]*border:\s*0[^}]*box-shadow:\s*none/);
  assert.match(stylesSource, /pchat-task-status__popover \.ac-message-task-plan[^}]*margin:\s*0[^}]*border:\s*0/);
});

test('建模助手可以让中栏定位流程或知识文件', () => {
  assert.match(reviewPanelSource, /mw-package-file-select/);
  assert.match(reviewPanelSource, /detail\.projectId/);
  assert.match(reviewPanelSource, /selectPackageTarget\(detail\.path\)/);
  assert.match(reviewPanelSource, /setPath\(resolved\)/);
});

test('评测和优化阶段的知识文件动作会回到编辑页后再定位文件', () => {
  assert.match(packageWorkspaceSource, /pendingFileSelectionRef/);
  assert.match(packageWorkspaceSource, /\['scenario', 'data', 'evaluate', 'improve'\]\.includes\(activeTab\)/);
  assert.match(packageWorkspaceSource, /activeTab === 'edit'\) return/);
  assert.match(packageWorkspaceSource, /setTab\('edit'\)/);
  assert.match(packageWorkspaceSource, /onNavigate\?\.\('ai_construction'\)/);
  assert.match(
    packageWorkspaceSource,
    /activeTab !== 'edit'[\s\S]*?window\.setTimeout\(\(\) => \{[\s\S]*?mw-package-file-select/,
  );
});

test('知识包 Markdown 相对链接由中栏接管而不是触发页面跳转', () => {
  assert.match(reviewPanelSource, /resolvePackageMarkdownTarget\(path, href, pkg\.files\)/);
  assert.match(reviewPanelSource, /event\.preventDefault\(\)/);
  assert.match(reviewPanelSource, /components=\{\{ a: renderPackageMarkdownLink \}\}/);
});

test('文件切换不会在 effect 清理阶段把当前加载请求悬空', () => {
  assert.doesNotMatch(
    reviewPanelSource,
    /return \(\) => fileRequestGate\.current\.invalidate\(\)/,
  );
  assert.match(reviewPanelSource, /if \(isCurrent\(\)\) setFileLoading\(false\)/);
  assert.match(reviewPanelSource, /useEffect\(\(\) => \(\) => \{[\s\S]*fileRequestGate\.current\.invalidate\(\);[\s\S]*fileAbortController\.current\?\.abort\(\);[\s\S]*\}, \[\]\)/);
  assert.match(reviewPanelSource, /window\.setTimeout\(\(\) => controller\.abort\(\), 20_000\)/);
});

test('场景推演在统一工作区内直接展示待确认交互', () => {
  assert.match(packageWorkspaceSource, /scenarioInteraction/);
  assert.match(packageWorkspaceSource, /<PendingInteraction[^>]*interaction=\{scenarioInteraction\}/);
  assert.match(packageWorkspaceSource, /respondToScenarioReview/);
  assert.match(packageWorkspaceSource, /answerScenario = async \(interactionId, response\)/);
  assert.match(packageWorkspaceSource, /onOpenCollaboration=\{openScenarioCollaboration\}/);
});

test('场景推演和数据源绑定使用纯阶段画布而不展示知识包文件树', () => {
  assert.match(packageWorkspaceSource, /\['scenario', 'data', 'evaluate', 'improve'\]\.includes\(activeTab\)/);
  assert.match(packageWorkspaceSource, /workflowStageOnly[\s\S]*pkw-workflow-stage/);
});

test('评测阶段的建模助手任务卡明确标注评测阶段', () => {
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-evaluation-stage',
    taskStageLabel: '评测阶段',
    aiConstruction: {
      progress: { currentMilestoneId: 'deterministic_confirmed', currentTaskId: 'repair' },
      tasks: [{ id: 'repair', milestoneId: 'deterministic_confirmed', title: '修复评测问题', status: 'running' }],
    },
  }));
  assert.match(html, /评测阶段 · 运行中/);
  assert.doesNotMatch(html, /AI辅助构建 · 运行中/);
});

test('统一工作台的交付阶段仍可查看版本记录', () => {
  assert.match(packageWorkspaceSource, /deliveryView/);
  assert.match(packageWorkspaceSource, />版本记录</);
  assert.match(packageWorkspaceSource, /embedded && activeTab === 'release'/);
});

test('验证改进交付阶段复用统一三栏工作台而不是替换知识包工作区', () => {
  assert.match(packageWorkspaceSource, /const stageContent =/);
  assert.match(packageWorkspaceSource, /<ReviewPanel[\s\S]*stageContent=\{stageContent\}/);
  assert.match(packageWorkspaceSource, /<PackageChat[\s\S]*structuredMessages=\{session\.aiConstructionMessages/);
  assert.doesNotMatch(packageWorkspaceSource, /activeTab === 'edit' && \(\s*<div ref=\{editGridRef\}/);
  assert.match(
    reviewPanelSource,
    /export function ReviewPanel\(\{[\s\S]*?projectId,[\s\S]*?stageContent = null,[\s\S]*?onDirtyChange,[\s\S]*?packageCheck = null,[\s\S]*?\}\)/,
  );
  assert.match(reviewPanelSource, /if \(stageContent\) return \(/);
  assert.match(stylesSource, /\.mwr \.rv-stage/);
});

test('待确认的修改作为建模助手消息展示而不是悬在助手上方', () => {
  assert.match(packageWorkspaceSource, /<PackageChat[\s\S]*candidateRevisionPanel=\{[\s\S]*<CandidateRevisionPanel/);
  assert.doesNotMatch(
    packageWorkspaceSource,
    /<aside className="pkw-assistant candidate-aware">\s*<CandidateRevisionPanel/,
  );
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'candidate-inside-chat',
    candidateRevisionPanel: React.createElement('section', {
      'aria-label': '需要你确认的修改',
    }, '候选修改卡片'),
  }));
  assert.ok(html.indexOf('知识包建模助手') < html.indexOf('需要你确认的修改'));
  assert.match(html, /pchat-candidate-review/);
});

test('优化候选成功后刷新右侧卡片并把异步出现的卡片滚动到可见区域', () => {
  assert.match(optimizationPanelSource, /onCandidateCreated\?\.\(result\)/);
  assert.match(packageWorkspaceSource, /onCandidateCreated=\{refreshCandidateRevisions\}/);
  assert.match(packageWorkspaceSource, /onVisibilityChange=\{setCandidateAttentionKey\}/);
  assert.match(packageWorkspaceSource, /candidateRevisionAttentionKey=\{candidateAttentionKey\}/);
  assert.match(
    packageChatSource,
    /if \(!candidateRevisionAttentionKey\) return;[\s\S]*?scrollTo[\s\S]*?\[candidateRevisionAttentionKey\]/,
  );
});

test('失败诊断回流只允许基于当前知识包 revision 的诊断', () => {
  assert.match(
    packageWorkspaceSource,
    /<FailureDiagnosisPanel[\s\S]*currentRevisionOid=\{pkg\?\.revision\}/,
  );
  assert.match(
    optimizationPanelSource,
    /canPlanEvaluationRepair\(diagnosis,\s*currentRevisionOid\)/,
  );
  assert.match(
    optimizationPanelSource,
    /selectableEvaluationDefects\(currentDiagnoses,\s*currentRevisionOid\)/,
  );
  assert.match(
    optimizationPanelSource,
    /submittableEvaluationDefectIds\([\s\S]*currentRevisionOid[\s\S]*\)/,
  );
});

test('优化页重新激活时刷新可改项且旧请求不能覆盖新状态', () => {
  assert.match(
    packageWorkspaceSource,
    /<OptimizationPanel[\s\S]*?active=\{evaluationLifecycleView === 'optimize'\}/,
  );
  assert.match(optimizationPanelSource, /active = true/);
  assert.match(optimizationPanelSource, /createLatestRequestGate\(\)/);
  assert.match(optimizationPanelSource, /const isCurrent = loadRequestGate\.current\.begin\(\)/);
  assert.match(optimizationPanelSource, /if \(!isCurrent\(\)\) return null/);
  assert.match(
    optimizationPanelSource,
    /if \(!active\)[\s\S]*?loadRequestGate\.current\.invalidate\(\)[\s\S]*?\[active, projectId, load\]/,
  );
});

test('自动重查挂在工作区而不是体检面板，评测与诊断页签下改包同样会重新校验', () => {
  const packageCheckSource = readFileSync(
    new URL('../frontend/src/PackageCheck.jsx', import.meta.url),
    'utf8',
  );
  // 体检面板只在「编辑与检查」页签渲染，重查跟着它走会漏掉其余页签
  assert.match(packageWorkspaceSource, /synchronizePackageCheck\(\{/);
  assert.match(packageWorkspaceSource, /const startCheckRun = useCallback/);
  assert.match(packageWorkspaceSource, /onRecheckStart: startCheckRun/);
  assert.doesNotMatch(packageCheckSource, /synchronizePackageCheck/);
  assert.match(packageCheckSource, /rechecking = false/);
  assert.match(packageWorkspaceSource, /packageCheck=\{\{/);
});

test('访谈题卡打开事件立即回源会话', () => {
  assert.match(packageWorkspaceSource, /planPackageWorkspaceProjectEvent\(\{/);
  assert.match(packageWorkspaceSource, /workspaceDirty:\s*workspaceDirtyRef\.current/);
  assert.match(packageWorkspaceSource, /if \(plan\.immediateLoad\) void load\(\{ advance: true \}\)/);
  assert.match(packageWorkspaceSource, /options\?\.advance === true \? advanceProjectSession : getProjectSession/);
});

test('waiting 运行缺少题卡时显示恢复出口而不是运行计时', () => {
  assert.match(packageChatSource, /waitingPackageChatRun/);
  assert.match(packageChatSource, /waitingCardMissing/);
  assert.match(packageChatSource, /重新加载访谈题卡/);
  assert.match(packageChatSource, /waitingInteractionRecoveryDelay/);
});
