import assert from 'node:assert/strict';
import test from 'node:test';

import { formatOutputBytes, resolveToolOutputUrl } from './toolOutput.js';

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
  assert.equal(formatOutputBytes(-1), '');
  assert.equal(formatOutputBytes('invalid'), '');
});
