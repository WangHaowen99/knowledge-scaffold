import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-testcase-prefill-'));

const { createProject } = await import('../src/projects/repo.js');
const { listTestcases, prefillFromQuestions } = await import('../src/workflow/m3-repo.js');

test('新流程从知识包业务流程预填评测问题并避免重复', () => {
  const project = createProject({ owner: 'alice', name: '流程评测', domain: '审批', background: '' });
  const questions = ['审批流程如何处理？', '异常申请如何退回？'];

  assert.equal(prefillFromQuestions(project.id, questions, 'package'), 2);
  assert.equal(prefillFromQuestions(project.id, questions, 'package'), 0);
  assert.deepEqual(listTestcases(project.id).map((item) => [item.question, item.source]), [
    ['审批流程如何处理？', 'package'],
    ['异常申请如何退回？', 'package'],
  ]);
});
