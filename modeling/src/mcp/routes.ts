import type { FastifyInstance } from 'fastify';

import { canReadProject, canWriteProject, getProject } from '../projects/repo.js';
import {
  issueProjectMcpToken,
  listProjectMcpTokens,
  ModelingMcpError,
  projectMcpRuntimeStatus,
  revokeProjectMcpToken,
} from './service.js';
import { toolNamesByToolset } from './toolset.js';

const TOOL_NAMES = toolNamesByToolset();

export function registerProjectMcpManagementRoutes(app: FastifyInstance): void {
  app.get('/api/projects/:id/mcp/runtime/status', async (request, reply) => {
    const projectId = String((request.params as any).id ?? '');
    const project = getProject(projectId);
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      return projectMcpRuntimeStatus(project.id, TOOL_NAMES);
    } catch (error) {
      return sendMcpError(reply, error);
    }
  });

  app.get('/api/projects/:id/mcp/tokens', async (request, reply) => {
    const projectId = String((request.params as any).id ?? '');
    const project = getProject(projectId);
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    return {
      tokens: listProjectMcpTokens(project.id),
    };
  });

  app.post('/api/projects/:id/mcp/tokens', async (request, reply) => {
    const projectId = String((request.params as any).id ?? '');
    const project = getProject(projectId);
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const body = (request.body ?? {}) as Record<string, unknown>;
      const issued = await issueProjectMcpToken({
        projectId: project.id,
        name: String(body.name ?? ''),
        subjectUsername: request.user!.username,
        issuedBy: request.user!.username,
        scopes: Array.isArray(body.scopes) ? body.scopes.map(String) : undefined,
        ttlSeconds: body.expires_in_seconds === undefined ? undefined : Number(body.expires_in_seconds),
      });
      return reply.code(201).send({
        endpoint_id: issued.endpoint.endpointId,
        token: issued.token,
        token_summary: issued.tokenSummary,
      });
    } catch (error) {
      return sendMcpError(reply, error);
    }
  });

  app.delete('/api/projects/:id/mcp/tokens/:tokenId', async (request, reply) => {
    const projectId = String((request.params as any).id ?? '');
    const tokenId = String((request.params as any).tokenId ?? '');
    const project = getProject(projectId);
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    const revoked = revokeProjectMcpToken(project.id, tokenId, request.user!.username);
    if (!revoked) {
      return reply.code(404).send({ error: 'token 不存在或已吊销' });
    }
    return { ok: true };
  });
}

function sendMcpError(reply: { code: (statusCode: number) => { send: (body: Record<string, unknown>) => unknown } }, error: unknown) {
  if (error instanceof ModelingMcpError) {
    return reply.code(error.statusCode).send({ code: error.code, error: error.message });
  }
  return reply.code(500).send({
    code: 'MCP_INTERNAL_ERROR',
    error: error instanceof Error ? error.message : String(error),
  });
}
