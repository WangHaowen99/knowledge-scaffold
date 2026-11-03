import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import Fastify from 'fastify';

// Synthetic fixtures; tests only call loopback servers, never a configured model gateway.
process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-judge-model-'));
process.env.MODELING_LLM_BASE_URL = '';
process.env.MODELING_LLM_MODEL = '';
process.env.MODELING_LLM_API_KEY = '';

const { buildJudgeLlm, judgeModelFailure, parseJudgeModelConfig } = await import('../src/evaluation/judge-model.js');
const { judge } = await import('../src/evaluation/judge.js');
const { judgeEvaluationAnswer, runCase, startEvaluation, waitForEvaluation } = await import('../src/evaluation/engine.js');
const { normalizeEvaluationCaseConcurrency } = await import('../src/config.js');
const { getConsumerGlobalModelProfiles } = await import('../src/consumer/client.js');
const { createProject } = await import('../src/projects/repo.js');
const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
const m3repo = await import('../src/workflow/m3-repo.js');
const { getDb } = await import('../src/db/index.js');
const { setSetting } = await import('../src/settings/repo.js');

function settings(api: 'openai-completions' | 'anthropic-messages' = 'openai-completions') {
  return {
    api,
    base_url: 'https://gateway.example.net/v1',
    api_key: 'synthetic-judge-credential',
    model: 'synthetic-judge-model',
    max_tokens: 1024,
    context_window: 8192,
  };
}

function sendOpenAiVerdict(response: ServerResponse): void {
  response.writeHead(200, { 'Content-Type': 'text/event-stream' });
  for (const chunk of [
    { id: 'synthetic-completion', model: 'synthetic-judge-model', choices: [{ index: 0, delta: {
      role: 'assistant', tool_calls: [{ index: 0, id: 'synthetic-tool-call', type: 'function', function: {
        name: 'submit_verdict', arguments: JSON.stringify({ verdict: 'pass', reason: '合成结论一致' }),
      } }],
    }, finish_reason: null }] },
    { id: 'synthetic-completion', model: 'synthetic-judge-model', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }],
      usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 } },
  ]) response.write(`data: ${JSON.stringify(chunk)}\n\n`);
  response.end('data: [DONE]\n\n');
}

function sendAnthropicVerdict(response: ServerResponse): void {
  response.writeHead(200, { 'Content-Type': 'text/event-stream' });
  for (const event of [
    { type: 'message_start', message: {
      id: 'synthetic-message', type: 'message', role: 'assistant', model: 'synthetic-judge-model', content: [],
      stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 0 },
    } },
    { type: 'content_block_start', index: 0, content_block: { type: 'tool_use', id: 'synthetic-tool-call', name: 'submit_verdict', input: {} } },
    { type: 'content_block_delta', index: 0, delta: {
      type: 'input_json_delta', partial_json: JSON.stringify({ verdict: 'pass', reason: '合成结论一致' }),
    } },
    { type: 'content_block_stop', index: 0 },
    { type: 'message_delta', delta: { stop_reason: 'tool_use', stop_sequence: null }, usage: { output_tokens: 10 } },
    { type: 'message_stop' },
  ]) response.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  response.end();
}

async function listen(handler: (request: IncomingMessage, response: ServerResponse) => void) {
  const server = createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  return { server, url: `http://127.0.0.1:${address.port}` };
}

function eventStream(events: any[]): Response {
  return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), {
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

test('case concurrency respects the supported range and safe fallback', () => {
  for (const value of [undefined, null, 0, -1, 1.5, 'invalid']) {
    assert.equal(normalizeEvaluationCaseConcurrency(value, 6), 6);
  }
  assert.equal(normalizeEvaluationCaseConcurrency('12'), 12);
  assert.equal(normalizeEvaluationCaseConcurrency(100), 16);
  assert.equal(normalizeEvaluationCaseConcurrency(undefined, 100), 16);
});

test('async runs accept Consumer terminal output variants and retain bounded tool failures', async () => {
  for (const terminal of [
    { content: '合成答案' },
    { final_answer: '合成答案' },
    { final_output: '合成答案' },
    { final_output: { text: '合成答案' } },
    { final_output: { formatted_result: '合成答案' } },
    { final_output: { total: 2 } },
  ]) {
    const calls: string[] = [];
    const request = async (_token: string, path: string, options?: RequestInit) => {
      calls.push(path);
      if (path === '/agentic/runs') {
        const body = JSON.parse(String(options?.body));
        assert.equal(body.model_profile_id, 'global:synthetic-consumer-model');
        assert.ok((options?.headers as any)['Idempotency-Key']);
        return Response.json({ run_id: 'synthetic-run' }, { status: 202 });
      }
      return eventStream([
        { seq: 1, event_type: 'tool.started', payload: { name: 'execute_script' } },
        { seq: 2, event_type: 'tool.finished', payload: {
          name: 'execute_script', status: 'error', error: { message: 'synthetic failure ' + 'x'.repeat(1100) },
        } },
        { seq: 3, event_type: 'turn.finished', payload: { status: 'completed', ...terminal } },
      ]);
    };
    const result = await runCase(request, 'workspace_demo', '合成问题', new AbortController().signal,
      {}, 'global:synthetic-consumer-model', 'project_alpha', true);
    assert.equal(result.finalAnswer, terminal.final_output && typeof terminal.final_output === 'object'
      && 'total' in terminal.final_output ? '{"total":2}' : '合成答案');
    assert.deepEqual(result.trace.toolCalls, ['execute_script']);
    assert.equal(result.trace.toolFailures?.[0].error.length, 1024);
    assert.equal(calls.length, 2);
  }
});

test('async event replay resumes from the last sequence without duplicating trace events', async () => {
  const calls: string[] = [];
  const started = { seq: 1, event_type: 'tool.started', payload: { tool_name: 'execute_script' } };
  const request = async (_token: string, path: string) => {
    calls.push(path);
    if (path === '/agentic/runs') return Response.json({ run_id: 'synthetic-run' }, { status: 202 });
    return eventStream(calls.length === 2 ? [started] : [started,
      { seq: 2, event_type: 'turn.finished', payload: { status: 'completed', final_output: '合成答案' } },
    ]);
  };
  const result = await runCase(request, 'workspace_demo', 'Q', new AbortController().signal, {}, '', '', true);
  assert.ok(calls[2].endsWith('after_seq=1'));
  assert.deepEqual(result.trace.toolCalls, ['execute_script']);
});

test('async terminal error codes still trigger a fresh session for transient preparation failures', async () => {
  const sessions: string[] = [];
  const request = async (_token: string, path: string, options?: RequestInit) => {
    if (path === '/agentic/runs') {
      sessions.push(JSON.parse(String(options?.body)).session_id);
      return Response.json({ run_id: `synthetic-run-${sessions.length}` }, { status: 202 });
    }
    return eventStream([{ seq: 1, event_type: 'turn.finished', payload: sessions.length === 1
      ? { status: 'failed', error: 'synthetic preparation failure', error_code: 'SESSION_PREPARE_FAILED' }
      : { status: 'completed', final_output: '合成答案' } }]);
  };
  const result = await runCase(request, 'workspace_demo', 'Q', new AbortController().signal,
    { backoffMs: () => 1 }, '', '', true);
  assert.equal(result.finalAnswer, '合成答案');
  assert.equal(new Set(sessions).size, 2);
});

test('cancellation during admission cancels the allocated Consumer run before returning', async () => {
  const controller = new AbortController();
  const calls: string[] = [];
  const request = async (_token: string, path: string) => {
    calls.push(path);
    if (path === '/agentic/runs') {
      controller.abort(new Error('synthetic cancellation'));
      return Response.json({ run_id: 'synthetic-run' }, { status: 202 });
    }
    return Response.json({});
  };
  await assert.rejects(runCase(request, 'workspace_demo', 'Q', controller.signal, {}, '', '', true), /synthetic cancellation/);
  assert.deepEqual(calls, ['/agentic/runs', '/agentic/runs/synthetic-run/cancel']);
});

test('cancellation while reading events cancels the remote Consumer run', async () => {
  const controller = new AbortController();
  const calls: string[] = [];
  const request = async (_token: string, path: string) => {
    calls.push(path);
    if (path === '/agentic/runs') return Response.json({ run_id: 'synthetic-run' }, { status: 202 });
    if (path.includes('/events?')) {
      controller.abort(new Error('synthetic cancellation'));
      throw controller.signal.reason;
    }
    return Response.json({});
  };
  await assert.rejects(runCase(request, 'workspace_demo', 'Q', controller.signal, {}, '', '', true), /synthetic cancellation/);
  assert.equal(calls.at(-1), '/agentic/runs/synthetic-run/cancel');
});

test('older Consumers retain the sync protocol fallback', async () => {
  const calls: string[] = [];
  const request = async (_token: string, path: string) => {
    calls.push(path);
    return path === '/agentic/runs' ? Response.json({}, { status: 404 }) : Response.json({
      status: 'completed', final_answer: '合成答案', execution_trace: { tool_calls: [] },
    });
  };
  const result = await runCase(request, 'workspace_demo', 'Q', new AbortController().signal, {}, '', '', true);
  assert.equal(result.finalAnswer, '合成答案');
  assert.deepEqual(calls, ['/agentic/runs', '/agentic/runs/sync']);
});

test('an immediate legacy terminal response keeps its run id without opening an event stream', async () => {
  const calls: string[] = [];
  const request = async (_token: string, path: string) => {
    calls.push(path);
    return Response.json({ run_id: 'synthetic-legacy-run', status: 'completed',
      final_answer: '合成答案', execution_trace: { tool_calls: [] } });
  };
  const result = await runCase(request, 'workspace_demo', 'Q', new AbortController().signal, {}, '', '', true);
  assert.equal(result.finalAnswer, '合成答案');
  assert.equal(result.trace.runId, 'synthetic-legacy-run');
  assert.deepEqual(calls, ['/agentic/runs']);
});

test('Consumer model pool exposes only enabled global profile metadata', async (t) => {
  const { server, url } = await listen((request, response) => {
    assert.equal(request.url, '/api/system-config');
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ llm_profiles: {
      profiles: [
        { id: 'synthetic-main', name: 'synthetic model', provider: 'openai_compatible',
          model: 'synthetic-model', enabled: true, api_key: 'synthetic-secret', base_url: 'https://gateway.example.net' },
        { id: 'synthetic-disabled', model: 'synthetic-model', enabled: false },
      ],
      default_main_profile_ref: { scope: 'global', id: 'synthetic-main' },
    } }));
  });
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  setSetting('consumer_api_url', url);
  const result = await getConsumerGlobalModelProfiles('synthetic-access');
  assert.deepEqual(result, { profiles: [{ id: 'global:synthetic-main', name: 'synthetic model',
    provider: 'openai_compatible', model: 'synthetic-model' }], defaultProfileId: 'global:synthetic-main' });
});

test('invalid judge configuration is rejected without exposing submitted credentials', () => {
  for (const invalid of [null, {}, { ...settings(), api: 'unknown' }, { ...settings(), api_key: '' },
    { ...settings(), base_url: 'file:///synthetic' }, { ...settings(), max_tokens: 0 },
    { ...settings(), max_tokens: 8192 }, { ...settings(), context_window: '8192' }]) {
    assert.throws(() => parseJudgeModelConfig(invalid), (error: any) => (
      error.statusCode === 400 && !String(error.message).includes('synthetic-judge-credential')
    ));
  }
});

for (const api of ['openai-completions', 'anthropic-messages'] as const) {
  test(`judge uses Onto credentials and protocol ${api}`, async (t) => {
    const observed: Array<{ path: string; authorization: string; key: string; body: any }> = [];
    const { server, url } = await listen((request, response) => {
      let raw = '';
      request.on('data', (part) => { raw += part; });
      request.on('end', () => {
        observed.push({ path: String(request.url), authorization: String(request.headers.authorization || ''),
          key: String(request.headers['x-api-key'] || ''), body: JSON.parse(raw) });
        if (api === 'anthropic-messages') sendAnthropicVerdict(response);
        else sendOpenAiVerdict(response);
      });
    });
    t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
    const config = parseJudgeModelConfig({ ...settings(api), base_url: api === 'anthropic-messages' ? url : `${url}/v1` });
    const llm = buildJudgeLlm(config);
    assert.ok(!JSON.stringify(llm.model).includes(config.api_key));
    assert.deepEqual(await judge('合成问题', '合成答案', '合成答案', llm), { verdict: 'pass', reason: '合成结论一致' });
    assert.equal(observed.length, 1);
    assert.equal(observed[0].path, api === 'anthropic-messages' ? '/v1/messages' : '/v1/chat/completions');
    assert.equal(observed[0].authorization, `Bearer ${config.api_key}`);
    assert.equal(observed[0].body.model, config.model);
  });
}

test('parallel judge instances keep users separate and never mix keys and gateways', async (t) => {
  const seen: string[] = [];
  const { server, url } = await listen((request, response) => {
    request.resume();
    request.on('end', () => { seen.push(String(request.headers.authorization)); sendOpenAiVerdict(response); });
  });
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  const configs = ['operator_a', 'operator_b'].map((user) => parseJudgeModelConfig({
    ...settings(), base_url: `${url}/v1`, api_key: `synthetic-${user}-key`,
  }));
  await Promise.all(configs.map((config) => judge('Q', 'G', 'G', buildJudgeLlm(config))));
  assert.deepEqual(seen.sort(), configs.map((config) => `Bearer ${config.api_key}`).sort());
});

test('judge errors preserve rate limit handling and redact all gateway error content', () => {
  const unsafe = 'synthetic-judge-credential https://private.example.net/v1';
  for (const message of [`401 invalid api key ${unsafe}`, `429 rate limit ${unsafe}`, `network error ${unsafe}`]) {
    const error = judgeModelFailure(new Error(message));
    assert.ok(!error.message.includes('synthetic-judge-credential'));
    assert.ok(!error.message.includes('private.example.net'));
    assert.match(error.message, /onto-modeling/);
  }
  assert.match(judgeModelFailure(new Error(`429 ${unsafe}`)).message, /429/);
  assert.equal(judgeModelFailure(new Error('评测判分服务未按协议提交结论')).message, '评测判分服务未按协议提交结论');
  assert.match(judgeModelFailure(new Error(`context_length_exceeded ${unsafe}`)).message, /上下文超出上限/);
  assert.match(judgeModelFailure(new Error(`timed out ${unsafe}`)).message, /调用超时/);
});

test('case judge forwards the configured instance and cancellation without consulting mw settings', async () => {
  const llm = buildJudgeLlm(parseJudgeModelConfig(settings()));
  const signal = new AbortController().signal;
  await judgeEvaluationAnswer('Q', 'G', 'A', async (_q, _g, _a, instance, observedSignal) => {
    assert.equal(instance, llm);
    assert.equal(observedSignal, signal);
    return { verdict: 'pass', reason: '合成测试' };
  }, signal, llm);
});

test('real evaluation round passes Onto judge to every case and does not persist credentials', async (t) => {
  const calls: Array<{ path: string; body: any; authorization: string }> = [];
  const { server, url } = await listen((request, response) => {
    let raw = '';
    request.on('data', (part) => { raw += part; });
    request.on('end', () => {
      const path = String(request.url);
      const body = raw.startsWith('{') ? JSON.parse(raw) : {};
      calls.push({ path, body, authorization: String(request.headers.authorization) });
      if (path === '/v1/chat/completions') { sendOpenAiVerdict(response); return; }
      response.setHeader('Content-Type', 'application/json');
      if (path === '/agentic/workspaces') {
        response.end(JSON.stringify({ workspaces: [] }));
      } else if (path === '/agentic/ontologies/import-zip') {
        response.end(JSON.stringify({ ontology_id: 'knowledge_package_demo', objects_count: 0, operations_count: 0 }));
      } else if (path.endsWith('/active-version')) {
        response.end(JSON.stringify({ active_version: '1.0.0' }));
      } else if (path === '/agentic/runs') {
        response.writeHead(202);
        response.end(JSON.stringify({ run_id: `synthetic-run-${calls.length}` }));
      } else if (path.includes('/events?')) {
        response.setHeader('Content-Type', 'text/event-stream');
        response.end(`data: ${JSON.stringify({ seq: 1, event_type: 'turn.finished', payload: {
          status: 'completed', content: '合成答案', execution_trace: { tool_calls: [] },
        } })}\n\n`);
      } else {
        response.end('{}');
      }
    });
  });
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  setSetting('consumer_api_url', url);
  // An existing MW config must never win over the per-round Onto judge.
  setSetting('llm_base_url', 'http://127.0.0.1:1');
  setSetting('llm_model', 'synthetic-mw-model');
  const project = createProject({ owner: 'operator_a', name: 'project_alpha' });
  saveProjectDataSource({ projectId: project.id, mode: 'none', workspaceId: `mw-shadow-${project.id}` });
  for (const question of ['Q1', 'Q2']) m3repo.addTestcase(project.id, question, '合成答案', 'manual');
  const judgeModel = parseJudgeModelConfig({ ...settings(), base_url: `${url}/v1` });
  const started = startEvaluation({
    projectId: project.id, token: 'synthetic-consumer-access', caseConcurrency: 2, judgeModel,
    modelProfileId: 'global:synthetic-consumer-model',
    packageSnapshot: { version: '1.0.0', revisionOid: 'synthetic-revision', files: {}, spec: {
      id: 'knowledge_package_demo', name: '合成知识包', description: '合成测试', version: '1.0.0',
      language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
    } },
  });
  const round = await waitForEvaluation(project.id, started.roundId);
  assert.equal(round.status, 'done', round.log);
  assert.equal(round.passCount, 2);
  assert.equal(calls.filter((call) => call.path === '/v1/chat/completions').length, 2);
  assert.ok(calls.filter((call) => call.path.startsWith('/agentic/')).every((call) => (
    !JSON.stringify(call).includes(judgeModel.api_key) && !Object.hasOwn(call.body, 'judge_model')
  )));
  assert.ok(calls.filter((call) => call.path === '/agentic/runs')
    .every((call) => call.body.model_profile_id === 'global:synthetic-consumer-model'));
  assert.ok(!JSON.stringify(m3repo.getRoundDetails(round.id)).includes(judgeModel.api_key));
  getDb().exec('PRAGMA wal_checkpoint(FULL)');
  assert.ok(!readFileSync(join(process.env.DATA_DIR!, 'modeling.db')).includes(Buffer.from(judgeModel.api_key)));
});

test('evaluate HTTP route validates and forwards the Onto judge without echoing it', async (t) => {
  const { getAuthClient } = await import('../src/auth/client.js');
  t.mock.method(getAuthClient(), 'verify', async () => ({ username: 'operator_a', role: 'user' }));
  const authorization = `Bearer synthetic.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 600 })).toString('base64url')}.synthetic`;
  const { registerRoutes } = await import('../src/routes.js');
  const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');
  const { server, url } = await listen((request, response) => {
    let raw = '';
    request.on('data', (part) => { raw += part; });
    request.on('end', () => {
      const path = String(request.url);
      if (path === '/v1/chat/completions') { sendOpenAiVerdict(response); return; }
      response.setHeader('Content-Type', 'application/json');
      if (path === '/agentic/workspaces') response.end(JSON.stringify({ workspaces: [] }));
      else if (path === '/agentic/ontologies/import-zip') response.end(JSON.stringify({ ontology_id: 'knowledge_package_demo' }));
      else if (path.endsWith('/active-version')) response.end(JSON.stringify({ active_version: '0.1.0' }));
      else if (path === '/agentic/runs') {
        assert.ok(!raw.includes('synthetic-judge-credential'));
        response.writeHead(202);
        response.end(JSON.stringify({ run_id: 'synthetic-http-run' }));
      } else if (path.includes('/events?')) {
        response.setHeader('Content-Type', 'text/event-stream');
        response.end(`data: ${JSON.stringify({ seq: 1, event_type: 'turn.finished', payload: {
          status: 'completed', content: '合成答案', execution_trace: { tool_calls: [] },
        } })}\n\n`);
      } else response.end('{}');
    });
  });
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  setSetting('consumer_api_url', url);
  const project = createProject({ owner: 'operator_a', name: 'project_beta', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'knowledge_package_demo', name: '合成知识包', description: '合成路由测试',
  });
  saveProjectDataSource({ projectId: project.id, mode: 'none', workspaceId: 'workspace_demo' });
  m3repo.addTestcase(project.id, 'Q1', '合成答案', 'manual');
  const app = Fastify({ logger: false });
  t.after(() => app.close());
  await registerRoutes(app);
  const invalid = await app.inject({ method: 'POST', url: `/api/projects/${project.id}/evaluate`,
    headers: { authorization }, payload: { judge_model: { ...settings(), api: 'unknown' } } });
  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.ok(!invalid.body.includes('synthetic-judge-credential'));
  assert.deepEqual(m3repo.listRounds(project.id), []);
  const result = await app.inject({ method: 'POST', url: `/api/projects/${project.id}/evaluate`,
    headers: { authorization }, payload: { judge_model: { ...settings(), base_url: `${url}/v1` }, case_concurrency: 1 } });
  assert.equal(result.statusCode, 200, result.body);
  assert.deepEqual(Object.keys(result.json()), ['roundId']);
  const round = await waitForEvaluation(project.id, result.json().roundId);
  assert.equal(round.status, 'done', round.log);
  assert.equal(round.passCount, 1);
});
