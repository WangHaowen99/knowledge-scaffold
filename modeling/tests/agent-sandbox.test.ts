import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { runScriptCasesInJiuwenbox } from '../src/agent/sandbox.js';

test('脚本契约只通过 JiuwenBox 创建、执行并释放一次性沙箱', async (t) => {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'modeling-jiuwenbox-')), 'jiuwenbox.sock');
  const calls: Array<{ method: string; path: string; body: any }> = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    request.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      const body = raw ? JSON.parse(raw) : undefined;
      calls.push({ method: request.method || '', path: request.url || '', body });
      if (request.method === 'POST' && request.url?.endsWith('/exec')) {
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({
          exit_code: 0,
          stdout: '__MODELING_RESULT__[{"index":0,"passed":true,"actual":3,"expected":3}]\n',
          stderr: '',
        }));
        return;
      }
      if (request.method === 'DELETE') {
        response.statusCode = 204;
        response.end();
        return;
      }
      response.setHeader('Content-Type', 'application/json');
      response.end('{}');
    });
  });
  await new Promise<void>((resolve) => server.listen(socketPath, resolve));
  t.after(() => server.close());

  const result = await runScriptCasesInJiuwenbox({
    runId: 'run-test',
    source: 'import json\nprint(json.dumps(3))\n',
    cases: [{ argv: ['--left', '1', '--right', '2'], expected: 3 }],
    socketPath,
  });

  assert.equal(result.sandbox, 'jiuwenbox');
  assert.equal(result.passed, 1);
  assert.deepEqual(calls.map((call) => call.method), ['POST', 'POST', 'DELETE']);
  assert.equal(calls[0].body.policy_mode, 'override');
  assert.equal(calls[0].body.policy.network.egress.default, 'deny');
  assert.equal(calls[0].body.policy.network.uplink.nat, false);
  assert.equal(calls[1].body.command[0], '/usr/bin/python3');
  assert.equal(calls[1].body.command.includes('/bin/sh'), false);
  const runner = String(calls[1].body.command[3]);
  assert.doesNotMatch(runner, /runpy|case\.get\("function"\)/u);
  assert.deepEqual(
    JSON.parse(Buffer.from(calls[1].body.command[5], 'base64').toString('utf8')),
    [{ argv: ['--left', '1', '--right', '2'], expected: 3 }],
  );
  assert.match(calls[2].path, /^\/api\/v1\/sandboxes\/mdl-/u);
});

test('JiuwenBox 不可用时不回退到宿主机执行', async () => {
  const missingSocket = join(mkdtempSync(join(tmpdir(), 'modeling-no-sandbox-')), 'missing.sock');
  await assert.rejects(
    runScriptCasesInJiuwenbox({
      runId: 'run-test',
      source: 'raise RuntimeError("host must not run this")\n',
      cases: [{ argv: [], expected: null }],
      socketPath: missingSocket,
    }),
    /ENOENT|connect/u,
  );
});
