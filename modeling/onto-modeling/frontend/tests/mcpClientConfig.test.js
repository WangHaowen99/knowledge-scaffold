import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildClientConfigs } from '../src/lib/mcpClientConfig.js';

// All endpoints and credentials below are synthetic fixtures.
function runtime(transports) {
  return {
    urls: transports.flatMap((transport) => ['read', 'change'].map((toolset) => ({
      transport,
      toolset,
      url: `https://example.com/${toolset}/${transport === 'sse' ? 'sse' : 'mcp'}`,
    }))),
  };
}

for (const [transport, type] of [['streamable-http', 'http'], ['sse', 'sse']]) {
  test(`${transport} produces matching read and change configs`, () => {
    const configs = buildClientConfigs(runtime([transport]), '<MCP_TOKEN>');
    const combined = JSON.parse(configs.combined).mcpServers;
    for (const toolset of ['read', 'change']) {
      const server = combined[`modeling-${toolset}`];
      assert.equal(server.type, type);
      assert.equal(server.url, `https://example.com/${toolset}/${type === 'sse' ? 'sse' : 'mcp'}`);
      assert.deepEqual(server.headers, { Authorization: 'Bearer <MCP_TOKEN>' });
      assert.deepEqual(JSON.parse(configs[toolset]).mcpServers, { [`modeling-${toolset}`]: server });
    }
  });
}

test('dual transport keeps HTTP as the default regardless of endpoint order', () => {
  for (const transports of [['sse', 'streamable-http'], ['streamable-http', 'sse']]) {
    const configs = buildClientConfigs(runtime(transports));
    assert.deepEqual(configs, buildClientConfigs(runtime(['streamable-http'])));
  }
});

test('SSE read-only token never exposes change config', () => {
  const configs = buildClientConfigs(runtime(['sse']), '<MCP_TOKEN>', ['ontology.read']);
  assert.equal(configs.change, '');
  assert.deepEqual(Object.keys(JSON.parse(configs.combined).mcpServers), ['modeling-read']);
});

test('change scopes expose only change config for either protocol', () => {
  for (const transport of ['streamable-http', 'sse']) {
    for (const scope of ['ontology.change.validate', 'ontology.change.apply', 'package.publish']) {
      const configs = buildClientConfigs(runtime([transport]), '<MCP_TOKEN>', [scope]);
      assert.equal(configs.read, '');
      assert.deepEqual(Object.keys(JSON.parse(configs.combined).mcpServers), ['modeling-change']);
    }
  }
});

test('missing endpoints or empty permissions do not generate unusable configs', () => {
  const empty = { read: '', change: '', combined: '' };
  assert.deepEqual(buildClientConfigs(null), empty);
  assert.deepEqual(buildClientConfigs(runtime(['sse']), '<MCP_TOKEN>', []), empty);
  assert.deepEqual(buildClientConfigs(runtime(['unsupported'])), empty);
  assert.deepEqual(buildClientConfigs({ urls: [{ toolset: 'read', transport: 'sse', url: '' }] }), empty);
});
