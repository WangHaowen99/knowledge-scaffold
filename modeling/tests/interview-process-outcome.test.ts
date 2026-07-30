import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-interview-process-'));

const { createProject } = await import('../src/projects/repo.js');
const { getNode } = await import('../src/workflow/engine.js');
const { answerInterview, startInterview } = await import('../src/workflow/m2.js');

function llmWith(...responses: ReturnType<typeof fauxAssistantMessage>[]) {
  const core = createFauxCore({ provider: 'interview-process', models: [{ id: 'interview-process' }] });
  core.setResponses(responses);
  return { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };
}

test('模型未提交结构化流程时不能把专家访谈伪装成已完成', async () => {
  const project = createProject({
    owner: 'alice', name: '访谈流程门禁', domain: '审批', background: '',
  });
  const started = await startInterview(project, { tables: [], documents: [] }, {}, llmWith(
    fauxAssistantMessage([fauxToolCall('ask_user', {
      questions: [{ kind: 'text', q: '请描述审批步骤', options: [], topic: '步骤' }],
    })]),
  ));
  assert.ok(started.batch);

  await assert.rejects(
    answerInterview(project, { tables: [], documents: [] }, started.batch!.id, [{
      questionId: started.batch!.questions[0].id,
      value: '受理、审核、审批、通知',
    }], llmWith(
      fauxAssistantMessage('流程已经比较清楚。'),
      fauxAssistantMessage('访谈结束。'),
    )),
    /未提交可绘制的业务流程/,
  );

  assert.notEqual(getNode(started.nodeId)?.status, 'done');
  assert.equal(getNode(started.nodeId)?.output, undefined);
});
