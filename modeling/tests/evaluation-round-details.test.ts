import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-evaluation-details-'));

test('评测轮次详情同时返回结果与安全的工具执行轨迹', async () => {
  const [{ createProject }, repo] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m3-repo.js'),
  ]);
  const project = createProject({ owner: 'alice', name: '评测详情', domain: 'demo', background: '' });
  const testcase = repo.addTestcase(project.id, '问题', '标准答案');
  const round = repo.createRound(project.id, '1.0.0', 1);
  const result = repo.addResult(round.id, {
    testcaseId: testcase.id,
    question: testcase.question,
    groundTruth: testcase.groundTruth,
    finalAnswer: '评测回答',
    verdict: 'fail',
    reason: '答案不完整',
    trace: {
      status: 'complete',
      toolCalls: ['business_data_search', 'execute_script'],
      toolFailures: [{ toolName: 'execute_script', error: '脚本参数缺失' }],
    },
  });

  const detail = repo.getRoundDetails(round.id);

  assert.equal(detail?.round.id, round.id);
  assert.equal(detail?.results[0].id, result.id);
  assert.deepEqual(detail?.traces[result.id]?.toolCalls, ['business_data_search', 'execute_script']);
  assert.deepEqual(detail?.traces[result.id]?.toolFailures, [
    { toolName: 'execute_script', error: '脚本参数缺失' },
  ]);
});
