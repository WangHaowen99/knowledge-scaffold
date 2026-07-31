import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';

import {
  adjustAiConstructionTask,
  confirmAiConstructionMilestone,
  editAiConstructionTasks,
  deferAiConstructionScripts,
  generateAiConstructionKnowledge,
  getProjectSession,
  getRevisionDiff,
  listRevisions,
  previewAiConstructionTaskAdjustment,
  previewAiConstructionTaskEdit,
  repairAiConstructionKnowledge,
  retryAiConstructionScripts,
  reviseAiConstructionModelingBrief,
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

async function waitFor(check: () => boolean, timeoutMs = 800) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) throw new Error('等待超时');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

test('相同回答失败重试时沿用同一个请求号', async () => {
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
  await respondToInteraction('project-a', 'question-1', { detail: { a: 1, b: 2 }, answer: '否' });

  assert.equal(calls.length, 3);
  assert.ok(calls[0].body.requestId);
  assert.equal(calls[1].body.requestId, calls[0].body.requestId);
  assert.notEqual(calls[2].body.requestId, calls[0].body.requestId);
  assert.deepEqual(calls[1].body.response, { detail: { a: 1, b: 2 }, answer: '是' });
  await assert.rejects(respondToInteraction('project-a', 'question-1', null as any), /格式不正确/);
});

test('项目、交互和版本参数都经过 URL 编码', async () => {
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
  await deferAiConstructionScripts(projectId);
  await previewAiConstructionTaskAdjustment(projectId, '任务/1', 'redo');
  await adjustAiConstructionTask(projectId, '任务/1', { expectedRevision: 4, action: 'redo', reason: '范围变化' });
  await previewAiConstructionTaskEdit(projectId, {
    action: 'reorder', taskId: '任务/1', beforeTaskId: '任务/2',
  });
  await editAiConstructionTasks(projectId, {
    expectedRevision: 5, reason: '先确认范围',
    edit: { action: 'reorder', taskId: '任务/1', beforeTaskId: '任务/2' },
  });
  await respondToInteraction(projectId, '问题/1?', { value: '确认' }, 'request-fixed');
  await listRevisions(projectId);
  await getRevisionDiff(projectId, '旧/版本#1', '新 版本?2');
  await restoreRevision(projectId, { targetRevision: 'old', expectedRevision: 'now' });

  const base = '/api/projects/%E4%BC%81%E4%B8%9A%E9%93%B6%E8%A1%8C%2F%E9%A1%B9%E7%9B%AE%20A';
  assert.deepEqual(calls.map((call) => [call.method, call.url]), [
    ['GET', `${base}/session`],
    ['POST', `${base}/ai-construction/scripts/defer`],
    ['GET', `${base}/ai-construction/tasks/%E4%BB%BB%E5%8A%A1%2F1/adjustment-preview?action=redo`],
    ['POST', `${base}/ai-construction/tasks/%E4%BB%BB%E5%8A%A1%2F1/adjust`],
    ['POST', `${base}/ai-construction/task-edit-preview`],
    ['POST', `${base}/ai-construction/task-edit`],
    ['POST', `${base}/interactions/%E9%97%AE%E9%A2%98%2F1%3F/respond`],
    ['GET', `${base}/revisions`],
    ['GET', `${base}/revisions/diff?from=%E6%97%A7%2F%E7%89%88%E6%9C%AC%231&to=%E6%96%B0%20%E7%89%88%E6%9C%AC%3F2`],
    ['POST', `${base}/revisions/restore`],
  ]);
  assert.deepEqual(calls[3].body, { expectedRevision: 4, action: 'redo', reason: '范围变化' });
  assert.deepEqual(calls[4].body, {
    edit: { action: 'reorder', taskId: '任务/1', beforeTaskId: '任务/2' },
  });
  assert.deepEqual(calls[5].body, {
    expectedRevision: 5, reason: '先确认范围',
    edit: { action: 'reorder', taskId: '任务/1', beforeTaskId: '任务/2' },
  });
  assert.equal(calls[6].body.requestId, 'request-fixed');
  assert.deepEqual(calls[9].body, { targetRevision: 'old', expectedRevision: 'now' });
});

test('知识结构修复使用独立受控修复接口', async () => {
  const calls: Array<{ url: string; method: string; body?: string }> = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({
      url: String(url),
      method: options.method || 'GET',
      body: options.body ? String(options.body) : undefined,
    });
    return jsonResponse({ status: 'valid', validation: { errors: 0 } });
  }) as typeof fetch;

  await repairAiConstructionKnowledge('项目/一');

  assert.deepEqual(calls, [{
    url: '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E4%B8%80/ai-construction/knowledge/repair',
    method: 'POST',
    body: '{}',
  }]);
});

test('建模里程碑与后续任务都调用已接线的 authoring 接口', async () => {
  const calls: Array<{ url: string; body?: any }> = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({
      url: String(url),
      body: options.body ? JSON.parse(String(options.body)) : undefined,
    });
    return jsonResponse({ ok: true });
  }) as typeof fetch;

  await confirmAiConstructionMilestone('project/a', 'goal_confirmed', 7, '确认目标');
  await reviseAiConstructionModelingBrief('project/a');
  await generateAiConstructionKnowledge('project/a');
  await deferAiConstructionScripts('project/a');
  await retryAiConstructionScripts('project/a');

  const base = '/api/projects/project%2Fa/ai-construction';
  assert.deepEqual(calls, [
    {
      url: `${base}/commands`,
      body: {
        command: {
          type: 'confirm_milestone', milestoneId: 'goal_confirmed',
          expectedRevision: 7, note: '确认目标',
        },
      },
    },
    { url: `${base}/modeling-brief/revise`, body: {} },
    { url: `${base}/knowledge/generate`, body: {} },
    { url: `${base}/scripts/defer`, body: {} },
    { url: `${base}/scripts/retry`, body: {} },
  ]);
});

test('事件订阅读取分块消息并从指定位置开始', async () => {
  const events: any[] = [];
  const errors: Error[] = [];
  const calls: Array<{ url: string; options: RequestInit }> = [];
  let streamCancelled = false;
  const encoder = new TextEncoder();
  const message = [
    'retry: 500',
    '',
    'id: 4',
    'event: authoring.run.updated',
    'data: {"id":"e4","projectId":"p/a","seq":4,"type":"authoring.run.updated","payload":{"status":"running"}}',
    '',
    'id: 5',
    'event: authoring.interaction.opened',
    'data: {"id":"e5","projectId":"p/a","seq":5,"type":"authoring.interaction.opened","payload":{"interactionId":"i1"}}',
    '',
    '',
  ].join('\n');

  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({ url: String(url), options });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        const bytes = encoder.encode(message);
        controller.enqueue(bytes.slice(0, 37));
        controller.enqueue(bytes.slice(37, 121));
        controller.enqueue(bytes.slice(121));
      },
      cancel() {
        streamCancelled = true;
      },
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }) as typeof fetch;

  const unsubscribe = subscribeProjectEvents('p/a', {
    after: 3,
    onEvent: (event) => events.push(event),
    onError: (error) => errors.push(error),
  });
  await waitFor(() => events.length === 2);
  unsubscribe();
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.deepEqual(events.map((event) => [event.seq, event.type]), [
    [4, 'authoring.run.updated'],
    [5, 'authoring.interaction.opened'],
  ]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/projects/p%2Fa/events?after=3');
  assert.equal((calls[0].options.headers as Record<string, string>).Accept, 'text/event-stream');
  assert.equal((calls[0].options.signal as AbortSignal).aborted, true);
  assert.equal(streamCancelled, true);
  assert.deepEqual(errors, []);
});

test('事件流断开后从最后位置继续', async () => {
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
          controller.enqueue(encoder.encode('id: 2\ndata: {"seq":2,"type":"two"}\n\n'));
        }
      },
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }) as typeof fetch;

  const unsubscribe = subscribeProjectEvents('project-a', {
    onEvent: (event) => events.push(event),
    onError: (error) => errors.push(error),
  });
  await waitFor(() => events.length === 2, 1_000);
  unsubscribe();

  assert.deepEqual(events.map((event) => event.seq), [1, 2]);
  assert.deepEqual(urls, [
    '/api/projects/project-a/events?after=0',
    '/api/projects/project-a/events?after=1',
  ]);
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /已断开/);
});

test('主动取消等待中的连接不会报错或重连', async () => {
  const errors: Error[] = [];
  const signals: AbortSignal[] = [];
  let calls = 0;
  globalThis.fetch = (async (_url: string | URL | Request, options: RequestInit = {}) => {
    calls += 1;
    signals.push(options.signal as AbortSignal);
    return await new Promise<Response>((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    });
  }) as typeof fetch;

  const unsubscribe = subscribeProjectEvents('project-a', { onError: (error) => errors.push(error) });
  await waitFor(() => signals.length === 1);
  unsubscribe();
  await new Promise((resolve) => setTimeout(resolve, 30));

  assert.equal(signals[0].aborted, true);
  assert.equal(calls, 1);
  assert.deepEqual(errors, []);
});
