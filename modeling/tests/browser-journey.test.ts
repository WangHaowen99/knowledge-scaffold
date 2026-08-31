import assert from 'node:assert/strict';
import test from 'node:test';

import {
  browserProtocolTimeout,
  chooseGuidedConstructionAction,
  describeJourneyStep,
  guidedInteractionCanAdvance,
  guidedConstructionIsProcessing,
  GUIDED_PENDING_INTERACTION_SELECTOR,
  isGuidedInteractionResponse,
  shouldRecoverGuidedRepairConflict,
  nextGuidedIdleIterations,
  matchesJourneyControlText,
  resolveJourneyFiles,
  resolveJourneyValue,
  waitForJourneyText,
  validateJourneyPlan,
} from '../scripts/browser-journey.js';

test('长时间页面等待会同步抬高浏览器协议超时', () => {
  assert.equal(browserProtocolTimeout({
    name: 'long-running-diagnosis',
    steps: [
      { action: 'waitFor', text: '诊断已完成', timeoutMs: 30 * 60_000 },
    ],
  }), 31 * 60_000);
});

test('提交结构化待办时只等待当前交互的响应请求', () => {
  assert.equal(isGuidedInteractionResponse(
    'http://example.test/api/projects/p1/interactions/i1/respond',
    'POST',
  ), true);
  assert.equal(isGuidedInteractionResponse(
    'http://example.test/api/projects/p1/interactions/i1/respond',
    'GET',
  ), false);
  assert.equal(isGuidedInteractionResponse(
    'http://example.test/api/projects/p1/agent/interactions/i1/answer',
    'POST',
  ), true);
  assert.equal(isGuidedInteractionResponse(
    'http://example.test/api/projects/p1/session',
    'POST',
  ), false);
});

test('知识包修订变化导致待办冲突时重新进入受控修复', () => {
  assert.equal(shouldRecoverGuidedRepairConflict(409, 'REVISION_CONFLICT'), true);
  assert.equal(shouldRecoverGuidedRepairConflict(409, 'busy'), true);
  assert.equal(shouldRecoverGuidedRepairConflict(409, 'OTHER_CONFLICT'), false);
  assert.equal(shouldRecoverGuidedRepairConflict(400, 'REVISION_CONFLICT'), false);
});

test('结构化待办提交先确认浏览器确实发起请求再等待长响应', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /waitForRequest/);
  assert.match(source, /提交按钮没有发起交互请求/);
});

test('结构化选择尚未勾选时先选择选项再等待提交按钮解锁', () => {
  assert.equal(guidedInteractionCanAdvance({
    hasEnabledSubmit: false,
    hasSelectableChoice: true,
  }), true);
  assert.equal(guidedInteractionCanAdvance({
    hasEnabledSubmit: false,
    hasSelectableChoice: false,
  }), false);
});

test('建模驾驶员同时识别问答表单和候选变更确认卡', () => {
  assert.match(GUIDED_PENDING_INTERACTION_SELECTOR, /section\[aria-label="待确认事项"\]/);
  assert.match(GUIDED_PENDING_INTERACTION_SELECTOR, /\[data-interaction-id\] \.card\.card-accent/);
});

test('可操作待办不会被历史任务卡的进行中快照阻塞', () => {
  assert.equal(guidedConstructionIsProcessing({
    hasSpinner: false,
    hasRunningTask: true,
    hasPendingInteraction: true,
  }), false);
  assert.equal(guidedConstructionIsProcessing({
    hasSpinner: true,
    hasRunningTask: true,
    hasPendingInteraction: true,
  }), true);
  assert.equal(guidedConstructionIsProcessing({
    hasSpinner: false,
    hasRunningTask: true,
    hasPendingInteraction: false,
  }), true);
});

test('流内长操作存在停止入口时保持等待而不累计空闲次数', () => {
  assert.equal(guidedConstructionIsProcessing({
    hasSpinner: false,
    hasRunningTask: false,
    hasActiveOperation: true,
    hasPendingInteraction: false,
  }), true);
});

test('长任务仍在运行时不会累计建模驾驶员空闲次数', () => {
  assert.equal(nextGuidedIdleIterations(149, { progressed: false, processing: true }), 0);
  assert.equal(nextGuidedIdleIterations(10, { progressed: true, processing: false }), 0);
  assert.equal(nextGuidedIdleIterations(10, { progressed: false, processing: false }), 11);
});

test('精确文字操作会忽略按钮中的装饰图标', () => {
  assert.equal(matchesJourneyControlText('＋ 新建项目', ['＋', '新建项目'], '新建项目', true), true);
  assert.equal(matchesJourneyControlText('新建项目说明', ['新建项目说明'], '新建项目', true), false);
});

test('受控建模驾驶员优先处理结构化待办，其次推进阶段动作', () => {
  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    pendingPlanApproval: true,
    nextActionIds: [],
    processing: true,
  }), { kind: 'approve_plan' });

  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: { type: 'question', canSubmit: true },
    nextActionIds: ['confirm_materials'],
  }), { kind: 'answer_interaction' });

  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: { type: 'question', canSubmit: false },
    nextActionIds: [],
    processing: true,
  }), { kind: 'wait' });

  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: { type: 'question', canSubmit: false },
    nextActionIds: ['repair_knowledge_structure', 'view_validation'],
    processing: false,
  }), { kind: 'click_action', actionId: 'repair_knowledge_structure' });

  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    nextActionIds: ['review_materials', 'confirm_goal', 'open_package_file'],
  }), { kind: 'click_action', actionId: 'confirm_goal' });

  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    nextActionIds: ['confirm_knowledge', 'accept_candidate_revision'],
  }), { kind: 'click_action', actionId: 'accept_candidate_revision' });
});

test('结构化待办正在提交时等待服务端完成而不重复点击', () => {
  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: { type: 'change_plan', canSubmit: true },
    nextActionIds: [],
    processing: true,
  }), { kind: 'wait' });
});

test('受控建模驾驶员会先通过页面复核可暂缓的提醒再继续确认', () => {
  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    warningAcceptance: { canSubmit: true },
    nextActionIds: ['confirm_construction'],
  }), { kind: 'accept_warnings' });
  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    warningAcceptance: { canSubmit: false },
    nextActionIds: ['confirm_construction'],
  }), { kind: 'accept_warnings' });
});

test('受控建模驾驶员不会用查看或解释动作伪装流程推进', () => {
  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    nextActionIds: ['review_materials', 'review_process_files', 'view_validation'],
  }), { kind: 'wait' });
  assert.deepEqual(chooseGuidedConstructionAction({
    pendingInteraction: null,
    nextActionIds: ['defer_script_generation'],
  }), { kind: 'wait' });
});

test('受控建模驾驶员只从最新动作卡推进流程', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /querySelectorAll<HTMLElement>\('\.ac-message-actions'\)/);
  assert.match(source, /actionSections\.at\(-1\)/);
});

test('等待页面文字不局限于按钮和标签控件', async () => {
  let receivedText = '';
  const handle = { dispose: async () => undefined };
  const page = {
    waitForFunction: async (_predicate: unknown, _options: unknown, text: string) => {
      receivedText = text;
      return handle;
    },
  };

  await waitForJourneyText(page as never, '注册成功，请登录', 1_000);

  assert.equal(receivedText, '注册成功，请登录');
});

test('浏览器旅程从环境变量读取敏感输入且不在步骤描述中回显', () => {
  const step = { action: 'fill', selector: 'input[type=password]', valueEnv: 'E2E_PASSWORD' } as const;
  const value = resolveJourneyValue(step, { E2E_PASSWORD: 'secret-value' });

  assert.equal(value, 'secret-value');
  assert.equal(describeJourneyStep(step).includes('secret-value'), false);
  assert.match(describeJourneyStep(step), /E2E_PASSWORD/);
});

test('浏览器旅程只接受受控前端动作并拒绝直接 API 调用', () => {
  assert.throws(() => validateJourneyPlan({
    name: 'invalid',
    steps: [{ action: 'request', url: '/api/projects' }],
  }), /不支持的前端动作/);

  assert.doesNotThrow(() => validateJourneyPlan({
    name: 'valid',
    steps: [
      { action: 'goto', path: '/' },
      { action: 'click', text: '新建项目' },
      { action: 'fill', selector: 'input', value: '通用测试项目' },
      { action: 'assertText', text: 'AI辅助构建' },
      { action: 'assertNotText', text: '原始内部错误' },
      { action: 'assertVisible', selector: 'button[aria-label="发送"]' },
      { action: 'assertLayout' },
      { action: 'clickAll', selector: '.wb-review-group' },
      { action: 'reviewAndAccept', timeoutMs: 60_000 },
      { action: 'reload' },
      { action: 'guidedConstruction', timeoutMs: 60_000 },
      { action: 'click', text: '进入项目', waitForSelector: '.tabs-fill', maxAttempts: 3 },
    ],
  }));

  assert.throws(() => validateJourneyPlan({
    name: 'invalid-click-retry',
    steps: [{ action: 'click', text: '进入项目', maxAttempts: 3 }],
  }), /重复点击必须提供 waitForSelector/);
});

test('浏览器旅程数量断言必须提供确定的选择器和整数', () => {
  assert.throws(() => validateJourneyPlan({
    name: 'invalid-count', steps: [{ action: 'assertCount', selector: '.candidate' }],
  }), /整数 expectedCount/);
  assert.doesNotThrow(() => validateJourneyPlan({
    name: 'valid-count', steps: [{ action: 'assertCount', selector: '.candidate', expectedCount: 1 }],
  }));
});

test('上传步骤必须引用仓库外运行时文件而不是内嵌内容', () => {
  assert.throws(() => validateJourneyPlan({
    name: 'invalid-upload',
    steps: [{ action: 'upload', triggerText: '选择文件', content: 'embedded' }],
  }), /上传步骤必须提供 file 或 fileEnv/);

  assert.throws(() => validateJourneyPlan({
    name: 'hidden-upload',
    steps: [{ action: 'upload', selector: 'input[type=file]', file: '/tmp/input.zip' }],
  }), /可见的 triggerSelector 或 triggerText/);
});

test('浏览器旅程可以从环境变量一次选择多份建模资料且不回显路径', () => {
  const step = { action: 'upload', triggerText: '选择资料', filesEnv: 'E2E_MATERIAL_FILES' } as const;
  const files = resolveJourneyFiles(step, {
    E2E_MATERIAL_FILES: JSON.stringify(['/tmp/material-a.md', '/tmp/material-b.xlsx']),
  });

  assert.deepEqual(files, ['/tmp/material-a.md', '/tmp/material-b.xlsx']);
  assert.equal(describeJourneyStep(step).includes('/tmp/material-a.md'), false);
  assert.match(describeJourneyStep(step), /E2E_MATERIAL_FILES/);
  assert.doesNotThrow(() => validateJourneyPlan({ name: 'multi-upload', steps: [step] }));
});

test('浏览器旅程上传时必须点击可见入口并等待原生文件选择器', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /waitForFileChooser/);
  assert.match(source, /triggerSelector/);
  assert.doesNotMatch(source, /\.uploadFile\(/);
});

test('浏览器旅程在操作页面前会关闭首次功能引导遮罩', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /dismissVisibleTour\(page\)/);
  assert.match(source, /\.tour-skip/);
});

test('浏览器旅程会为隔离页面上下文安装转译辅助函数', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /installBrowserEvaluationHelpers\(page\)/);
  assert.match(source, /globalThis\.__name/);
});

test('浏览器旅程替换受控输入时只使用鼠标和键盘', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /keyboard\.down\('Control'\)/);
  assert.match(source, /keyboard\.up\('Control'\)/);
  assert.match(source, /press\('Backspace'\)/);
  assert.doesNotMatch(source, /HTMLInputElement\.prototype/);
  assert.doesNotMatch(source, /dispatchEvent\(/);
});

test('浏览器旅程由浏览器鼠标点击控件，不在页面脚本中调用 DOM click', async () => {
  const source = await import('node:fs/promises').then(({ readFile }) => (
    readFile(new URL('../scripts/browser-journey.ts', import.meta.url), 'utf8')
  ));
  assert.match(source, /ElementHandle<HTMLElement>\)\.click\(\)/);
  assert.match(source, /ElementHandle<HTMLElement>\)\.hover\(\)/);
  assert.doesNotMatch(source, /node\.click\(\)/);
  assert.doesNotMatch(source, /button\?\.click\(\)/);
  assert.match(source, /item\.querySelectorAll\('\*'\)/);
});
