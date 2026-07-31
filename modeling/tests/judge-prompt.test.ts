import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { buildJudgeSystemPrompt, buildJudgeUserPrompt, judge } from '../src/evaluation/judge.js';
import { buildFauxModeler } from '../src/llm/provider.js';

test('Judge 将通用数值舍入差异视为一致', () => {
  const prompt = buildJudgeSystemPrompt();

  assert.match(prompt, /百分比绝对误差不超过 0\.05 个百分点/);
  assert.match(prompt, /一般数值相对误差不超过 0\.1%/);
  assert.match(prompt, /必须判 pass/);
  assert.match(prompt, /小数位数、尾随零或舍入方式不同/);
});

test('Judge 能看到长回答末尾的业务结论', () => {
  const answer = `${'计算过程。'.repeat(1200)}\n最终结论：所有装置均未过载，两个逻辑罐超容。`;
  const prompt = buildJudgeUserPrompt('问题', '标准答案', answer);

  assert.match(prompt, /最终结论：所有装置均未过载，两个逻辑罐超容/);
});

test('faux Judge 识别当前“智能体完整回答”提示词并按内容判分', async () => {
  const verdict = await judge(
    '申请是否通过？',
    '材料齐全，申请通过。',
    '经核验，材料齐全，申请通过。',
    buildFauxModeler(),
  );

  assert.equal(verdict.verdict, 'pass');
  assert.doesNotMatch(verdict.reason, /0\/0/);
});

test('Judge 首次只输出正文时会要求通过工具补交结论', async () => {
  const core = createFauxCore({ provider: 'judge-recovery', models: [{ id: 'judge-recovery' }] });
  core.setResponses([
    fauxAssistantMessage('关键结论不一致，应判失败。'),
    fauxAssistantMessage([fauxToolCall('submit_verdict', { verdict: 'fail', reason: '关键结论不一致' })]),
  ]);

  const verdict = await judge('问题', '标准答案', '回答', {
    model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux',
  });

  assert.equal(core.state.callCount, 2);
  assert.deepEqual(verdict, { verdict: 'fail', reason: '关键结论不一致' });
});

test('Judge 连续未提交工具结果时必须抛错而不是伪造 fail', async () => {
  const core = createFauxCore({ provider: 'judge-protocol-error', models: [{ id: 'judge-protocol-error' }] });
  core.setResponses([
    fauxAssistantMessage('没有结构化结论。'),
    fauxAssistantMessage('仍然没有结构化结论。'),
  ]);

  await assert.rejects(
    judge('问题', '标准答案', '回答', {
      model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux',
    }),
    /判分服务未按协议提交结论/,
  );
});
