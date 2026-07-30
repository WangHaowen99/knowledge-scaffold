import assert from 'node:assert/strict';
import test from 'node:test';

import {
  acceptsMcpResponse,
  buildMcpClientConfig,
  buildMcpTokenRequest,
  canUseMcpSelfService,
  createMcpScopeState,
  normalizeMcpStatus,
  normalizeMcpTokens,
} from './model.js';

test('MCP scope and sequence reject stale workspace responses', () => {
  assert.equal(acceptsMcpResponse({
    currentScope: '["b"]', requestScope: '["a"]', currentSequence: 2, requestSequence: 2,
  }), false);
  assert.equal(acceptsMcpResponse({
    currentScope: '["a"]', requestScope: '["a"]', currentSequence: 3, requestSequence: 2,
  }), false);
});

test('workspace switch state clears created token plaintext and dialogs', () => {
  const state = createMcpScopeState('["b"]');
  assert.equal(state.createdToken, '');
  assert.equal(state.selectedServiceId, '');
  assert.equal(state.revokeTokenId, '');
});

test('token listing keeps masked metadata and strips plaintext or hashes', () => {
  const tokens = normalizeMcpTokens({ items: [{
    token_id: 'id-1', token: 'secret', token_hash: 'hash', token_masked: 'mcp_***',
    workspace_id: 'alpha', username: 'alice', scopes: ['mcp:chat', 'unknown'], status: 'active',
  }] }, 'alpha');
  assert.deepEqual(tokens[0].scopes, ['mcp:chat']);
  assert.equal('token' in tokens[0], false);
  assert.equal('token_hash' in tokens[0], false);
  assert.equal(normalizeMcpTokens({ items: [{ token_id: 'id-2', workspace_id: 'beta' }] }, 'alpha').length, 0);
});

test('runtime status fails closed on workspace mismatch', () => {
  const status = normalizeMcpStatus({ workspace_id: 'beta', services: [{ id: 'search' }] }, 'alpha');
  assert.equal(status.valid, false);
  assert.deepEqual(status.services, []);
});

test('runtime status requires and preserves a workspace MCP endpoint id', () => {
  const valid = normalizeMcpStatus({
    workspace_id: 'alpha',
    mcp_endpoint_id: 'cdd181f5b6cb47a9',
    services: [{ id: 'search', active_url: 'https://mcp/cdd181f5b6cb47a9/search/mcp' }],
  }, 'alpha');
  const invalid = normalizeMcpStatus({
    workspace_id: 'alpha',
    mcp_endpoint_id: 'invalid',
    services: [],
  }, 'alpha');

  assert.equal(valid.valid, true);
  assert.equal(valid.mcpEndpointId, 'cdd181f5b6cb47a9');
  assert.equal(valid.services[0].active_url, 'https://mcp/cdd181f5b6cb47a9/search/mcp');
  assert.equal(invalid.valid, false);
});

test('MCP is authenticated self service and client config contains only the supplied placeholder', () => {
  assert.equal(canUseMcpSelfService({ authenticated: true, capability: true }), true);
  assert.equal(canUseMcpSelfService({ authenticated: false, capability: true }), false);
  const request = buildMcpTokenRequest({ scopes: ['mcp:chat', 'mcp:chat'], expiresIn: '3600' });
  assert.deepEqual(request.scopes, ['mcp:chat']);
  assert.match(buildMcpClientConfig({ id: 'chat', active_url: 'https://mcp/chat', client_config_type: 'http' }), /<MCP_TOKEN>/);
});
