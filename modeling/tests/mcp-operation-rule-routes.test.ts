import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';
import { Client, SSEClientTransport, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-operation-rule-routes-'));
process.env.MODELING_MCP_OPERATION_RULE_ENABLED = 'true';

const [
  { registerProjectMcpTransportRoutes },
  { createProject },
  { getPackageArtifactStore },
  { issueProjectMcpToken },
] = await Promise.all([
  import('../src/mcp/transport.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/mcp/service.js'),
]);

function headers(method: string, toolName = '') {
  return {
    'mcp-protocol-version': '2026-07-28',
    'mcp-method': method,
    ...(toolName ? { 'mcp-name': toolName } : {}),
    accept: 'application/json, text/event-stream',
  };
}

function meta() {
  return {
    _meta: {
      'io.modelcontextprotocol/protocolVersion': '2026-07-28',
      'io.modelcontextprotocol/clientCapabilities': {},
      'io.modelcontextprotocol/clientInfo': { name: 'synthetic-route-client', version: '1.0.0' },
    },
  };
}

async function seed(projectId: string) {
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(projectId, {
    id: `synthetic-${projectId}`,
    name: '合成路由包',
    description: 'MCP transport 合成测试',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      objects: [{
        key: 'record',
        title: '记录',
        description: '合成记录对象。',
        fields: [{ key: 'record_id', type: 'string', required: true }],
      }],
    },
    coverage: initial.coverage,
    action: {
      cause: 'mcp_route_test_seed',
      summary: '建立合成路由基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

async function rpc(
  app: ReturnType<typeof Fastify>,
  url: string,
  token: string,
  id: number,
  method: string,
  params: Record<string, unknown>,
  toolName = '',
) {
  return app.inject({
    method: 'POST',
    url,
    headers: { authorization: `Bearer ${token}`, ...headers(method, toolName) },
    payload: { jsonrpc: '2.0', id, method, params: { ...meta(), ...params } },
  });
}

test('Operation/Rule 与 Object 共用 Modeling MCP toolset 和 scope 门禁', async (t) => {
  const project = createProject({ owner: 'operator_a', name: 'MCP route integration', runtime: 'agent' });
  const seeded = await seed(project.id);
  const app = Fastify();
  registerProjectMcpTransportRoutes(app);
  t.after(() => app.close());

  const read = await issueProjectMcpToken({
    projectId: project.id,
    name: 'synthetic read',
    subjectUsername: project.owner,
    issuedBy: project.owner,
    scopes: ['ontology.read'],
    ttlSeconds: 3600,
  });
  const validate = await issueProjectMcpToken({
    projectId: project.id,
    name: 'synthetic validate',
    subjectUsername: project.owner,
    issuedBy: project.owner,
    scopes: ['ontology.change.validate'],
    ttlSeconds: 3600,
  });
  const apply = await issueProjectMcpToken({
    projectId: project.id,
    name: 'synthetic apply',
    subjectUsername: project.owner,
    issuedBy: project.owner,
    scopes: ['ontology.change.apply'],
    ttlSeconds: 3600,
  });
  const readUrl = `/${read.endpoint.endpointId}/read/mcp`;
  const changeUrl = `/${read.endpoint.endpointId}/change/mcp`;

  const listed = await rpc(app, readUrl, read.token, 1, 'tools/list', {});
  assert.equal(listed.statusCode, 200, listed.body);
  const readTools = listed.json().result.tools.map((item: any) => item.name);
  assert.ok(readTools.includes('list_ontology_objects'));
  assert.ok(readTools.includes('list_ontology_operations'));
  assert.ok(readTools.includes('get_ontology_rule'));

  const validation = await rpc(
    app,
    changeUrl,
    validate.token,
    2,
    'tools/call',
    {
      name: 'validate_ontology_operation_rule_change',
      arguments: {
        expected_revision: seeded.revision.oid,
        resource_type: 'rule',
        action: 'create',
        resource_id: 'rule:record_valid',
        payload: { title: '记录有效规则', content: '记录编号存在时有效。', applies_to_objects: ['record'] },
      },
    },
    'validate_ontology_operation_rule_change',
  );
  assert.equal(validation.statusCode, 200, validation.body);
  assert.equal(validation.json().result.structuredContent.ok, true);

  const validateCannotWrite = await rpc(
    app,
    changeUrl,
    validate.token,
    3,
    'tools/call',
    {
      name: 'create_ontology_rule',
      arguments: {
        expected_revision: seeded.revision.oid,
        key: 'record_valid',
        title: '记录有效规则',
        content: '记录编号存在时有效。',
        applies_to_objects: ['record'],
      },
    },
    'create_ontology_rule',
  );
  assert.equal(validateCannotWrite.statusCode, 403, validateCannotWrite.body);
  assert.equal(validateCannotWrite.json().error.data.code, 'MCP_SCOPE_FORBIDDEN');

  const created = await rpc(
    app,
    changeUrl,
    apply.token,
    4,
    'tools/call',
    {
      name: 'create_ontology_rule',
      arguments: {
        expected_revision: seeded.revision.oid,
        key: 'record_valid',
        title: '记录有效规则',
        content: '记录编号存在时有效。',
        applies_to_objects: ['record'],
      },
    },
    'create_ontology_rule',
  );
  assert.equal(created.statusCode, 200, created.body);
  assert.notEqual(created.json().result.structuredContent.revision, seeded.revision.oid);
});

test('官方 MCP v2 Client 可发现并调用 Operation/Rule 工具', async (t) => {
  const project = createProject({ owner: 'operator_b', name: 'Official MCP client', runtime: 'agent' });
  await seed(project.id);
  const issued = await issueProjectMcpToken({
    projectId: project.id,
    name: 'synthetic official client',
    subjectUsername: project.owner,
    issuedBy: project.owner,
    scopes: ['ontology.read'],
    ttlSeconds: 3600,
  });
  const app = Fastify();
  registerProjectMcpTransportRoutes(app);
  const address = await app.listen({ host: '127.0.0.1', port: 0 });
  const client = new Client(
    { name: 'synthetic-official-client', version: '1.0.0' },
    { capabilities: {}, versionNegotiation: { mode: { pin: '2026-07-28' } } },
  );
  const transport = new StreamableHTTPClientTransport(
    new URL(`${address}/${issued.endpoint.endpointId}/read/mcp`),
    { authProvider: { token: async () => issued.token } },
  );
  t.after(async () => {
    await client.close().catch(() => undefined);
    await app.close();
  });

  await client.connect(transport);
  const tools = await client.listTools();
  assert.ok(tools.tools.some((tool) => tool.name === 'list_ontology_objects'));
  assert.ok(tools.tools.some((tool) => tool.name === 'list_ontology_operations'));
  const result = await client.callTool({ name: 'list_ontology_operations', arguments: {} });
  assert.equal(result.isError, undefined);
  assert.ok(result.structuredContent && typeof result.structuredContent === 'object');

  const sseClient = new Client(
    { name: 'synthetic-official-sse-client', version: '1.0.0' },
    { capabilities: {} },
  );
  const sseTransport = new SSEClientTransport(
    new URL(`${address}/${issued.endpoint.endpointId}/read/sse`),
    { authProvider: { token: async () => issued.token } },
  );
  t.after(async () => sseClient.close().catch(() => undefined));
  await sseClient.connect(sseTransport);
  const sseTools = await sseClient.listTools();
  assert.ok(sseTools.tools.some((tool) => tool.name === 'list_ontology_operations'));
});
