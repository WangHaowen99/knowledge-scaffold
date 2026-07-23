import assert from 'node:assert/strict';
import test from 'node:test';

import { describeJourneyStep, resolveJourneyValue, validateJourneyPlan } from '../scripts/browser-journey.js';

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
    ],
  }));
});

test('上传步骤必须引用仓库外运行时文件而不是内嵌内容', () => {
  assert.throws(() => validateJourneyPlan({
    name: 'invalid-upload',
    steps: [{ action: 'upload', selector: 'input[type=file]', content: 'embedded' }],
  }), /上传步骤必须提供 file 或 fileEnv/);
});
