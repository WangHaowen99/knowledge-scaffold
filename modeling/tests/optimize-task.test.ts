import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as optimizeApi from '../frontend/src/api.js';

test('优化请求在面板卸载后仍可由重新进入的面板接管', async () => {
  const originalFetch = globalThis.fetch;
  let finishRequest: ((response: Response) => void) | undefined;

  globalThis.fetch = (() => new Promise<Response>((resolve) => {
    finishRequest = resolve;
  })) as typeof fetch;

  try {
    const request = optimizeApi.proposeOptimize('project-1', 'round-1');

    assert.equal(typeof optimizeApi.getOptimizeTask, 'function');
    const resumed = optimizeApi.getOptimizeTask('project-1');
    assert.equal(resumed?.kind, 'propose');
    assert.ok(Number.isFinite(Date.parse(resumed?.startedAt ?? '')));
    assert.equal(resumed?.promise, request);

    finishRequest?.(new Response(JSON.stringify({ suggestions: [{ id: 'sg-1' }] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    assert.deepEqual(await resumed?.promise, { suggestions: [{ id: 'sg-1' }] });
    assert.equal(optimizeApi.getOptimizeTask('project-1'), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('失败诊断请求在切换阶段后仍可恢复', async () => {
  const originalFetch = globalThis.fetch;
  let finishRequest: ((response: Response) => void) | undefined;
  globalThis.fetch = (() => new Promise<Response>((resolve) => { finishRequest = resolve; })) as typeof fetch;

  try {
    const request = optimizeApi.diagnoseOptimize('project-diagnose', 'round-1');
    const resumed = optimizeApi.getOptimizeTask('project-diagnose');
    assert.equal(resumed?.kind, 'diagnose');
    assert.equal(resumed?.promise, request);

    finishRequest?.(new Response(JSON.stringify({ diagnoses: [{ id: 'dg-1' }], canProposeOptimizations: true }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    assert.deepEqual(await resumed?.promise, { diagnoses: [{ id: 'dg-1' }], canProposeOptimizations: true });
    assert.equal(optimizeApi.getOptimizeTask('project-diagnose'), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
