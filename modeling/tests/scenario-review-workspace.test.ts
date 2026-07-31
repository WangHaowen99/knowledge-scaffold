import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, test } from 'node:test';

import { respondToScenarioReview } from '../frontend/src/authoring-api.js';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

test('场景审核使用专用接口并在失败重试时复用请求号', async () => {
  const calls: Array<{ url: string; body: any }> = [];
  let attempt = 0;
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({ url: String(url), body: JSON.parse(String(options.body)) });
    attempt += 1;
    return attempt === 1 ? jsonResponse({ error: '暂时不可用' }, 503) : jsonResponse({ remainingCount: 0 });
  }) as typeof fetch;

  await assert.rejects(
    respondToScenarioReview('项目/一', '场景/一', { action: 'accept' }),
    /暂时不可用/,
  );
  await respondToScenarioReview('项目/一', '场景/一', { action: 'accept' });

  assert.equal(calls[0].url, '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E4%B8%80/workflow/scenario/reviews/%E5%9C%BA%E6%99%AF%2F%E4%B8%80/respond');
  assert.equal(calls[1].body.requestId, calls[0].body.requestId);
  assert.deepEqual(calls[1].body.response, { action: 'accept' });
  await assert.rejects(respondToScenarioReview('project-a', 'scenario-a', null as any), /格式不正确/);
});

test('统一工作台把场景确认入口、专用提交和下一项聚焦接在同一面板', () => {
  const source = readFileSync(new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url), 'utf8');
  assert.match(source, /respondToScenarioReview\(projectId, interactionId, response\)/);
  assert.match(source, /result\.remainingCount > 0/);
  assert.match(source, /onOpenCollaboration=\{openScenarioCollaboration\}/);
  assert.match(source, /ref=\{scenarioReviewRef\}/);
  assert.match(source, /aria-label="场景推演确认"/);
});
