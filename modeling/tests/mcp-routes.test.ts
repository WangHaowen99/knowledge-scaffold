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
  { createProject },
  { getPackageArtifactStore },
  { insertDocument },
  { issueProjectMcpToken },
] = await Promise.all([
  import('../src/mcp/routes.js'),
  import('../src/mcp/transport.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/documents/repo.js'),
  import('../src/mcp/service.js'),
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
    filename: 'customer.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'ods_customer',
      rowCount: 1,
      fields: [
        { name: 'customer_id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 },
        { name: 'customer_name', inferredType: 'string', sampleValues: ['张三'], distinctRatio: 1 },
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
  assert.equal(status.json().server_name, 'knowledge-scaffold-modeling-mcp');
  assert.equal(status.json().server_version, '0.2.0');
  assert.equal(status.json().token_policy.min_ttl_seconds, 300);
  assert.equal(status.json().scope_catalog.some((item: any) => item.scope === 'ontology.change.review'), true);
  assert.equal(status.json().toolsets[0].tools.includes('list_ontology_objects'), true);
  assert.deepEqual(
    status.json().urls.map((item: any) => `${item.toolset}:${item.transport}`),
    ['read:streamable-http', 'change:streamable-http', 'read:sse', 'change:sse'],
  );

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
    scopes: ['ontology.change.validate', 'ontology.change.apply', 'ontology.change.review'],
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
  assert.equal(discover.json().result.supportedVersions[0], '2026-07-28');

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
      'get_ontology_object_review_status',
      'list_ontology_object_audit_records',
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
  const workingRevision = detail.json().result.structuredContent.working_revision as string;
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
      'validate_ontology_object_patch',
      'apply_ontology_object_patch',
      'submit_ontology_object_review',
      'prepare_ontology_object_confirmation_groups',
      'preflight_ontology_object_confirmation',
      'confirm_ontology_object_review',
      'reject_ontology_object_review',
    ],
  );

  const validated = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'validate_ontology_object_patch'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 4,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'validate_ontology_object_patch',
        arguments: {
          object_id: 'object:customer',
          expected_revision: workingRevision,
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

  const applied = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'apply_ontology_object_patch'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 5,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'apply_ontology_object_patch',
        arguments: {
          object_id: 'object:customer',
          expected_revision: workingRevision,
          client_mutation_id: 'mcp-route-apply-1',
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
  assert.equal(applied.statusCode, 200, applied.body);
  assert.equal(applied.json().result.structuredContent.candidate, true);

  const reviewed = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'submit_ontology_object_review'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 6,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'submit_ontology_object_review',
        arguments: {
          expected_revision: applied.json().result.structuredContent.working_revision,
          client_mutation_id: 'mcp-route-review-1',
        },
      },
    },
  });
  assert.equal(reviewed.statusCode, 200, reviewed.body);
  assert.equal(reviewed.json().result.structuredContent.review_requested, true);

  const prepared = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'prepare_ontology_object_confirmation_groups'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 7,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'prepare_ontology_object_confirmation_groups',
        arguments: {},
      },
    },
  });
  assert.equal(prepared.statusCode, 200, prepared.body);
  assert.deepEqual(
    [...new Set(prepared.json().result.structuredContent.change_groups.flatMap((group: any) => group.impact ?? []))].sort(),
    ['mappings/object-data-mappings.json', 'objects/customer.md'],
  );

  const preflight = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'preflight_ontology_object_confirmation'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 8,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'preflight_ontology_object_confirmation',
        arguments: {
          summary: '确认 MCP 路由写入的对象模型修改',
          change_groups: prepared.json().result.structuredContent.change_groups,
        },
      },
    },
  });
  assert.equal(preflight.statusCode, 200, preflight.body);
  const receipt = preflight.json().result.structuredContent.validation_receipt;

  const confirmed = await app.inject({
    method: 'POST',
    url: `/${changeIssued.endpoint.endpointId}/change/mcp`,
    headers: {
      authorization: `Bearer ${changeIssued.token}`,
      ...modernHeaders('tools/call', 'confirm_ontology_object_review'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 9,
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'confirm_ontology_object_review',
        arguments: {
          summary: '确认 MCP 路由写入的对象模型修改',
          change_groups: prepared.json().result.structuredContent.change_groups,
          expected_candidate_revision: receipt.candidateRevision,
          expected_candidate_hash: receipt.candidateHash,
          expected_validator_version: receipt.validatorVersion,
          review: {
            approved: true,
            review_mode: 'full',
            selected_groups: prepared.json().result.structuredContent.change_groups.map((group: any) => group.id),
          },
        },
      },
    },
  });
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  assert.notEqual(confirmed.json().result.structuredContent.revision, workingRevision);

  const after = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/call', 'get_ontology_object'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 10,
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
      id: 11,
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

  const mismatch = await app.inject({
    method: 'POST',
    url: `/${readIssued.endpoint.endpointId}/read/mcp`,
    headers: {
      authorization: `Bearer ${readIssued.token}`,
      ...modernHeaders('tools/call', 'list_ontology_objects'),
    },
    payload: {
      jsonrpc: '2.0',
      id: 12,
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
      ...modernHeaders('tools/call', 'get_ontology_object_review_status'),
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 'sse-call',
      method: 'tools/call',
      params: {
        ...modernMeta(),
        name: 'get_ontology_object_review_status',
        arguments: { object_id: 'object:customer' },
      },
    }),
  });
  assert.equal(toolCall.status, 202);

  const resultEvent = await sse.nextEvent();
  assert.equal(resultEvent.event, 'message');
  const resultPayload = JSON.parse(resultEvent.data);
  assert.equal(resultPayload.id, 'sse-call');
  assert.equal(resultPayload.result.structuredContent.stage, 'idle');

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
