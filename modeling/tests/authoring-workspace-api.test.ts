import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import {
  adjustAiConstructionTask,
  applyAiConstructionCommand,
  editAiConstructionTasks,
  getProjectSession,
  getRevisionDiff,
  listRevisions,
  previewAiConstructionTaskAdjustment,
  previewAiConstructionTaskEdit,
  respondToInteraction,
  restoreRevision,
  subscribeProjectEvents,
} from '../frontend/src/authoring-api.js';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

async function waitFor(check: () => boolean, timeoutMs = 1_000) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) throw new Error('等待超时');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

test('相同待办回答失败重试时沿用同一个请求号', async () => {
  const calls: Array<{ url: string; body: any }> = [];
  let attempt = 0;
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({ url: String(url), body: JSON.parse(String(options.body)) });
    attempt += 1;
    return attempt === 1 ? jsonResponse({ error: '暂时不可用' }, 503) : jsonResponse({ saved: true });
  }) as typeof fetch;

  await assert.rejects(
    respondToInteraction('project-a', 'question-1', { answer: '是', detail: { b: 2, a: 1 } }),
    /暂时不可用/,
  );
  await respondToInteraction('project-a', 'question-1', { detail: { a: 1, b: 2 }, answer: '是' });
  await respondToInteraction('project-a', 'question-1', { answer: '否' });

  assert.equal(calls.length, 3);
  assert.ok(calls[0].body.requestId);
  assert.equal(calls[1].body.requestId, calls[0].body.requestId);
  assert.notEqual(calls[2].body.requestId, calls[0].body.requestId);
  await assert.rejects(respondToInteraction('project-a', 'question-1', null as any), /格式不正确/);
});

test('项目、任务、交互和版本参数全部经过 URL 编码', async () => {
  const calls: Array<{ url: string; method: string; body?: any }> = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({
      url: String(url),
      method: options.method || 'GET',
      body: options.body ? JSON.parse(String(options.body)) : undefined,
    });
    return jsonResponse({ ok: true });
  }) as typeof fetch;

  const projectId = '企业银行/项目 A';
  await getProjectSession(projectId);
  await applyAiConstructionCommand(projectId, { type: 'start_task', taskId: '任务/1', expectedRevision: 2 });
  await previewAiConstructionTaskAdjustment(projectId, '任务/1', 'redo');
  await adjustAiConstructionTask(projectId, '任务/1', { expectedRevision: 3, action: 'redo', reason: '范围变化' });
  await previewAiConstructionTaskEdit(projectId, { action: 'reorder', taskId: '任务/1' });
  await editAiConstructionTasks(projectId, {
    expectedRevision: 4,
    reason: '调整顺序',
    edit: { action: 'reorder', taskId: '任务/1' },
  });
  await respondToInteraction(projectId, '问题/1?', { accepted: true }, 'request-fixed');
  await listRevisions(projectId);
  await getRevisionDiff(projectId, '旧/版本#1', '新 版本?2');
  await restoreRevision(projectId, { targetRevision: 'old', expectedRevision: 'now' });

  const base = '/api/projects/%E4%BC%81%E4%B8%9A%E9%93%B6%E8%A1%8C%2F%E9%A1%B9%E7%9B%AE%20A';
  assert.deepEqual(calls.map((call) => [call.method, call.url]), [
    ['GET', `${base}/session`],
    ['POST', `${base}/ai-construction/commands`],
    ['GET', `${base}/ai-construction/tasks/%E4%BB%BB%E5%8A%A1%2F1/adjustment-preview?action=redo`],
    ['POST', `${base}/ai-construction/tasks/%E4%BB%BB%E5%8A%A1%2F1/adjust`],
    ['POST', `${base}/ai-construction/task-edit-preview`],
    ['POST', `${base}/ai-construction/task-edit`],
    ['POST', `${base}/interactions/%E9%97%AE%E9%A2%98%2F1%3F/respond`],
    ['GET', `${base}/revisions`],
    ['GET', `${base}/revisions/diff?from=%E6%97%A7%2F%E7%89%88%E6%9C%AC%231&to=%E6%96%B0%20%E7%89%88%E6%9C%AC%3F2`],
    ['POST', `${base}/revisions/restore`],
  ]);
  assert.deepEqual(calls[1].body.command, { type: 'start_task', taskId: '任务/1', expectedRevision: 2 });
  assert.equal(calls[6].body.requestId, 'request-fixed');
});

test('事件流断开后从最后游标续传并忽略重复事件', async () => {
  const events: any[] = [];
  const errors: Error[] = [];
  const urls: string[] = [];
  const encoder = new TextEncoder();

  globalThis.fetch = (async (url: string | URL | Request) => {
    urls.push(String(url));
    const call = urls.length;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        if (call === 1) {
          controller.enqueue(encoder.encode('retry: 250\n\nid: 1\ndata: {"seq":1,"type":"one"}\n\n'));
          controller.close();
        } else {
          controller.enqueue(encoder.encode([
            'id: 1',
            'data: {"seq":1,"type":"duplicate"}',
            '',
            'id: 2',
            'data: {"seq":2,"type":"two"}',
            '',
            '',
          ].join('\n')));
        }
      },
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }) as typeof fetch;

  const unsubscribe = subscribeProjectEvents('project/a', {
    onEvent: (event) => events.push(event),
    onError: (error) => errors.push(error),
  });
  await waitFor(() => events.length === 2);
  unsubscribe();

  assert.deepEqual(events.map((event) => [event.seq, event.type]), [[1, 'one'], [2, 'two']]);
  assert.deepEqual(urls, [
    '/api/projects/project%2Fa/events?after=0',
    '/api/projects/project%2Fa/events?after=1',
  ]);
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /已断开/);
});
