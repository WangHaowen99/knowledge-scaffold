import type { FastifyInstance, FastifyReply } from 'fastify';

import { mapArtifactHttpError } from '../artifacts/http-error.js';
import { sendHttpError } from '../http-error.js';
import { getProject } from '../projects/repo.js';
import { PackageFusionService, PackageFusionServiceError } from './package-fusion-service.js';
import {
  deliverAgentProjectEvents,
  enqueueFusionReviewProjectEvent,
} from './project-event-outbox.js';

export function registerPackageFusionRoutes(app: FastifyInstance): void {
  const service = new PackageFusionService();

  app.get('/api/projects/:id/agent/fusion-plans', async (request, reply) => {
    const project = ownedProject(request, reply);
    if (!project) return;
    try {
      return service.listPlans(project.id);
    } catch (error) {
      return sendPackageFusionError(reply, error);
    }
  });

  app.get('/api/projects/:id/agent/fusion-plans/:planId', async (request, reply) => {
    const project = ownedProject(request, reply);
    if (!project) return;
    try {
      return service.getPlan(project.id, String((request.params as any).planId ?? ''));
    } catch (error) {
      return sendPackageFusionError(reply, error);
    }
  });

  app.post('/api/projects/:id/agent/fusion-plans', async (request, reply) => {
    const project = ownedProject(request, reply);
    if (!project) return;
    try {
      const plan = await service.createPlan(project.id, request.user!.username, request.body);
      return reply.code(201).send(plan);
    } catch (error) {
      return sendPackageFusionError(reply, error);
    }
  });

  app.patch('/api/projects/:id/agent/fusion-plans/:planId', async (request, reply) => {
    const project = ownedProject(request, reply);
    if (!project) return;
    try {
      return service.updatePlan(
        project.id,
        String((request.params as any).planId ?? ''),
        request.body,
      );
    } catch (error) {
      return sendPackageFusionError(reply, error);
    }
  });

  app.post('/api/projects/:id/agent/fusion-plans/:planId/compile', async (request, reply) => {
    const project = ownedProject(request, reply);
    if (!project) return;
    try {
      const plan = await service.compilePlan(
        project.id,
        String((request.params as any).planId ?? ''),
        request.user!.username,
      );
      if (plan.candidateId) {
        enqueueFusionReviewProjectEvent(project.id, {
          eventId: `fusion-review:${project.id}:${plan.id}`,
          planId: plan.id,
          candidateId: plan.candidateId,
          outputName: plan.output.name,
          sourceCount: plan.sources.length,
          summary: `已生成“${plan.output.name}”的整体融合候选，包含 ${plan.sources.length} 个来源知识包`,
        });
        triggerAgentProjectEventDelivery(request, project.id);
      }
      return plan;
    } catch (error) {
      return sendPackageFusionError(reply, error);
    }
  });
}

function triggerAgentProjectEventDelivery(request: any, projectId: string): void {
  const authorization = String(request.headers.authorization ?? '');
  void deliverAgentProjectEvents(projectId, authorization).catch((error) => {
    request.log.warn({ err: error, projectId }, '融合复核事件等待下次刷新重投');
  });
}

function ownedProject(request: any, reply: FastifyReply) {
  const project = getProject(String(request.params?.id ?? ''));
  if (!project || project.owner !== request.user!.username) {
    reply.code(404).send({ error: '项目不存在' });
    return null;
  }
  return project;
}

function sendPackageFusionError(reply: FastifyReply, error: unknown) {
  if (error instanceof PackageFusionServiceError) {
    if (error.statusCode >= 500) return sendHttpError(reply, error);
    return reply.code(error.statusCode).send({
      code: error.code,
      reason: error.message,
      ...error.details,
      display: { text: error.message },
    });
  }
  const artifactError = mapArtifactHttpError(error);
  if (artifactError) {
    if (artifactError.statusCode >= 500) return sendHttpError(reply, error);
    return reply.code(artifactError.statusCode).send({
      code: `ARTIFACT_${artifactError.code.toUpperCase()}`,
      error: artifactError.message,
      reason: artifactError.message,
      display: { text: artifactError.message },
    });
  }
  return sendHttpError(reply, error);
}
