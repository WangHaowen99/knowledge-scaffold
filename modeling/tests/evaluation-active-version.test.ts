import assert from 'node:assert/strict';
import test from 'node:test';

import { activateEvaluationPackageVersion } from '../src/evaluation/engine.js';

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

test('评测只在 Consumer 明确确认目标 active_version 后继续', async () => {
  await activateEvaluationPackageVersion({
    token: 'token', workspaceId: 'workspace', ontologyId: 'ontology', version: '1.0.0',
    request: async () => response({ active_version: '1.0.0' }),
  });
  await assert.rejects(activateEvaluationPackageVersion({
    token: 'token', workspaceId: 'workspace', ontologyId: 'ontology', version: '1.0.0',
    request: async () => response({ active_version: '0.9.0' }),
  }), /激活版本不一致/);
});

test('激活请求失败会终止评测而不是记录 warning 后继续', async () => {
  await assert.rejects(activateEvaluationPackageVersion({
    token: 'token', workspaceId: 'workspace', ontologyId: 'ontology', version: '1.0.0',
    request: async () => response({ error: 'not found' }, 404),
  }), /激活评测目标版本失败（404）/);
  await assert.rejects(activateEvaluationPackageVersion({
    token: 'token', workspaceId: 'workspace', ontologyId: 'ontology', version: '1.0.0',
    request: async () => { throw new Error('network down'); },
  }), /激活评测目标版本失败：network down/);
});
