import assert from 'node:assert/strict';
import test from 'node:test';

import {
  acceptsMcpResponse,
  buildMcpClientConfig,
  buildMcpTokenRequest,
  canUseMcpSelfService,
  createMcpScopeState,
  mcpOntologyEligibility,
  normalizeMcpOntologies,
  normalizeMcpStatus,
  normalizeMcpTokens,
  resolveMcpOntologySelection,
} from './model.js';

test('MCP scope and sequence reject stale workspace responses', () => {
  assert.equal(
    acceptsMcpResponse({
      currentScope: '["b"]',
      requestScope: '["a"]',
      currentSequence: 2,
      requestSequence: 2,
    }),
    false,
  );
  assert.equal(
    acceptsMcpResponse({
      currentScope: '["a"]',
      requestScope: '["a"]',
      currentSequence: 3,
      requestSequence: 2,
    }),
    false,
  );
});

test('workspace switch state clears created token plaintext and dialogs', () => {
  const state = createMcpScopeState('["b"]');
  assert.equal(state.createdToken, '');
  assert.equal(state.selectedServiceId, '');
  assert.equal(state.revokeTokenId, '');
});

test('token listing keeps masked metadata and strips plaintext or hashes', () => {
  const tokens = normalizeMcpTokens(
    {
      items: [
        {
          token_id: 'id-1',
          token: '<MCP_TOKEN>',
          token_hash: 'sha256:synthetic',
          token_masked: 'mcp_***',
          workspace_id: 'workspace_demo',
          username: 'operator_a',
          ontology_id: 'knowledge_package_demo',
          scopes: ['mcp:chat', 'unknown'],
          status: 'active',
        },
      ],
    },
    'workspace_demo',
  );
  assert.deepEqual(tokens[0].scopes, ['mcp:chat']);
  assert.equal(tokens[0].ontology_id, 'knowledge_package_demo');
  assert.equal(tokens[0].binding_mode, 'ontology');
  assert.equal('token' in tokens[0], false);
  assert.equal('token_hash' in tokens[0], false);
  assert.equal(
    normalizeMcpTokens(
      { items: [{ token_id: 'id-2', workspace_id: 'beta' }] },
      'alpha',
    ).length,
    0,
  );
});

test('runtime status fails closed on workspace mismatch', () => {
  const status = normalizeMcpStatus(
    { workspace_id: 'beta', services: [{ id: 'search' }] },
    'alpha',
  );
  assert.equal(status.valid, false);
  assert.deepEqual(status.services, []);
});

test('runtime status requires and preserves a workspace MCP endpoint id', () => {
  const valid = normalizeMcpStatus(
    {
      workspace_id: 'alpha',
      mcp_endpoint_id: 'cdd181f5b6cb47a9',
      services: [
        { id: 'search', active_url: 'https://service.example.net/cdd181f5b6cb47a9/search/mcp' },
      ],
    },
    'alpha',
  );
  const invalid = normalizeMcpStatus(
    {
      workspace_id: 'alpha',
      mcp_endpoint_id: 'invalid',
      services: [],
    },
    'alpha',
  );

  assert.equal(valid.valid, true);
  assert.equal(valid.mcpEndpointId, 'cdd181f5b6cb47a9');
  assert.equal(
    valid.services[0].active_url,
    'https://service.example.net/cdd181f5b6cb47a9/search/mcp',
  );
  assert.equal(invalid.valid, false);
});

test('MCP is authenticated self service and client config contains only the supplied placeholder', () => {
  assert.equal(
    canUseMcpSelfService({ authenticated: true, capability: true }),
    true,
  );
  assert.equal(
    canUseMcpSelfService({ authenticated: false, capability: true }),
    false,
  );
  const request = buildMcpTokenRequest({
    scopes: ['mcp:chat', 'mcp:chat'],
    expiresIn: '3600',
    ontologyId: 'knowledge_package_demo',
  });
  assert.deepEqual(request.scopes, ['mcp:chat']);
  assert.equal(request.ontology_id, 'knowledge_package_demo');
  assert.equal('active_version' in request, false);
  assert.match(
    buildMcpClientConfig({
      id: 'chat',
      active_url: 'https://service.example.net/chat',
      client_config_type: 'http',
    }),
    /<MCP_TOKEN>/,
  );
});

test('ontology selection follows scope capabilities and only auto-selects a unique eligible package', () => {
  const ontologies = normalizeMcpOntologies({
    ontologies: [
      {
        id: 'knowledge_package_graph',
        name: 'Graph package',
        enabled: true,
        active_version: '1.0.0',
        versions: ['1.0.0'],
        business_data: { ready: true, configured: true, provider: 'neo4j' },
      },
      {
        id: 'knowledge_package_chat',
        name: 'Chat package',
        enabled: true,
        active_version: '2.0.0',
        versions: ['2.0.0'],
        business_data: { ready: false, configured: false, provider: '' },
      },
    ],
  });

  assert.equal(
    mcpOntologyEligibility(ontologies[1], ['mcp:search']).eligible,
    false,
  );
  assert.equal(
    mcpOntologyEligibility(ontologies[1], ['mcp:chat']).eligible,
    true,
  );
  assert.equal(
    resolveMcpOntologySelection({ ontologies, scopes: ['mcp:search'] }),
    'knowledge_package_graph',
  );
  assert.equal(
    resolveMcpOntologySelection({ ontologies, scopes: ['mcp:chat'] }),
    '',
  );
});
