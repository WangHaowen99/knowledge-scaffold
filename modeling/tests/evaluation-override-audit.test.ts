import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-override-audit-'));

const m3repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');

function createResult(verdict: 'pass' | 'partial' | 'fail' | 'error') {
  const project = createProject({ owner: 'alice', name: `改判-${verdict}` });
  const testcase = m3repo.addTestcase(project.id, '如何处理申请？', '按审批流程处理');
  const round = m3repo.createRound(project.id, '1.0.0', 1, 'revision-1');
  return m3repo.addResult(round.id, {
    testcaseId: testcase.id, question: testcase.question, groundTruth: testcase.groundTruth,
    finalAnswer: '原始回答', verdict, reason: '自动判分原因',
  });
}

test('人工改判必须记录原判定、理由、操作人和时间', () => {
  const result = createResult('fail');
  assert.throws(() => m3repo.overrideVerdict(result.id, 'pass', {
    reason: '', actor: 'alice',
  }), /请填写人工改判理由/);

  const overridden = m3repo.overrideVerdict(result.id, 'pass', {
    reason: '标准答案允许同义表达', actor: 'alice',
  })!;
  assert.equal(overridden.verdict, 'pass');
  assert.equal(overridden.overridden, true);
  assert.equal(overridden.originalVerdict, 'fail');
  assert.equal(overridden.overrideReason, '标准答案允许同义表达');
  assert.equal(overridden.overrideActor, 'alice');
  assert.match(overridden.overriddenAt ?? '', /^\d{4}-/);
});

test('运行错误不能被人工改判为通过', () => {
  const result = createResult('error');
  assert.throws(() => m3repo.overrideVerdict(result.id, 'pass', {
    reason: '想直接改判', actor: 'alice',
  }), /运行错误不能人工改判/);
});
