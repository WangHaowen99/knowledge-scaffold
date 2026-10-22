import type { FastifyInstance, FastifyReply } from 'fastify';

import { registerOntoPackageSyncRoute } from './onto-package-sync.js';
import { sendHttpError } from '../http-error.js';
import { canReadProject, canWriteProject, getProject } from '../projects/repo.js';
import {
  issueProjectMcpToken,
  listProjectMcpTokens,
  projectMcpRuntimeStatus,
  revokeProjectMcpToken,
} from './service.js';
import { toolNamesByToolset } from './toolset.js';

const TOOL_NAMES = toolNamesByToolset();

export function registerProjectMcpManagementRoutes(app: FastifyInstance): void {
  registerOntoPackageSyncRoute(app);
  app.get('/api/projects/:id/mcp/runtime/status', async (request, reply) => {
    const projectId = String((request.params as any).id ?? '');
    const project = getProject(projectId);
    if (!project || (request.ontoAdminProjectId !== project.id && !canReadProject(project.id, request.user!.username))) {
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
    if (!project || (request.ontoAdminProjectId !== project.id && !canWriteProject(project.id, request.user!.username))) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    return {
      tokens: listProjectMcpTokens(project.id),
    };
  });

  app.post('/api/projects/:id/mcp/tokens', async (request, reply) => {
    const projectId = String((request.params as any).id ?? '');
    const project = getProject(projectId);
    if (!project || (request.ontoAdminProjectId !== project.id && !canWriteProject(project.id, request.user!.username))) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const body = (request.body ?? {}) as Record<string, unknown>;
      const issued = await issueProjectMcpToken({
        projectId: project.id,
        name: String(body.name ?? ''),
        // A delegated administrator issues a project-owner token; the actual
        // administrator remains the issuer in the audit record. Runtime ACL
        // and scope checks continue to use the existing project membership.
        subjectUsername: request.ontoAdminProjectId === project.id ? project.owner : request.user!.username,
        issuedBy: request.user!.username,
        scopes: Array.isArray(body.scopes) ? body.scopes.map(String) : undefined,
        ttlSeconds: body.expires_in_seconds === null
          ? null
          : body.expires_in_seconds === undefined
            ? undefined
            : Number(body.expires_in_seconds),
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
    if (!project || (request.ontoAdminProjectId !== project.id && !canWriteProject(project.id, request.user!.username))) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    const revoked = revokeProjectMcpToken(project.id, tokenId, request.user!.username);
    if (!revoked) {
      return reply.code(404).send({ error: 'token 不存在或已吊销' });
    }
    return { ok: true };
  });
}

function sendMcpError(reply: FastifyReply, error: unknown) {
  return sendHttpError(reply, error, {
    code: 'MCP_INTERNAL_ERROR',
    message: 'MCP 服务内部错误',
    logMessage: 'MCP 管理接口执行失败',
    includeReason: false,
  });
}
