import assert from 'node:assert/strict';
import test from 'node:test';

import { ModelingMcpError } from '../src/mcp/service.js';
import { SseSessionRegistry } from '../src/mcp/transport.js';

const policy = {
  maxSessions: 4,
  maxSessionsPerToken: 2,
  maxSessionsPerProject: 3,
  maxSessionsPerUser: 3,
  maxSessionsPerIp: 3,
  maxLifetimeMs: 1_000,
  idleTimeoutMs: 100,
};

test('SSE 会话限制单 token 和单项目容量，释放后可重新建立', () => {
  const registry = new SseSessionRegistry(policy, () => 0, false);
  const close = () => undefined;
  const send = () => undefined;
  const first = registry.create('endpoint', 'read', {
    tokenId: 't1', projectId: 'p1', username: 'u1', clientIp: '127.0.0.1',
  }, send, close);
  registry.create('endpoint', 'read', {
    tokenId: 't1', projectId: 'p1', username: 'u1', clientIp: '127.0.0.1',
  }, send, close);
  assert.throws(
    () => registry.create('endpoint', 'read', {
      tokenId: 't1', projectId: 'p1', username: 'u1', clientIp: '127.0.0.1',
    }, send, close),
    (error: unknown) => error instanceof ModelingMcpError && error.code === 'MCP_SESSION_TOKEN_LIMIT_REACHED',
  );
  registry.create('endpoint', 'read', {
    tokenId: 't2', projectId: 'p1', username: 'u2', clientIp: '127.0.0.2',
  }, send, close);
  assert.throws(
    () => registry.create('endpoint', 'read', {
      tokenId: 't3', projectId: 'p1', username: 'u3', clientIp: '127.0.0.3',
    }, send, close),
    (error: unknown) => error instanceof ModelingMcpError && error.code === 'MCP_SESSION_PROJECT_LIMIT_REACHED',
  );
  registry.release(first.sessionId);
  assert.doesNotThrow(() => registry.create(
    'endpoint', 'read', {
      tokenId: 't3', projectId: 'p1', username: 'u3', clientIp: '127.0.0.3',
    }, send, close,
  ));
});

test('SSE 会话达到空闲或最大生命周期后主动释放', () => {
  let now = 0;
  let closed = 0;
  const registry = new SseSessionRegistry(policy, () => now, false);
  const idle = registry.create(
    'endpoint', 'read', {
      tokenId: 'idle', projectId: 'p1', username: 'u1', clientIp: '127.0.0.1',
    }, () => undefined, () => { closed += 1; },
  );
  now = 101;
  registry.releaseExpired();
  assert.equal(closed, 1);
  assert.equal(registry.resolve(idle.sessionId, 'endpoint', 'read'), null);

  const lifetime = registry.create(
    'endpoint', 'read', {
      tokenId: 'life', projectId: 'p2', username: 'u2', clientIp: '127.0.0.2',
    }, () => undefined, () => { closed += 1; },
  );
  now = 1_050;
  assert.ok(registry.resolve(lifetime.sessionId, 'endpoint', 'read'));
  now = 1_102;
  registry.releaseExpired();
  assert.equal(closed, 2);
  assert.equal(registry.resolve(lifetime.sessionId, 'endpoint', 'read'), null);
});

test('SSE 会话限制单用户和单来源地址容量', () => {
  const registry = new SseSessionRegistry({
    ...policy,
    maxSessions: 10,
    maxSessionsPerToken: 10,
    maxSessionsPerProject: 10,
    maxSessionsPerUser: 1,
    maxSessionsPerIp: 1,
  }, () => 0, false);
  const send = () => undefined;
  const close = () => undefined;
  registry.create('endpoint', 'read', {
    tokenId: 't1', projectId: 'p1', username: 'u1', clientIp: '127.0.0.1',
  }, send, close);
  assert.throws(
    () => registry.create('endpoint', 'read', {
      tokenId: 't2', projectId: 'p2', username: 'u1', clientIp: '127.0.0.2',
    }, send, close),
    (error: unknown) => error instanceof ModelingMcpError && error.code === 'MCP_SESSION_USER_LIMIT_REACHED',
  );
  assert.throws(
    () => registry.create('endpoint', 'read', {
      tokenId: 't3', projectId: 'p3', username: 'u3', clientIp: '127.0.0.1',
    }, send, close),
    (error: unknown) => error instanceof ModelingMcpError && error.code === 'MCP_SESSION_IP_LIMIT_REACHED',
  );
});
