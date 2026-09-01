import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-token-atomicity-'));
process.env.MODELING_MCP_TOKEN_SECRET = 'short';

const [
  { config },
  { createProject },
  { issueProjectMcpToken, listProjectMcpTokens, ModelingMcpError },
] = await Promise.all([
  import('../src/config.js'),
  import('../src/projects/repo.js'),
  import('../src/mcp/service.js'),
]);

test('MCP token 签名失败时不写入 active token 记录', async () => {
  config.mcp.tokenSecret = 'short';
  const project = createProject({ owner: 'alice', name: 'MCP token atomicity', runtime: 'agent' });

  await assert.rejects(
    () => issueProjectMcpToken({
      projectId: project.id,
      name: 'invalid signing token',
      subjectUsername: project.owner,
      issuedBy: project.owner,
      scopes: ['ontology.read'],
      ttlSeconds: 3_600,
    }),
    (error: unknown) => error instanceof ModelingMcpError && error.code === 'MCP_TOKEN_SECRET_INVALID',
  );

  assert.deepEqual(listProjectMcpTokens(project.id, { includeRevoked: true }), []);
});
