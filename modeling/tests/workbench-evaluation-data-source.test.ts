import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, test } from 'node:test';

import { saveEvaluationDataSource } from '../frontend/src/workbench/lib/api.js';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const evaluationView = readFileSync(
  new URL('../frontend/src/workbench/center/EvaluationView.jsx', import.meta.url),
  'utf8',
);
const dataSourcePanel = readFileSync(
  new URL('../frontend/src/DataSourcePanel.jsx', import.meta.url),
  'utf8',
);
const workbenchEntry = readFileSync(
  new URL('../frontend/src/workbench/main.jsx', import.meta.url),
  'utf8',
);

test('新工作台评测准备页复用完整数据源组件并保持 Agent 状态独立', () => {
  assert.match(evaluationView, /import DataSourcePanel from ['"]\.\.\/\.\.\/DataSourcePanel\.jsx['"]/);
  assert.match(evaluationView, /<DataSourcePanel[\s\S]*saveDataSourceRequest=\{saveEvaluationDataSource\}[\s\S]*onAdvance=\{onSaved\}/);
  assert.doesNotMatch(evaluationView, /function ConnectionForm|function DataSourceInput/);

  assert.match(dataSourcePanel, /saveDataSourceRequest = saveDataSource/);
  assert.match(dataSourcePanel, /request: \(\) => saveDataSourceRequest\(/);
  assert.match(dataSourcePanel, /<EvaluationDatabaseImportPanel/);
  assert.match(workbenchEntry, /import ['"]\.\.\/styles\.css['"]/);
});

test('新工作台保存数据源只更新 Agent 评测事实', async () => {
  let requestPath = '';
  globalThis.fetch = (async (path: string | URL | Request) => {
    requestPath = String(path);
    return Response.json({ dataSource: { mode: 'none' } });
  }) as typeof fetch;

  await saveEvaluationDataSource('project/agent', { mode: 'none' });

  assert.equal(requestPath, '/api/projects/project%2Fagent/data-source?workflow=agent');
});
