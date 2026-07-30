import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';

import {
  fetchEvaluationRunTrace,
  traceFromSyncResponse,
} from '../src/evaluation/trace-client.js';

test('evaluation prefers the trace embedded in runs/sync without a second request', () => {
  assert.deepEqual(traceFromSyncResponse({
    session_id: 'session-1',
    run_id: 'run-1',
    execution_trace: { tool_calls: ['business_data_search', 'execute_script'] },
  }), {
    sessionId: 'session-1',
    runId: 'run-1',
    status: 'complete',
    toolCalls: ['business_data_search', 'execute_script'],
  });
});

test('evaluation preserves bounded tool failures from runs/sync', () => {
  assert.deepEqual(traceFromSyncResponse({
    session_id: 'session-1',
    run_id: 'run-1',
    execution_trace: {
      tool_calls: ['execute_script'],
      tool_failures: [{
        tool_name: 'execute_script',
        error: "TypeError: query() got an unexpected keyword argument 'filters'",
      }],
    },
  }), {
    sessionId: 'session-1',
    runId: 'run-1',
    status: 'complete',
    toolCalls: ['execute_script'],
    toolFailures: [{
      toolName: 'execute_script',
      error: "TypeError: query() got an unexpected keyword argument 'filters'",
    }],
  });
});

test('evaluation trace uses the modeling service credential after the user token expires', async () => {
  let seenAuth = '';
  let seenWorkspace = '';
  const server = createServer((req, res) => {
    seenAuth = String(req.headers.authorization ?? '');
    seenWorkspace = String(req.headers['x-workspace-id'] ?? '');
    assert.equal(
      req.url,
      '/agentic/internal/modeling/performance/runs/run-1?session_id=session-1',
    );
    res.statusCode = 200;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({
      tools: [
        { labels: { tool_name: 'business_data_search' } },
        { labels: { tool_name: 'execute_script' } },
      ],
    }));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));

  try {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const trace = await fetchEvaluationRunTrace({
      workspaceId: 'mw-shadow-project-1',
      sessionId: 'session-1',
      runId: 'run-1',
      serviceOptions: {
        baseUrl: `http://127.0.0.1:${address.port}`,
        serviceToken: 's'.repeat(32),
      },
      retryDelaysMs: [],
    });

    assert.equal(seenAuth, `Bearer ${'s'.repeat(32)}`);
    assert.equal(seenWorkspace, 'mw-shadow-project-1');
    assert.equal(trace.status, 'complete');
    assert.deepEqual(trace.toolCalls, ['business_data_search', 'execute_script']);
  } finally {
    server.close();
  }
});

test('evaluation trace records an explicit unavailable state instead of a false empty trace', async () => {
  const server = createServer((_req, res) => {
    res.statusCode = 503;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ error: 'temporarily unavailable' }));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));

  try {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const trace = await fetchEvaluationRunTrace({
      workspaceId: 'mw-shadow-project-1',
      sessionId: 'session-1',
      runId: 'run-1',
      serviceOptions: {
        baseUrl: `http://127.0.0.1:${address.port}`,
        serviceToken: 's'.repeat(32),
      },
      retryDelaysMs: [],
    });

    assert.equal(trace.status, 'unavailable');
    assert.deepEqual(trace.toolCalls, []);
    assert.match(trace.error ?? '', /503/);
  } finally {
    server.close();
  }
});
