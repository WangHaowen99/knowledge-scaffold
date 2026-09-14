// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatOutputBytes,
  outputResponseDigestMatches,
  parseOutputResponse,
  resolveToolOutputUrl,
} from './toolOutput.js';

test('resolveToolOutputUrl binds an opaque ref to the active session and run', () => {
  assert.equal(
    resolveToolOutputUrl('ABCDEF0123456789ABCDEF0123456789', 'session / 1', 'run?#1'),
    '/agentic/sessions/session%20%2F%201/runs/run%3F%231/tool-outputs/abcdef0123456789abcdef0123456789',
  );
});

test('resolveToolOutputUrl rejects paths, malformed refs and missing scope', () => {
  assert.equal(resolveToolOutputUrl('/auth/users', 'session-1', 'run-1'), '');
  assert.equal(resolveToolOutputUrl('//example.com/output', 'session-1', 'run-1'), '');
  assert.equal(resolveToolOutputUrl('../secret', 'session-1', 'run-1'), '');
  assert.equal(resolveToolOutputUrl('abcdef0123456789abcdef0123456789', '', 'run-1'), '');
  assert.equal(resolveToolOutputUrl('abcdef0123456789abcdef0123456789', 'session-1', ''), '');
});

test('formatOutputBytes formats valid byte counts and ignores invalid values', () => {
  assert.equal(formatOutputBytes(512), '512 B');
  assert.equal(formatOutputBytes(1536), '1.5 KB');
  assert.equal(formatOutputBytes(2 * 1024 * 1024), '2.0 MB');
  assert.equal(formatOutputBytes(null), '');
  assert.equal(formatOutputBytes(undefined), '');
  assert.equal(formatOutputBytes(''), '');
  assert.equal(formatOutputBytes(-1), '');
  assert.equal(formatOutputBytes('invalid'), '');
});

test('parseOutputResponse preserves raw JSON artifact content', async () => {
  const source = '{"output":"business value","error":"diagnostic","other":1}';
  const response = new Response(source, {
    headers: { 'Content-Type': 'application/json' },
  });

  assert.equal(await parseOutputResponse(response, { raw: true }), source);
});

test('parseOutputResponse still unwraps regular tool output envelopes', async () => {
  const response = new Response('{"output":"tool result","other":1}', {
    headers: { 'Content-Type': 'application/json' },
  });

  assert.equal(await parseOutputResponse(response), 'tool result');
});

test('outputResponseDigestMatches rejects a mismatched authoritative artifact', () => {
  const response = {
    headers: new Headers({ 'X-Tool-Output-SHA256': 'sha256:stored' }),
  };

  assert.equal(
    outputResponseDigestMatches(response, 'sha256:expected'),
    false,
  );
  assert.equal(
    outputResponseDigestMatches(response, 'sha256:stored'),
    true,
  );
  assert.equal(outputResponseDigestMatches({ headers: new Headers() }, 'sha256:expected'), false);
  assert.equal(outputResponseDigestMatches({ headers: new Headers() }, ''), true);
});
