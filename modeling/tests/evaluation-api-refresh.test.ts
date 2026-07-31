import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';

const originalFetch = globalThis.fetch;
const originalLocalStorage = globalThis.localStorage;

afterEach(() => {
  globalThis.fetch = originalFetch;
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: originalLocalStorage });
});

test('评测请求在静默续签重试后携带最新 refresh token', async () => {
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => store.set(key, String(value)),
      removeItem: (key: string) => store.delete(key),
    },
  });

  const requests: Array<{ path: string; body?: string }> = [];
  globalThis.fetch = async (path: string | URL | Request, init?: RequestInit) => {
    requests.push({ path: String(path), body: init?.body ? String(init.body) : undefined });
    if (requests.length === 1) return new Response('{}', { status: 401 });
    if (String(path) === '/auth/refresh') {
      return Response.json({ token: 'access-2', refresh_token: 'refresh-2' });
    }
    return Response.json({ roundId: 'round-1' });
  };

  const api = await import('../frontend/src/api.js');
  api.setSession({ token: 'access-1', refresh_token: 'refresh-1' });
  await api.startEvaluate('project-1');

  assert.equal(requests[0].body, JSON.stringify({ refresh_token: 'refresh-1' }));
  assert.equal(requests[2].body, JSON.stringify({ refresh_token: 'refresh-2' }));
});
