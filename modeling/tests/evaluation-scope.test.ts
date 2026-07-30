import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-scope-'));

const { evaluationFinalAnswer, judgeEvaluationAnswer, selectOfficialEvaluationTestcases } = await import('../src/evaluation/engine.js');
const m3repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');

test('正式评测运行全部用户真实用例，缺少参考答案项也不静默跳过', () => {
  const project = createProject({ owner: 'alice', name: '评测范围' });
  m3repo.addTestcase(project.id, '知识包自动建议问题', '建议答案', 'competency');
  const manual = m3repo.addTestcase(project.id, '专家真实场景问题', '', 'manual');

  const selected = selectOfficialEvaluationTestcases(m3repo.listTestcases(project.id));
  assert.deepEqual(selected.map((item) => item.id), [manual.id]);
});

test('缺少参考答案时不调用自动 judge，而是等待人工判定', async () => {
  let judgeCalls = 0;
  const verdict = await judgeEvaluationAnswer(
    '真实问题',
    '',
    '智能体给出的完整回答',
    async () => {
      judgeCalls += 1;
      return { verdict: 'pass', reason: '不应被调用' };
    },
  );

  assert.equal(judgeCalls, 0);
  assert.deepEqual(verdict, {
    verdict: 'partial',
    reason: '缺少参考答案，等待人工判定',
  });
});

test('有参考答案时仍使用自动 judge', async () => {
  let judgeCalls = 0;
  const verdict = await judgeEvaluationAnswer(
    '真实问题',
    '专家答案',
    '智能体回答',
    async (question, groundTruth, finalAnswer) => {
      judgeCalls += 1;
      assert.deepEqual([question, groundTruth, finalAnswer], ['真实问题', '专家答案', '智能体回答']);
      return { verdict: 'fail', reason: '关键结论不一致' };
    },
  );

  assert.equal(judgeCalls, 1);
  assert.deepEqual(verdict, { verdict: 'fail', reason: '关键结论不一致' });
});

test('没有用户真实用例时不能用知识包派生问题替代评测', () => {
  const project = createProject({ owner: 'alice', name: '无真实用例' });
  m3repo.addTestcase(project.id, '派生问题', '派生答案', 'competency');
  assert.throws(
    () => selectOfficialEvaluationTestcases(m3repo.listTestcases(project.id)),
    /请先录入用户真实场景用例/,
  );
});

test('Consumer 返回 error 状态时评测必须抛出真实运行错误', () => {
  assert.throws(
    () => evaluationFinalAnswer({ status: 'error', error: '模型额度已耗尽', final_answer: '' }),
    /模型额度已耗尽/,
  );
});

test('Consumer 标记成功但没有回答时不能继续进入 Judge', () => {
  assert.throws(
    () => evaluationFinalAnswer({ status: 'success', final_answer: '   ' }),
    /未返回有效回答/,
  );
});
