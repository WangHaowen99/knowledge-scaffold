import assert from 'node:assert/strict';
import test from 'node:test';

import { requestObservabilityDownload, requestObservabilityJson } from './api.js';

test('request adapter returns parsed successful payload', async () => {
  const request = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ runs: [{ run_id: 'run-a' }] }),
  });
  assert.deepEqual(await requestObservabilityJson(request, '/runs'), {
    runs: [{ run_id: 'run-a' }],
  });
});

test('request adapter preserves server error, status and code', async () => {
  const request = async () => ({
    ok: false,
    status: 403,
    json: async () => ({ error: '无权查看', code: 'FORBIDDEN' }),
  });
  await assert.rejects(
    () => requestObservabilityJson(request, '/overview'),
    (error) => error.message === '无权查看' && error.status === 403 && error.code === 'FORBIDDEN',
  );
});

test('request adapter supplies a stable fallback for non-JSON failures', async () => {
  const request = async () => ({
    ok: false,
    status: 502,
    json: async () => {
      throw new Error('invalid json');
    },
  });
  await assert.rejects(
    () => requestObservabilityJson(request, '/overview', {}, '加载失败'),
    { message: '加载失败（HTTP 502）' },
  );
});

test('download adapter returns the response blob and attachment filename', async () => {
  const trace = { type: 'blob' };
  const request = async () => ({
    ok: true,
    status: 200,
    headers: {
      get: (name) => name === 'content-disposition' ?
        'attachment; filename="session-a.speedscope.json"' :
        '',
    },
    blob: async () => trace,
  });

  assert.deepEqual(await requestObservabilityDownload(request, '/speedscope'), {
    blob: trace,
    fileName: 'session-a.speedscope.json',
  });
});
