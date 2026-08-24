import type { FastifyInstance, FastifyReply } from 'fastify';

import { PackageArtifactStoreError } from '../artifacts/package-store.js';
import { canReadProject, canWriteProject, claimAgentRuntime, getProject, projectMemberRole } from '../projects/repo.js';
import { parsePackageArchive } from '../workflow/package-import.js';
import { AgentCoreError, AgentCoreService } from './core-service.js';
import { deliverAgentProjectEvents } from './project-event-outbox.js';
import {
  STUDIO_SCHEMA,
  STUDIO_SCHEMA_VERSION,
  StudioReadError,
  StudioReadService,
} from './studio-read-service.js';
import { StudioReviewService } from './studio-review-service.js';
import { StudioWriteError, StudioWriteService } from './studio-write-service.js';

export function registerAgentCoreRoutes(app: FastifyInstance): void {
  const service = new AgentCoreService();
  const studioWrites = new StudioWriteService();
  const studioReviews = new StudioReviewService();

  app.get('/api/projects/:id/agent/snapshot', async (request, reply) => {
    let project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    project = claimAgentRuntime(project.id)!;
    try {
      return await service.workbenchSnapshot(project, request.user!.username);
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.put('/api/projects/:id/agent/confirm-policy', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const body = (request.body ?? {}) as any;
      return service.setWorkbenchConfirmPolicy(project.id, body.tier, body.mode);
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.get('/api/projects/:id/agent/file', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      return await service.workbenchFile(
        project,
        request.user!.username,
        String((request.query as any)?.path ?? ''),
      );
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.get('/api/projects/:id/agent/diff', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      return await service.workbenchDiff(project, request.user!.username);
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.get('/api/projects/:id/agent/package-sources', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    return service.workbenchPackageSources(project);
  });

  app.post('/api/projects/:id/agent/package-sources', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    const file = await (request as any).file();
    if (!file) return reply.code(400).send({ error: '请选择来源知识包 ZIP' });
    if (!String(file.filename || '').toLowerCase().endsWith('.zip')) {
      return reply.code(400).send({ error: '来源知识包只支持 .zip' });
    }
    try {
      const archive = parsePackageArchive(new Uint8Array(await file.toBuffer()));
      return reply.code(201).send(service.addWorkbenchPackageSource(
        project,
        request.user!.username,
        String(file.filename),
        archive,
      ));
    } catch (error) {
      if (error instanceof AgentCoreError) return sendAgentCoreError(reply, error);
      return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
    }
  });

  app.get('/api/projects/:id/agent/materials/:documentId', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      return service.workbenchMaterial(project, String((request.params as any).documentId ?? ''));
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.get('/api/projects/:id/evidence/locate', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      return service.workbenchEvidenceLocate(project, String((request.query as any)?.ref ?? ''));
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.get('/api/projects/:id/materials/:documentId/referrers', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      return await service.workbenchMaterialReferrers(
        project,
        request.user!.username,
        String((request.params as any).documentId ?? ''),
        String((request.query as any)?.anchor ?? ''),
      );
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });

  app.get('/api/projects/:id/studio/schema', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    reply.header('Cache-Control', 'private, max-age=3600, must-revalidate');
    const role = projectMemberRole(project.id, request.user!.username);
    const lease = studioWrites.leaseStatus(project.id, request.user!.username);
    const schemaEtag = `${STUDIO_SCHEMA_VERSION}-${role}-${lease.mode}-${lease.owner ?? ''}-${lease.expiresAt ?? ''}`;
    if (sendNotModified(request.headers['if-none-match'], reply, schemaEtag)) return reply;
    return studioSchemaForRole(role, lease);
  });

  app.get('/api/projects/:id/studio/elements', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const query = (request.query ?? {}) as Record<string, unknown>;
      const studio = new StudioReadService(await service.workbenchStudioFiles(
        project,
        request.user!.username,
      ));
      studio.assertRevision(query.workingRevision);
      const response = studio.list(query);
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioReadError(reply, error);
    }
  });

  app.post('/api/projects/:id/studio/elements', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioWrites.create(project, request.user!.username, request.body);
      setRevisionEtag(reply, response.workingRevision);
      return reply.code(201).send(response);
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.patch('/api/projects/:id/studio/elements/:elementId', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioWrites.patch(
        project,
        request.user!.username,
        String((request.params as any).elementId ?? ''),
        request.body,
      );
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.post('/api/projects/:id/studio/batch', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioWrites.batch(project, request.user!.username, request.body);
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.post('/api/projects/:id/studio/deletions/preview', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioWrites.previewDelete(project, request.user!.username, request.body);
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.post('/api/projects/:id/studio/deletions/apply', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioWrites.delete(project, request.user!.username, request.body);
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.delete('/api/projects/:id/studio/elements/:elementId', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioWrites.delete(
        project,
        request.user!.username,
        request.body,
        String((request.params as any).elementId ?? ''),
      );
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.post('/api/projects/:id/studio/review', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const response = await studioReviews.requestReview(
        project,
        request.user!.username,
        request.body,
      );
      setRevisionEtag(reply, response.workingRevision);
      triggerAgentProjectEventDelivery(request, project.id);
      return reply.code(202).send(response);
    } catch (error) {
      return sendStudioError(reply, error);
    }
  });

  app.get('/api/projects/:id/studio/elements/:elementId', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const query = (request.query ?? {}) as Record<string, unknown>;
      const studio = new StudioReadService(await service.workbenchStudioFiles(
        project,
        request.user!.username,
      ));
      studio.assertRevision(query.workingRevision);
      const response = studio.detail((request.params as any).elementId);
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioReadError(reply, error);
    }
  });

  app.get('/api/projects/:id/studio/graph', async (request, reply) => {
    const project = getProject(String((request.params as any).id ?? ''));
    if (!project || !canReadProject(project.id, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    try {
      const query = (request.query ?? {}) as Record<string, unknown>;
      const studio = new StudioReadService(await service.workbenchStudioFiles(
        project,
        request.user!.username,
      ));
      studio.assertRevision(query.workingRevision);
      const response = studio.graph(query);
      setRevisionEtag(reply, response.workingRevision);
      return response;
    } catch (error) {
      return sendStudioReadError(reply, error);
    }
  });

  app.post('/internal/agent/tools/:tool', async (request, reply) => {
    const projectId = String(request.headers['x-modeling-project'] ?? '').trim();
    const runId = String(request.headers['x-modeling-run'] ?? '').trim();
    if (!projectId || !runId) {
      return reply.code(400).send({
        code: 'INVALID_AGENT_CONTEXT',
        reason: '缺少 Agent 项目或运行标识',
        next: ['由 modeling-agent 携带 X-Modeling-Project 和 X-Modeling-Run 后重试'],
      });
    }
    const project = getProject(projectId);
    if (!project || !canWriteProject(project.id, request.user!.username)) {
      return reply.code(404).send({
        code: 'FORBIDDEN',
        reason: '项目不存在或当前用户无权访问',
        next: [],
      });
    }
    const argumentsValue = (request.body as any)?.arguments;
    const args = argumentsValue && typeof argumentsValue === 'object' && !Array.isArray(argumentsValue)
      ? argumentsValue as Record<string, unknown>
      : {};
    try {
      return await service.invoke({
        project,
        runId,
        username: request.user!.username,
        token: String(request.headers.authorization ?? '').replace(/^Bearer\s+/i, '').trim(),
      }, String((request.params as any).tool ?? ''), args);
    } catch (error) {
      return sendAgentCoreError(reply, error);
    }
  });
}

function studioSchemaForRole(
  role: ReturnType<typeof projectMemberRole>,
  lease: ReturnType<StudioWriteService['leaseStatus']>,
) {
  const writable = role !== 'viewer' && ['write', 'takeover'].includes(lease.mode);
  if (writable) return { ...STUDIO_SCHEMA, access: { role, lease } };
  return {
    ...STUDIO_SCHEMA,
    access: { role, lease },
    capabilities: {
      ...STUDIO_SCHEMA.capabilities,
      create: false,
      update: false,
      delete: false,
      batch: false,
    },
    kinds: STUDIO_SCHEMA.kinds.map((kind) => ({
      ...kind,
      capabilities: {
        ...kind.capabilities,
        create: false,
        update: false,
        delete: false,
        batch: false,
      },
    })),
  };
}

function setRevisionEtag(reply: FastifyReply, revision: string): void {
  reply.header('ETag', `"${revision.replaceAll('"', '')}"`);
  reply.header('Cache-Control', 'private, no-cache');
}

function triggerAgentProjectEventDelivery(
  request: { headers: Record<string, unknown>; log: { warn: (input: unknown, message: string) => void } },
  projectId: string,
): void {
  const authorization = String(request.headers.authorization ?? '');
  void deliverAgentProjectEvents(projectId, authorization).catch((error) => {
    request.log.warn({ err: error, projectId }, 'Studio 修改事件等待下次刷新重投');
  });
}

function sendNotModified(
  ifNoneMatch: string | string[] | undefined,
  reply: FastifyReply,
  etag: string,
): boolean {
  const quoted = `"${etag}"`;
  reply.header('ETag', quoted);
  const values = Array.isArray(ifNoneMatch) ? ifNoneMatch : String(ifNoneMatch ?? '').split(',');
  if (!values.some((value) => value.trim() === quoted || value.trim() === '*')) return false;
  reply.code(304).send();
  return true;
}

function sendStudioReadError(reply: FastifyReply, error: unknown) {
  if (error instanceof StudioReadError) {
    return reply.code(error.statusCode).send({
      code: error.code,
      reason: error.message,
      ...error.details,
      display: { text: error.message },
    });
  }
  return sendAgentCoreError(reply, error);
}

function sendStudioError(reply: FastifyReply, error: unknown) {
  if (error instanceof StudioWriteError) {
    return reply.code(error.statusCode).send({
      code: error.code,
      reason: error.message,
      ...error.details,
      display: { text: error.message },
    });
  }
  return sendStudioReadError(reply, error);
}

export function sendAgentCoreError(reply: FastifyReply, error: unknown) {
  if (error instanceof AgentCoreError) {
    return reply.code(error.statusCode).send({
      code: error.code,
      reason: error.message,
      next: error.next,
      ...error.details,
      display: { text: error.message },
    });
  }
  if (error instanceof PackageArtifactStoreError) {
    const status = error.code === 'invalid_input' ? 400
      : ['project_not_found', 'package_not_found', 'file_not_found', 'candidate_not_found'].includes(error.code) ? 404
        : 409;
    return reply.code(status).send({
      code: `ARTIFACT_${error.code.toUpperCase()}`,
      reason: error.message,
      next: ['核对当前候选和修订后重试'],
      display: { text: error.message },
    });
  }
  throw error;
}
