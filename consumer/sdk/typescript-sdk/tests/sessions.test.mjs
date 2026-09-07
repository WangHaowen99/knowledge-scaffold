import assert from 'node:assert/strict';
import test from 'node:test';

import { AgenticSessionsApi } from '../dist/protocols/agentic/sessions.js';

test('list remains compatible while listPage exposes cursor metadata', async () => {
  const calls = [];
  const http = {
    async get(path, options = {}) {
      calls.push({ path, options });
      if (options.query) {
        return {
          sessions: [{ id: 'paged' }],
          next_cursor: 'opaque',
          has_more: true,
          index_revision: 3,
        };
      }
      return { sessions: [{ id: 'legacy' }] };
    },
  };
  const sessions = new AgenticSessionsApi(http);

  assert.deepEqual(await sessions.list(), [{ id: 'legacy' }]);
  assert.deepEqual(await sessions.listPage({ limit: 25, cursor: 'before' }), {
    sessions: [{ id: 'paged' }],
    next_cursor: 'opaque',
    has_more: true,
    index_revision: 3,
  });
  assert.deepEqual(calls, [
    { path: '/agentic/sessions', options: { signal: undefined } },
    {
      path: '/agentic/sessions',
      options: {
        query: { limit: 25, cursor: 'before' },
        signal: undefined,
      },
    },
  ]);
  assert.throws(() => sessions.listPage({ limit: 101 }), /between 1 and 100/);
});

test('listPage normalizes the legacy response when the summary index is disabled', async () => {
  const http = {
    async get() {
      return { sessions: [{ id: 'legacy-only' }] };
    },
  };
  const sessions = new AgenticSessionsApi(http);

  assert.deepEqual(await sessions.listPage({ limit: 50 }), {
    sessions: [{ id: 'legacy-only' }],
    next_cursor: null,
    has_more: false,
    index_revision: 0,
  });
});
