import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DATA_SOURCE_MODE,
  buildDataSourcePayload,
  dataSourceScopeKey,
  isCurrentDataSourceRequest,
  normalizeDataSourceDraft,
  publicProviderConfig,
  validateDataSourceDraft,
} from './dataSourceDraft.js';

const schema = {
  name: 'demo',
  fields: [
    { name: 'url', label: '服务地址', required: true, default: '' },
    { name: 'token', label: '访问 Token', required: true, secret: true, default: '' },
    { name: 'timeout_seconds', label: '超时时间', type: 'int', default: 30 },
  ],
};

test('public config never copies a secret value returned by a server', () => {
  assert.deepEqual(publicProviderConfig(schema, {
    url: 'https://example.com',
    token: 'must-not-reach-input-state',
    has_token: true,
  }), {
    url: 'https://example.com',
    has_token: true,
    timeout_seconds: 30,
  });
});

test('direct payload omits empty secrets and all has-secret markers', () => {
  assert.deepEqual(buildDataSourcePayload({
    mode: DATA_SOURCE_MODE.DIRECT_PROVIDER,
    provider: 'demo',
    schema,
    config: { url: 'https://example.com', has_token: true, token: '', timeout_seconds: 30 },
  }), {
    mode: 'direct_provider',
    provider: 'demo',
    config: { url: 'https://example.com', timeout_seconds: 30 },
  });
});

test('existing required secret satisfies validation while a new source must provide it', () => {
  const base = {
    mode: DATA_SOURCE_MODE.DIRECT_PROVIDER,
    provider: 'demo',
    schema,
  };
  assert.match(validateDataSourceDraft({ ...base, config: { url: 'https://example.com' } }), /Token/);
  assert.equal(validateDataSourceDraft({
    ...base,
    config: { url: 'https://example.com', has_token: true },
  }), '');
});

test('load normalization supports shared and direct modes', () => {
  const shared = normalizeDataSourceDraft({
    connectionsPayload: { connections: [{ id: 'c1' }], default_connection_id: 'c1' },
    providersPayload: { providers: [schema] },
    sourcePayload: { configured: false },
  });
  assert.equal(shared.mode, DATA_SOURCE_MODE.CONNECTION_REF);
  assert.equal(shared.connectionId, 'c1');

  const direct = normalizeDataSourceDraft({
    providersPayload: { providers: [schema] },
    sourcePayload: {
      configured: true,
      source: { mode: 'direct_provider', provider: 'demo', config: { url: 'https://example.com', has_token: true } },
    },
  });
  assert.equal(direct.mode, DATA_SOURCE_MODE.DIRECT_PROVIDER);
  assert.deepEqual(direct.config, { url: 'https://example.com', has_token: true, timeout_seconds: 30 });
});

test('workspace and ontology scope plus sequence reject stale responses', () => {
  const request = { scopeKey: dataSourceScopeKey('alpha', 'demo'), sequence: 3 };
  assert.equal(isCurrentDataSourceRequest(request, { ...request }), true);
  assert.equal(isCurrentDataSourceRequest(request, { ...request, sequence: 4 }), false);
  assert.equal(isCurrentDataSourceRequest(request, {
    scopeKey: dataSourceScopeKey('beta', 'demo'),
    sequence: 3,
  }), false);
});
