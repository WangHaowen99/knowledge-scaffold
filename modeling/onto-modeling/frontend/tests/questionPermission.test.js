import assert from 'node:assert/strict';
import test from 'node:test';
import { createApi } from '../src/api/endpoints.js';
import { decisionAnswer } from '../src/pages/workbench/questionAnswers.js';

// 决策卡逐题回答走 S-11，答复体只有选项 ID 和文字（不再按问题文本拼答案发到确认接口 S-10）。合成项目与题目
test('决策卡答复发到 S-11，多选连同「其他」文字一起提交', async () => {
  const calls = [];
  const api = createApi({ post: async (path, body) => { calls.push({ path, body }); return { question: { state: 'answered' } }; } });
  const q = { options: [{ id: '0.0' }, { id: '0.1' }, { id: '0.custom', custom: true }], multi: true };
  const body = decisionAnswer(q, ['0.0', '0.custom'], '合成补充');
  await api.answerQuestion('project_alpha', 'conv_demo', 'request_demo-0', body);
  assert.deepEqual(calls, [{
    path: '/projects/project_alpha/conversations/conv_demo/questions/request_demo-0/answer',
    body: { option_ids: ['0.0', '0.custom'], custom_text: '合成补充' },
  }]);
});
