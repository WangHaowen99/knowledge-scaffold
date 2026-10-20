import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify from 'fastify';

const data = mkdtempSync(join(tmpdir(), 'synthetic-association-'));
process.env.DATA_DIR = data;
process.env.MODELING_SERVICE_TOKEN = 'synthetic-association-credential-' + 'x'.repeat(32);
process.env.MODELING_MCP_TOKEN_SECRET = 'synthetic-mcp-signing-secret-' + 'x'.repeat(32);
const { requireAuth } = await import('../src/auth/middleware.js');
const { getAuthClient } = await import('../src/auth/client.js');
const { createProject, setProjectMember, listProjectMembers } = await import('../src/projects/repo.js');
const { registerProjectMcpManagementRoutes } = await import('../src/mcp/routes.js');
const { verifyProjectMcpToken, listProjectMcpTokens } = await import('../src/mcp/service.js');
const app = Fastify();
app.addHook('preHandler', requireAuth);
for (const [method, url] of [
  ['POST', '/api/projects'], ['GET', '/api/projects/by-external-id'],
  ['PUT', '/api/projects/:id/members/sync'], ['DELETE', '/api/projects/:id'],
  ['GET', '/api/unrelated'], ['PUT', '/api/projects/:id/mcp/tokens'],
] as const) app.route({ method, url, handler: (req) => ({ user: req.user }) });
registerProjectMcpManagementRoutes(app);
const linked = createProject({ owner: 'operator_a', name: 'Synthetic linked', externalId: 'onto:synthetic-project' });
const ordinary = createProject({ owner: 'operator_a', name: 'Synthetic ordinary' });
const headers = {
  'x-modeling-service-token': process.env.MODELING_SERVICE_TOKEN,
  'x-onto-username': 'operator_a', 'x-onto-project-id': 'onto:synthetic-project',
};
after(async () => { await app.close(); rmSync(data, { recursive: true, force: true }); });

test('service credential is limited to matching Onto project and exact methods', async () => {
  assert.equal((await app.inject({ url: `/api/projects/${linked.id}/mcp/runtime/status`, headers })).statusCode, 200);
  for (const url of [`/api/projects/${ordinary.id}/mcp/runtime/status`, '/api/unrelated']) {
    assert.equal((await app.inject({ url, headers })).statusCode, 403);
  }
  assert.equal((await app.inject({ method: 'PUT', url: `/api/projects/${linked.id}/mcp/tokens`, headers })).statusCode, 403);
  assert.equal((await app.inject({ url: `/api/projects/${linked.id}/mcp/runtime/status`, headers: { ...headers, 'x-onto-project-id': 'onto:another-project' } })).statusCode, 403);
});

test('creation and lookup require matching external id and explicit identity', async () => {
  for (const external_id of ['ordinary', 'onto:another-project', '']) {
    assert.equal((await app.inject({ method: 'POST', url: '/api/projects', headers, payload: { external_id } })).statusCode, 403);
  }
  assert.equal((await app.inject({ method: 'POST', url: '/api/projects', headers, payload: { external_id: headers['x-onto-project-id'] } })).statusCode, 200);
  assert.equal((await app.inject({ url: '/api/projects/by-external-id?external_id=ordinary', headers })).statusCode, 403);
  assert.equal((await app.inject({ method: 'POST', url: '/api/projects', headers: { ...headers, 'x-onto-username': '' }, payload: { external_id: headers['x-onto-project-id'] } })).statusCode, 403);
});

test('cleanup requires exact linkage and rejects wrong owner; missing projects remain idempotent', async () => {
  const url = `/api/projects/${linked.id}`;
  assert.equal((await app.inject({ method: 'DELETE', url, headers, payload: { external_id: 'onto:wrong' } })).statusCode, 403);
  assert.equal((await app.inject({ method: 'DELETE', url, headers: { ...headers, 'x-onto-username': 'operator_b' }, payload: { external_id: headers['x-onto-project-id'] } })).statusCode, 403);
  assert.equal((await app.inject({ method: 'DELETE', url, headers: { ...headers, 'x-onto-username': '' }, payload: { external_id: headers['x-onto-project-id'] } })).statusCode, 200);
  assert.equal((await app.inject({ method: 'DELETE', url: '/api/projects/missing', headers })).statusCode, 404);
});

test('invalid credentials never authenticate, including unequal UTF-8 byte lengths', async () => {
  for (const token of ['', 'invalid', 'é'.repeat(process.env.MODELING_SERVICE_TOKEN!.length)]) {
    assert.equal((await app.inject({ url: `/api/projects/${linked.id}/mcp/runtime/status`, headers: { ...headers, 'x-modeling-service-token': token } })).statusCode, 401);
  }
});

test('owner and delegated admin can manage usable MCP tokens with separate issuer audit', async () => {
  const membersBefore = listProjectMembers(linked.id);
  for (const username of ['operator_a', 'operator_admin']) {
    const actorHeaders = { ...headers, 'x-onto-username': username,
      'x-onto-role': username === 'operator_admin' ? 'admin' : 'user' };
    const base = `/api/projects/${linked.id}/mcp`;
    assert.equal((await app.inject({ url: `${base}/runtime/status`, headers: actorHeaders })).statusCode, 200);
    assert.equal((await app.inject({ url: `${base}/tokens`, headers: actorHeaders })).statusCode, 200);
    const issued = await app.inject({ method: 'POST', url: `${base}/tokens`, headers: actorHeaders,
      payload: { name: 'Synthetic token', scopes: ['ontology.read'] } });
    assert.equal(issued.statusCode, 201, issued.body);
    const result = issued.json();
    assert.equal(result.token_summary.issuedBy, username);
    assert.equal(result.token_summary.subjectUsername, 'operator_a');
    const verified = await verifyProjectMcpToken({ bearerToken: result.token,
      endpointId: result.endpoint_id, minimumAccess: 'read' });
    assert.equal(verified.projectId, linked.id);
    const revoked = await app.inject({ method: 'DELETE', url: `${base}/tokens/${result.token_summary.id}`,
      headers: actorHeaders });
    assert.equal(revoked.statusCode, 200);
    assert.equal(listProjectMcpTokens(linked.id, { includeRevoked: true })
      .find((token) => token.id === result.token_summary.id)?.revokedBy, username);
    await assert.rejects(verifyProjectMcpToken({ bearerToken: result.token,
      endpointId: result.endpoint_id, minimumAccess: 'read' }),
    (error: any) => error.code === 'MCP_TOKEN_REVOKED');
  }
  assert.deepEqual(listProjectMembers(linked.id), membersBefore);
});

test('viewer cannot issue tokens and delegated admin cannot cross project boundaries', async () => {
  setProjectMember(linked.id, 'operator_a', 'operator_reader', 'viewer');
  const readerHeaders = { ...headers, 'x-onto-username': 'operator_reader' };
  assert.equal((await app.inject({ url: `/api/projects/${linked.id}/mcp/runtime/status`, headers: readerHeaders })).statusCode, 200);
  for (const method of ['GET', 'POST', 'DELETE'] as const) {
    const url = `/api/projects/${linked.id}/mcp/tokens${method === 'DELETE' ? '/missing' : ''}`;
    assert.equal((await app.inject({ method, url, headers: readerHeaders })).statusCode, 404);
  }
  const adminHeaders = { ...headers, 'x-onto-username': 'operator_admin', 'x-onto-role': 'admin' };
  for (const id of [ordinary.id, createProject({ owner: 'operator_b', name: 'Synthetic other', externalId: 'onto:other' }).id]) {
    assert.equal((await app.inject({ url: `/api/projects/${id}/mcp/tokens`, headers: adminHeaders })).statusCode, 403);
  }
  assert.equal((await app.inject({ url: `/api/projects/${linked.id}/mcp/tokens`,
    headers: { ...adminHeaders, 'x-modeling-service-token': '' } })).statusCode, 401);
  assert.equal((await app.inject({ url: `/api/projects/${linked.id}/mcp/tokens`,
    headers: { ...adminHeaders, 'x-onto-role': 'user' } })).statusCode, 404);
});

test('ordinary authenticated requests cannot grant themselves Onto admin access via headers', async (context) => {
  const claims = Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 60 })).toString('base64url');
  const syntheticJwt = `eyJhbGciOiJIUzI1NiJ9.${claims}.synthetic`;
  context.mock.method(getAuthClient(), 'verify', async () => ({ username: 'operator_reader', role: 'user' }));
  const response = await app.inject({ method: 'POST', url: `/api/projects/${linked.id}/mcp/tokens`,
    headers: { authorization: `Bearer ${syntheticJwt}`, 'x-onto-role': 'admin',
      'x-onto-username': 'operator_a', 'x-onto-project-id': headers['x-onto-project-id'] },
    payload: { name: 'Synthetic unauthorized token' } });
  assert.equal(response.statusCode, 404);
});

// Full routes with synthetic JWT claims; only signature verification is mocked.
test('JWT admin association preserves the Onto owner and scopes MCP delegation', async (context) => {
  const { registerRoutes } = await import('../src/routes.js');
  const fullApp = Fastify();
  await registerRoutes(fullApp);
  context.after(() => fullApp.close());
  context.mock.method(getAuthClient(), 'verify', async (token: string) => {
    const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
    return { username: claims.sub, role: claims.role };
  });
  const externalId = 'onto:synthetic-jwt-project';
  const jwtHeaders = (username: string, role: string) => {
    const claims = Buffer.from(JSON.stringify({
      sub: username, role, exp: Math.floor(Date.now() / 1000) + 120,
    })).toString('base64url');
    return { authorization: `Bearer eyJhbGciOiJIUzI1NiJ9.${claims}.synthetic-association`,
      'x-onto-project-id': externalId };
  };
  const admin = jwtHeaders('operator_admin', 'admin');
  const owner = jwtHeaders('operator_a', 'modeler');
  const reader = { ...jwtHeaders('operator_reader', 'viewer'), 'x-onto-role': 'admin',
    'x-onto-username': 'operator_a' };
  const payload = { name: 'Synthetic JWT association', external_id: externalId,
    onto_owner: 'operator_a', runtime: 'agent' };
  const created = await fullApp.inject({ method: 'POST', url: '/api/projects', headers: admin, payload });
  assert.equal(created.statusCode, 201, created.body);
  const project = created.json().project;
  assert.equal(project.owner, 'operator_a');
  const membersBefore = listProjectMembers(project.id);
  const synced = await fullApp.inject({ method: 'PUT',
    url: `/api/projects/${project.id}/members/sync`, headers: admin,
    payload: { members: [] } });
  assert.equal(synced.statusCode, 200, synced.body);
  const base = `/api/projects/${project.id}/mcp`;
  const freshAdmin = { ...admin, authorization: admin.authorization + '-uncached' };
  assert.equal((await fullApp.inject({
    url: `${base}/runtime/status`, headers: freshAdmin,
  })).statusCode, 200);
  for (const actor of [admin, owner]) {
    const retry = await fullApp.inject({ method: 'POST', url: '/api/projects', headers: actor, payload });
    assert.equal(retry.statusCode, 200, retry.body);
    assert.equal(retry.json().project.id, project.id);
    assert.equal(retry.json().created, false);
    const lookup = await fullApp.inject({ url: `/api/projects/by-external-id?external_id=${externalId}`,
      headers: actor });
    assert.equal(lookup.statusCode, 200, lookup.body);
    assert.equal(lookup.json().project.id, project.id);
    assert.equal((await fullApp.inject({ url: `${base}/runtime/status`, headers: actor })).statusCode, 200);
    assert.equal((await fullApp.inject({ url: `${base}/tokens`, headers: actor })).statusCode, 200);
    const issued = await fullApp.inject({ method: 'POST', url: `${base}/tokens`, headers: actor,
      payload: { name: 'Synthetic JWT token', scopes: ['ontology.read'] } });
    assert.equal(issued.statusCode, 201, issued.body);
    const result = issued.json();
    assert.equal(result.token_summary.subjectUsername, 'operator_a');
    assert.equal(result.token_summary.issuedBy, actor === admin ? 'operator_admin' : 'operator_a');
    const verified = await verifyProjectMcpToken({ bearerToken: result.token,
      endpointId: result.endpoint_id, minimumAccess: 'read' });
    assert.equal(verified.projectId, project.id);
    assert.equal((await fullApp.inject({ method: 'DELETE',
      url: `${base}/tokens/${result.token_summary.id}`, headers: actor })).statusCode, 200);
  }
  assert.deepEqual(listProjectMembers(project.id), membersBefore);
  setProjectMember(project.id, 'operator_a', 'operator_reader', 'viewer');
  assert.equal((await fullApp.inject({ url: `${base}/runtime/status`, headers: reader })).statusCode, 200);
  // These calls reuse the cached admin JWT: delegation must remain request-scoped.
  for (const invalid of [reader, { authorization: admin.authorization },
    { ...admin, 'x-onto-project-id': 'onto:another-project' }]) {
    for (const method of ['GET', 'POST', 'DELETE'] as const) {
      assert.equal((await fullApp.inject({ method,
        url: `${base}/tokens${method === 'DELETE' ? '/missing' : ''}`, headers: invalid })).statusCode, 404);
    }
  }
  for (const invalid of [reader, { ...admin, 'x-onto-project-id': 'onto:another-project' }]) {
    assert.equal((await fullApp.inject({ url: `/api/projects/by-external-id?external_id=${externalId}`,
      headers: invalid })).statusCode, 404);
    assert.equal((await fullApp.inject({ method: 'POST', url: '/api/projects',
      headers: invalid, payload })).statusCode, 409);
  }
  for (const id of [ordinary.id, linked.id]) {
    assert.equal((await fullApp.inject({ url: `/api/projects/${id}/mcp/tokens`,
      headers: admin })).statusCode, 404);
  }
  // Ordinary creation still uses the authenticated user, ignoring an owner hint.
  for (const [actor, expectedOwner, suffix] of [
    [reader, 'operator_reader', 'reader'], [admin, 'operator_admin', 'admin'],
  ] as const) {
    const response = await fullApp.inject({ method: 'POST', url: '/api/projects',
      headers: actor, payload: { ...payload, external_id: `onto:synthetic-${suffix}` } });
    assert.equal(response.statusCode, 201, response.body);
    assert.equal(response.json().project.owner, expectedOwner);
  }
});

// Synthetic content verifies actual artifact import, retries and conflict protection.
test('Onto package sync preserves remote edits and rejects unauthorized archives', async () => {
  const { zipPackage } = await import('../src/workflow/export.js');
  const { renderPackage } = await import('../src/workflow/package-render.js');
  const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');
  const spec = {
    id: 'synthetic-package', name: 'Synthetic package', description: 'Synthetic test content',
    version: '0.1.0', status: 'draft' as const, language: 'zh-CN',
    objects: [{ key: 'Item', title: 'Item', description: 'Synthetic item', primaryKey: ['id'],
      fields: [{ key: 'id', cnName: '编号', type: 'string', required: true }] }],
    operations: [],
  };
  const archive = (description: string) => Buffer.from(zipPackage(renderPackage({ ...spec, description }), 'synthetic-package')).toString('base64');
  const url = `/api/projects/${linked.id}/mcp/package`;
  const payload = { archive: archive('Synthetic original') };
  const post = (body: object, actorHeaders = headers) => app.inject({ method: 'POST', url, headers: actorHeaders, payload: body });
  assert.equal((await post(payload, { ...headers, 'x-onto-username': 'operator_reader' })).statusCode, 404);
  assert.equal((await post(payload, { ...headers, 'x-onto-project-id': 'onto:wrong' })).statusCode, 403);
  assert.equal((await post({ archive: 'invalid' })).statusCode, 400);
  assert.equal((await post({ archive: Buffer.from('not a zip').toString('base64') })).statusCode, 400);
  const first = await post(payload);
  assert.equal(first.statusCode, 200, first.body);
  const revision = first.json().revision;
  const store = getPackageArtifactStore();
  assert.equal((await store.current(linked.id))?.spec.objects[0]?.key, 'Item');
  const repeated = await post(payload);
  assert.equal(repeated.json().revision, revision);
  assert.equal(repeated.json().changed, false);
  const changed = { archive: archive('Synthetic changed') };
  assert.equal((await post(changed)).statusCode, 409);
  const second = await post({ ...changed, expected_revision: revision });
  assert.equal(second.statusCode, 200, second.body);
  const remote = await store.saveFile(linked.id, {
    expectedRevision: second.json().revision, path: 'package/index.md', content: '# Synthetic remote edit\n',
    action: { cause: 'manual_edit', summary: 'Synthetic edit', actorKind: 'user', actorName: 'operator_a' },
  });
  assert.equal((await post({ ...payload, expected_revision: second.json().revision })).statusCode, 409);
  assert.equal((await store.readFile(linked.id, 'package/index.md')).revision.oid, remote.revision.oid);
  const { createOrGetPackageDraft } = await import('../src/workflow/package-draft.js');
  await createOrGetPackageDraft(linked.id, 'operator_a');
  const blocked = await post({ ...payload, expected_revision: remote.revision.oid });
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'PACKAGE_DRAFT_REQUIRES_PUBLISH');
});
