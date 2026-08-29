import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  createProjectEvaluationDatabase,
  importEvaluationDatabase,
} from '../frontend/src/api.js';

const originalFetch = globalThis.fetch;
const importPanel = readFileSync(
  new URL('../frontend/src/EvaluationDatabaseImportPanel.jsx', import.meta.url),
  'utf8',
);
const urlConfirmModal = readFileSync(
  new URL('../frontend/src/EvaluationDatabaseUrlConfirmModal.jsx', import.meta.url),
  'utf8',
);

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('项目评测库 API 按服务端契约提交默认和自定义数据库连接', async () => {
  const requests: Array<{ path: string; options: RequestInit }> = [];
  globalThis.fetch = (async (path: string | URL | Request, options: RequestInit = {}) => {
    requests.push({ path: String(path), options });
    return Response.json({ database: 'mv_shadow_project_1', created: false });
  }) as typeof fetch;

  await createProjectEvaluationDatabase('project-1', { mode: 'default', databaseUrl: '' });
  await createProjectEvaluationDatabase('project-1', {
    mode: 'custom',
    databaseUrl: 'mysql://user:password@mysql.local:3306',
  });

  assert.equal(requests[0].path, '/api/projects/project-1/evaluation-database/project-database');
  assert.deepEqual(JSON.parse(String(requests[0].options.body)), {
    database_url_mode: 'default',
  });
  assert.deepEqual(JSON.parse(String(requests[1].options.body)), {
    database_url_mode: 'custom',
    database_url: 'mysql://user:password@mysql.local:3306',
  });
});

test('评测库导入 API 使用弹窗返回的自定义数据库连接', async () => {
  let requestBody: FormData | undefined;
  globalThis.fetch = (async (_path: string | URL | Request, options: RequestInit = {}) => {
    requestBody = options.body as FormData;
    return Response.json({ run: { id: 'run-1', status: 'running' } });
  }) as typeof fetch;

  const file = new Blob(['excel'], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
  await importEvaluationDatabase('project-1', file, true, {
    mode: 'custom',
    databaseUrl: 'mysql://user:password@mysql.local:3306',
  });

  assert.equal(requestBody?.get('database_url_mode'), 'custom');
  assert.equal(requestBody?.get('database_url'), 'mysql://user:password@mysql.local:3306');
  assert.equal(requestBody?.get('custom_database_url'), null);
});

test('Excel 导入确认复用已配置的服务端连接展示模式', () => {
  assert.match(importPanel, /selectionLocked=\{Boolean\(targetRequest\.lockedSelection\) \|\| targetRequest\.configured\}/);
  assert.match(importPanel, /lockedSelection=\{targetRequest\.lockedSelection\}/);
  assert.match(urlConfirmModal, /使用已确认的服务端 EVAL_DB_URL/);
  assert.match(urlConfirmModal, /使用已确认的本次自定义 URL/);
  assert.match(urlConfirmModal, /databaseUrl: lockedChoice\.mode === 'custom' \? lockedChoice\.databaseUrl : ''/);
});
