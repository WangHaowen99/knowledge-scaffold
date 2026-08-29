import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR ||= mkdtempSync(join(tmpdir(), 'modeling-mcp-routes-'));

const [
  { registerProjectMcpManagementRoutes },
  { registerProjectMcpTransportRoutes },
  { createProject, setProjectMember },
  { getPackageArtifactStore },
  { insertDocument },
  { issueProjectMcpToken, markModelingMcpRuntimeRunning, markModelingMcpRuntimeStopped },
  { executeModelingMcpTool },
] = await Promise.all([
  import('../src/mcp/routes.js'),
  import('../src/mcp/transport.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/documents/repo.js'),
  import('../src/mcp/service.js'),
  import('../src/mcp/toolset.js'),
]);

function managementApp(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    (request as any).user = { username, role: 'modeler' };
  });
  registerProjectMcpManagementRoutes(app);
  t.after(() => app.close());
  return app;
}

function transportApp(t: test.TestContext) {
  const app = Fastify();
  registerProjectMcpTransportRoutes(app);
  t.after(() => app.close());
  return app;
}

async function seedProject(projectId: string) {
  insertDocument({
    projectId,
    filename: 'synthetic_records.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'ods_customer',
      rowCount: 1,
      fields: [
        { name: 'customer_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 },
        { name: 'customer_name', inferredType: 'string', sampleValues: ['person_0001'], distinctRatio: 1 },
        { name: 'customer_level', inferredType: 'string', sampleValues: ['A'], distinctRatio: 0.4 },
      ],
    }],
  });

  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(projectId, {
    id: `mcp-routes-${projectId}`,
    name: 'MCP 路由测试',
    description: 'MCP 路由测试基线',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [
        {
          key: 'customer',
          title: '客户',
          description: '客户主数据对象。',
          aliases: ['客户主档'],
          queryBoundary: '只回答客户主数据。',
          sourceRefs: [{ kind: 'table', table: 'ods_customer' }],
          primaryKey: ['customer_id'],
          fields: [
            { key: 'customer_id', cnName: '客户ID', type: 'string', required: true, sourceColumn: 'customer_id' },
            { key: 'customer_name', cnName: '客户名称', type: 'string', sourceColumn: 'customer_name' },
          ],
          relations: [{ relation: '关联订单', target: 'order', note: '客户与订单关联。' }],
        },
        {
          key: 'order',
          title: '订单',
          description: '订单对象。',
          fields: [{ key: 'order_id', cnName: '订单ID', type: 'string', required: true }],
          primaryKey: ['order_id'],
        },
      ],
      operations: [],
    },
    coverage: initialized.coverage,
    note: '写入 MCP 路由测试基线',
    action: {
      cause: 'mcp_routes_seeded',
      summary: '写入 MCP 路由测试基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

function modernHeaders(method: string, toolName = '') {
  return {
    'mcp-protocol-version': '2026-07-28',
    'mcp-method': method,
    ...(toolName ? { 'mcp-name': toolName } : {}),
    accept: 'application/json, text/event-stream',
  };
}

function modernMeta() {
  return {
    _meta: {
      'io.modelcontextprotocol/protocolVersion': '2026-07-28',
      'io.modelcontextprotocol/clientCapabilities': {},
      'io.modelcontextprotocol/clientInfo': { name: 'synthetic-test-client', version: '1.0.0' },
    },
  };
}

function createSseReader(stream: ReadableStream<Uint8Array>) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  return {
    async nextEvent(timeoutMs = 2000): Promise<{ event: string; data: string }> {
      while (true) {
        const marker = buffer.indexOf('\n\n');
        if (marker >= 0) {
          const chunk = buffer.slice(0, marker);
          buffer = buffer.slice(marker + 2);
          const lines = chunk.split('\n');
          let event = 'message';
          const dataLines: string[] = [];
          for (const line of lines) {
            if (line.startsWith('event:')) event = line.slice('event:'.length).trim();
            if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).trimStart());
          }
          return { event, data: dataLines.join('\n') };
        }
        const result = await Promise.race([
          reader.read(),
          new Promise<never>((_, reject) => setTimeout(() => reject(new Error('等待 SSE 事件超时')), timeoutMs)),
        ]);
        if (result.done) throw new Error('SSE 连接提前结束');
        buffer += decoder.decode(result.value, { stream: true }).replace(/\r\n/gu, '\n');
      }
    },
    close() {
      return reader.cancel();
    },
  };
}

test('项目 MCP 管理接口可查询状态并管理 token', async (t) => {
  const project = createProject({ owner: 'alice', name: 'MCP 管理路由', runtime: 'agent' });
  const app = managementApp(t);
  await seedProject(project.id);

  const status = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/mcp/runtime/status`,
  });
  assert.equal(status.statusCode, 200, status.body);
  assert.match(status.json().endpoint_id, /^[0-9a-f]{16}$/u);
  assert.equal(status.json().transport, 'both');
  assert.equal(status.json().configured, true);
  assert.equal(status.json().running, false);
  assert.equal(status.json().server_name, 'knowledge-scaffold-modeling-mcp');
  assert.equal(status.json().server_version, '0.2.0');
  assert.equal(status.json().token_policy.min_ttl_seconds, 300);
  assert.equal(status.json().scope_catalog.some((item: any) => item.scope === 'ontology.change.review'), false);
  assert.equal(status.json().toolsets[0].tools.includes('list_ontology_objects'), true);
  assert.deepEqual(
    status.json().urls.map((item: any) => `${item.toolset}:${item.transport}`),
    ['read:streamable-http', 'change:streamable-http', 'read:sse', 'change:sse'],
  );

  markModelingMcpRuntimeRunning('synthetic-mcp-instance', 'synthetic heartbeat');
  const runningStatus = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/mcp/runtime/status`,
  });
  assert.equal(runningStatus.statusCode, 200, runningStatus.body);
  assert.equal(runningStatus.json().running, true);
  assert.equal(runningStatus.json().enabled, true);
  markModelingMcpRuntimeStopped('synthetic-mcp-instance', 'synthetic stop');

  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/mcp/tokens`,
    payload: {
      name: '建模只读 token',
      scopes: ['ontology.read'],
      expires_in_seconds: 3600,
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  assert.ok(created.json().token);
  assert.equal(created.json().token_summary.scopes[0], 'ontology.read');
  assert.equal(created.json().endpoint_id, status.json().endpoint_id);

  const emptyScopes = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/mcp/tokens`,
    payload: { name: 'empty scopes', scopes: [], expires_in_seconds: 3600 },
  });
  assert.equal(emptyScopes.statusCode, 400, emptyScopes.body);
  assert.equal(emptyScopes.json().code, 'MCP_SCOPE_INVALID');

  const listed = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/mcp/tokens`,
  });
  assert.equal(listed.statusCode, 200, listed.body);
  assert.equal(listed.json().tokens.length, 1);
  assert.equal(listed.json().tokens[0].name, '建模只读 token');

  const removed = await app.inject({
    method: 'DELETE',
    url: `/api/projects/${project.id}/mcp/tokens/${listed.json().tokens[0].id}`,
  });
  assert.equal(removed.statusCode, 200, removed.body);

  const after = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/mcp/tokens`,
  });
  assert.equal(after.statusCode, 200, after.body);
  assert.equal(after.json().tokens.length, 0);
});

test('项目 MCP transport 支持 read/change 工具和 legacy initialize', async (t) => {
  const project = createProject({ owner: 'alice', name: 'MCP transport', runtime: 'agent' });
  const app = transportApp(t);
  await seedProject(project.id);

  const readIssued = await issueProjectMcpToken({
    projectId: project.id,
    name: 'read token',
    subjectUsername: 'alice',
    issuedBy: 'alice',
    scopes: ['ontology.read'],
    ttlSeconds: 3600,
  });
  const changeIssued = await issueProjectMcpToken({
    projectId: project.id,
    name: 'change token',
    subjectUsername: 'alice',
    issuedBy: 'alice',
    scopes: ['ontology.change.validate', 'ontology.change.apply'],
    ttlSeconds: 3600,
  });

  const discover = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('server/discover'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 1,
      method: 'server/discover',
      params: modernMeta(),
    },
  });
  assert.equal(discover.statusCode, 200, discover.body);
  assert.equal(discover.json().result.resultType, 'complete');
  assert.equal(discover.json().result.supportedVersions[0], '2026-07-28');
  assert.equal(discover.json().result._meta['io.modelcontextprotocol/serverInfo'].name, 'knowledge-scaffold-modeling-mcp');

  const sdkCompatibleDiscover = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      accept: 'application/json, text/event-stream',
    },
    payload: {
      jsonrpc: '2.0',
      id: 'sdk-compatible-discover',
      method: 'server/discover',
      params: modernMeta(),
    },
  });
  assert.equal(sdkCompatibleDiscover.statusCode, 200, sdkCompatibleDiscover.body);

  const toolsList = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/list'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 2,
      method: 'tools/list',
      params: modernMeta(),
    },
  });
  assert.equal(toolsList.statusCode, 200, toolsList.body);
  assert.deepEqual(
    toolsList.json().result.tools.map((tool: any) => tool.name),
    [
      'list_ontology_objects',
      'get_ontology_object',
    ],
  );

  const detail = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/call', 'get_ontology_object'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 3,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'get_ontology_object',
        arguments: { object_id: 'object:customer' },
      },
    },
  });
  assert.equal(detail.statusCode, 200, detail.body);
  assert.equal(detail.json().result.structuredContent.object.key, 'customer');
  const revision = detail.json().result.structuredContent.revision as string;
  const relationId = detail.json().result.structuredContent.object.relations[0].id as string;

  const changeToolsList = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/list'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 'change-tools',
      method: 'tools/list',
      params: modernMeta(),
    },
  });
  assert.equal(changeToolsList.statusCode, 200, changeToolsList.body);
  assert.deepEqual(
    changeToolsList.json().result.tools.map((tool: any) => tool.name),
    [
      'validate_ontology_object_change',
      'update_ontology_object',
    ],
  );

  const validated = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'validate_ontology_object_change'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 4,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'validate_ontology_object_change',
        arguments: {
          object_id: 'object:customer',
          expected_revision: revision,
          metadata: {
            aliases: ['客户主档', '客户实体'],
          },
          field_ops: {
            add: [{ key: 'customer_level', cnName: '客户等级', type: 'string', sourceColumn: 'customer_level' }],
          },
          relation_ops: {
            update: [{ relationId, changes: { label: '客户订单' } }],
          },
        },
      },
    },
  });
  assert.equal(validated.statusCode, 200, validated.body);
  assert.equal(validated.json().result.structuredContent.ok, true);
  assert.equal(validated.json().result.structuredContent.resource.id, 'object:customer');

  const updated = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'update_ontology_object'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 5,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'update_ontology_object',
        arguments: {
          object_id: 'object:customer',
          expected_revision: revision,
          metadata: {
            aliases: ['客户主档', '客户实体'],
          },
          field_ops: {
            add: [{ key: 'customer_level', cnName: '客户等级', type: 'string', sourceColumn: 'customer_level' }],
          },
          relation_ops: {
            update: [{ relationId, changes: { label: '客户订单' } }],
          },
        },
      },
    },
  });
  assert.equal(updated.statusCode, 200, updated.body);
  assert.notEqual(updated.json().result.structuredContent.revision, revision);
  assert.equal(updated.json().result.structuredContent.resource.id, 'object:customer');
  assert.equal(updated.json().result.structuredContent.object.fields.length, 3);

  const after = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/call', 'get_ontology_object'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 6,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'get_ontology_object',
        arguments: { object_id: 'object:customer' },
      },
    },
  });
  assert.equal(after.statusCode, 200, after.body);
  assert.deepEqual(after.json().result.structuredContent.object.metadata.aliases, ['客户主档', '客户实体']);
  assert.equal(after.json().result.structuredContent.object.fields.length, 3);
  assert.equal(after.json().result.structuredContent.object.relations[0].label, '客户订单');

  const legacyInitialize = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
    },
    payload: {
      jsonrpc: '2.0',
      id: 7,
      method: 'initialize',
      params: {
        protocolVersion: '2025-11-25',
        capabilities: {},
        clientInfo: { name: 'legacy-client', version: '1.0.0' },
      },
    },
  });
  assert.equal(legacyInitialize.statusCode, 200, legacyInitialize.body);
  assert.equal(legacyInitialize.json().result.protocolVersion, '2025-11-25');

  const legacyListObjects = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      'mcp-protocol-version': '2025-11-25',
      accept: 'application/json, text/event-stream',
    },
    payload: {
      jsonrpc: '2.0',
      id: 'legacy-list-objects',
      method: 'tools/call',
      params: {
        name: 'list_ontology_objects',
        arguments: { query: 'customer', limit: 10 },
      },
    },
  });
  assert.equal(legacyListObjects.statusCode, 200, legacyListObjects.body);
  const legacyListResult = legacyListObjects.json().result;
  assert.equal(legacyListResult.isError, false);
  const legacyListText = JSON.parse(legacyListResult.content[0].text);
  assert.equal(legacyListText.revision, legacyListResult.structuredContent.revision);
  assert.equal(legacyListText.total, 1);
  assert.equal(legacyListText.items[0].object_id, 'object:customer');

  const legacyMissingObject = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      'mcp-protocol-version': '2025-11-25',
      accept: 'application/json, text/event-stream',
    },
    payload: {
      jsonrpc: '2.0',
      id: 'legacy-missing-object',
      method: 'tools/call',
      params: {
        name: 'get_ontology_object',
        arguments: { object_id: 'object:missing' },
      },
    },
  });
  assert.equal(legacyMissingObject.statusCode, 200, legacyMissingObject.body);
  const legacyErrorResult = legacyMissingObject.json().result;
  assert.equal(legacyErrorResult.isError, true);
  const legacyErrorText = JSON.parse(legacyErrorResult.content[0].text);
  assert.equal(legacyErrorText.code, 'RESOURCE_NOT_FOUND');
  assert.equal(legacyErrorText.code, legacyErrorResult.structuredContent.code);

  const mismatch = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/call', 'list_ontology_objects'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 8,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'get_ontology_object',
        arguments: { object_id: 'object:customer' },
      },
    },
  });
  assert.equal(mismatch.statusCode, 400, mismatch.body);
  assert.match(mismatch.json().error.message, /Mcp-Name/u);

  const missingCapabilities = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/list'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 9,
      method: 'tools/list',
      params: {
        _meta: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28' },
      },
    },
  });
  assert.equal(missingCapabilities.statusCode, 400, missingCapabilities.body);
  assert.match(missingCapabilities.json().error.message, /clientCapabilities/u);

  const forbiddenOrigin = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      origin: 'https://example.org',
      ...modernHeaders('tools/list'),
    },
    payload: { jsonrpc: '2.0', id: 14, method: 'tools/list', params: modernMeta() },
  });
  assert.equal(forbiddenOrigin.statusCode, 403, forbiddenOrigin.body);
  assert.equal(forbiddenOrigin.json().error.data.code, 'MCP_ORIGIN_FORBIDDEN');
});

test('项目 Viewer 可读取 MCP runtime，但不能读取或管理 token', async (t) => {
  const project = createProject({ owner: 'owner_a', name: 'MCP Viewer', runtime: 'agent' });
  setProjectMember(project.id, project.owner, 'viewer_a', 'viewer');
  await seedProject(project.id);
  const app = managementApp(t, 'viewer_a');

  const status = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/mcp/runtime/status`,
  });
  assert.equal(status.statusCode, 200, status.body);
  assert.ok(status.json().endpoint_id);
  assert.ok(status.json().urls.length > 0);

  const tokens = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/mcp/tokens`,
  });
  assert.equal(tokens.statusCode, 404, tokens.body);
});

test('同一用户轮换 MCP token 后仍可继续按最新 revision 修改对象', async () => {
  const project = createProject({ owner: 'alice', name: 'MCP token 轮换', runtime: 'agent' });
  await seedProject(project.id);
  const listed = await executeModelingMcpTool({
    toolName: 'list_ontology_objects',
    project,
    username: 'alice',
    tokenId: 'token-before-rotation',
    argumentsValue: {},
  });
  const first = await executeModelingMcpTool({
    toolName: 'update_ontology_object',
    project,
    username: 'alice',
    tokenId: 'token-before-rotation',
    argumentsValue: {
      object_id: 'object:customer',
      expected_revision: listed.revision,
      metadata: { aliases: ['客户主档', 'synthetic_alias_a'] },
    },
  });
  const second = await executeModelingMcpTool({
    toolName: 'update_ontology_object',
    project,
    username: 'alice',
    tokenId: 'token-after-rotation',
    argumentsValue: {
      object_id: 'object:customer',
      expected_revision: first.revision,
      metadata: { aliases: ['客户主档', 'synthetic_alias_b'] },
    },
  });
  assert.notEqual(second.revision, first.revision);
  assert.deepEqual(second.object.metadata.aliases, ['客户主档', 'synthetic_alias_b']);
});

test('项目 MCP transport 支持 SSE 握手、消息投递和 session 绑定', async (t) => {
  const project = createProject({ owner: 'alice', name: 'MCP SSE transport', runtime: 'agent' });
  const app = transportApp(t);
  await seedProject(project.id);

  const issued = await issueProjectMcpToken({
    projectId: project.id,
    name: 'sse read token',
    subjectUsername: 'alice',
    issuedBy: 'alice',
    scopes: ['ontology.read'],
    ttlSeconds: 3600,
  });

  const address = await app.listen({ host: '127.0.0.1', port: 0 });
  const aborter = new AbortController();
  t.after(async () => {
    aborter.abort();
  });

  const sseResponse = await fetch(`${address}/${issued.endpoint.endpointId}/read/sse`, {
    headers: {
      authorization: `Bearer ${issued.token}`,
      accept: 'text/event-stream',
    },
    signal: aborter.signal,
  });
  assert.equal(sseResponse.status, 200);
  assert.match(String(sseResponse.headers.get('content-type') ?? ''), /^text\/event-stream/u);
  assert.ok(sseResponse.body);

  const sse = createSseReader(sseResponse.body!);
  const endpointEvent = await sse.nextEvent();
  assert.equal(endpointEvent.event, 'endpoint');
  assert.match(
    endpointEvent.data,
    new RegExp(`^/${issued.endpoint.endpointId}/read/sse/messages/\\?session_id=[0-9a-f]{32}$`, 'u'),
  );

  const messageUrl = new URL(endpointEvent.data, address);
  const sessionId = messageUrl.searchParams.get('session_id');
  assert.match(String(sessionId ?? ''), /^[0-9a-f]{32}$/u);

  const initialize = await fetch(messageUrl, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${issued.token}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 'sse-init',
      method: 'initialize',
      params: {
        protocolVersion: '2025-11-25',
        capabilities: {},
        clientInfo: { name: 'sse-client', version: '1.0.0' },
      },
    }),
  });
  assert.equal(initialize.status, 202);

  const initializedEvent = await sse.nextEvent();
  assert.equal(initializedEvent.event, 'message');
  assert.equal(JSON.parse(initializedEvent.data).result.protocolVersion, '2025-11-25');

  const toolCall = await fetch(messageUrl, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${issued.token}`,
      'content-type': 'application/json',
      ...modernHeaders('tools/call', 'get_ontology_object'),
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 'sse-call',
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'get_ontology_object',
        arguments: { object_id: 'object:customer' },
      },
    }),
  });
  assert.equal(toolCall.status, 202);

  const resultEvent = await sse.nextEvent();
  assert.equal(resultEvent.event, 'message');
  const resultPayload = JSON.parse(resultEvent.data);
  assert.equal(resultPayload.id, 'sse-call');
  assert.equal(resultPayload.result.structuredContent.object.key, 'customer');

  const wrongEndpoint = await fetch(
    `${address}/ffffffffffffffff/read/sse/messages/?session_id=${encodeURIComponent(String(sessionId ?? ''))}`,
    {
      method: 'POST',
      headers: {
        authorization: `Bearer ${issued.token}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 'wrong-endpoint',
        method: 'initialize',
        params: {
          protocolVersion: '2025-11-25',
          capabilities: {},
          clientInfo: { name: 'sse-client', version: '1.0.0' },
        },
      }),
    },
  );
  assert.equal(wrongEndpoint.status, 404);
  assert.match((await wrongEndpoint.json()).error.message, /Session not found/u);

  await sse.close().catch(() => undefined);
});
