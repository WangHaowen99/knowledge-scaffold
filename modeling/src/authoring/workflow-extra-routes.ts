import type { FastifyInstance, FastifyReply } from 'fastify';

import {
  AiConstructionTaskGraph,
  aiConstructionGraphSourceForProjectKind,
} from '../ai-construction/task-graph.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getDb } from '../db/index.js';
import { getProject } from '../projects/repo.js';
import {
  ASSISTED_WRITE_LEASE_TTL_MS,
  ProjectLeaseError,
  ProjectOperationLease,
  runWithProjectLease,
  SHORT_WRITE_LEASE_TTL_MS,
} from './lease.js';
import { getPiAuthoringAdapter } from './pi-adapter.js';
import { getAuthoringState } from './state.js';
import { AuthoringStateError, type AuthoringInteraction, type JsonObject } from './types.js';
import { buildScenarioDrafts } from './workflow-extras.js';
import { WorkflowStepProgressRepo } from './workflow-progress.js';
import type { WorkflowStepId } from './workflow-definition.js';

function ownedProject(id: string, username: string) {
  const project = getProject(id);
  return project?.owner === username ? project : null;
}

function inImmediateTransaction<T>(work: () => T): T {
  const db = getDb();
  if (db.isTransaction) return work();
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = work();
    db.exec('COMMIT');
    return result;
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

function sendWorkflowExtraError(reply: FastifyReply, error: unknown) {
  if (error instanceof ProjectLeaseError) {
    const status = error.code === 'project_not_found' ? 404
      : error.code === 'busy' || error.code === 'expired' || error.code === 'not_owner' ? 409 : 400;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  if (error instanceof AuthoringStateError) {
    const status = error.code === 'PROJECT_NOT_FOUND'
      || error.code === 'RUN_NOT_FOUND'
      || error.code === 'INTERACTION_NOT_FOUND'
      ? 404
      : error.code === 'INVALID_INPUT' ? 400 : 409;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  return reply.code(500).send({ error: error instanceof Error ? error.message : String(error) });
}

export function registerWorkflowExtraRoutes(app: FastifyInstance): void {
  const packages = getPackageArtifactStore();
  const progress = new WorkflowStepProgressRepo();
  const adapter = getPiAuthoringAdapter();
  const state = getAuthoringState();
  const leases = new ProjectOperationLease();
  const aiConstruction = new AiConstructionTaskGraph();

  app.get('/api/projects/:id/workflow/steps/:stepId', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const stepId = String((req.params as any).stepId) as WorkflowStepId;
    try {
      return { progress: progress.get(project.id, stepId) };
    } catch (error) {
      return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
    }
  });

  app.post('/api/projects/:id/workflow/scenario/start', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const construction = aiConstruction.snapshot(project.id)
      ?? aiConstruction.initialize(project.id, aiConstructionGraphSourceForProjectKind(project.kind));
    if (project.kind !== 'package' && construction.status !== 'completed') {
      return reply.code(409).send({ error: '请先完成 AI 辅助构建' });
    }
    if (state.snapshot(project.id).pendingInteractions.some((item) => item.type === 'scenario_review')) {
      return reply.code(409).send({ error: '还有场景待确认' });
    }
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '场景推演',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
      }, async () => {
        const current = await packages.current(project.id);
        if (!current) return reply.code(409).send({ error: '请先生成知识包' });
        const scenarios = buildScenarioDrafts(current.spec);
        if (!scenarios.length) return reply.code(409).send({ error: '知识包中没有可推演的操作' });
        const run = adapter.start(project.id, '场景推演', current.revision.oid, {
          stepId: 'scenario', total: scenarios.length,
        }).run;
        const interactions = scenarios.map((scenario) => adapter.request(
          project.id,
          run.id,
          'scenario_review',
          { title: '确认场景', ...scenario },
          current.revision.oid,
        ).interaction);
        const saved = progress.save({
          projectId: project.id,
          stepId: 'scenario',
          status: 'waiting_review',
          output: {
            runId: run.id,
            scenarios: scenarios as unknown as JsonObject[],
            decisions: [],
          },
          revision: current.revision.oid,
        });
        return { progress: saved, interactions };
      });
    } catch (error) {
      if (error instanceof ProjectLeaseError) {
        return reply.code(error.code === 'busy' ? 409 : 400).send({ error: error.message });
      }
      return reply.code(500).send({ error: error instanceof Error ? error.message : String(error) });
    }
  });

  app.post('/api/projects/:id/workflow/scenario/reviews/:interactionId/respond', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const interactionId = String((req.params as any).interactionId ?? '');
    const { requestId, response } = (req.body ?? {}) as {
      requestId?: unknown;
      response?: unknown;
    };
    if (typeof requestId !== 'string' || !requestId.trim()) {
      return reply.code(400).send({ error: '请求编号不能为空', code: 'INVALID_INPUT' });
    }
    if (!response || typeof response !== 'object' || Array.isArray(response)) {
      return reply.code(400).send({ error: '回答格式不正确', code: 'INVALID_INPUT' });
    }
    const action = String((response as Record<string, unknown>).action ?? '');
    if (!['accept', 'revise', 'reject'].includes(action)) {
      return reply.code(400).send({ error: '场景处理动作不正确', code: 'INVALID_INPUT' });
    }

    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '确认场景推演',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => inImmediateTransaction(() => {
        const interaction = state.interaction(project.id, interactionId);
        if (!interaction) {
          throw new AuthoringStateError('INTERACTION_NOT_FOUND', '场景待办不存在');
        }
        if (interaction.type !== 'scenario_review') {
          throw new AuthoringStateError('INVALID_INPUT', '该待办不是场景推演审核');
        }
        if (!aiConstruction.snapshot(project.id)) {
          aiConstruction.initialize(project.id, aiConstructionGraphSourceForProjectKind(project.kind));
        }
        const answered = adapter.respond(
          project.id,
          interaction.id,
          requestId.trim(),
          response as JsonObject,
        );
        const completion = completeScenarioReview(answered.interaction, response as JsonObject);
        return {
          interaction: answered.interaction,
          replayed: answered.replayed,
          progress: progress.get(project.id, 'scenario'),
          ...completion,
          cursor: state.snapshot(project.id).lastEventSeq,
        };
      }));
    } catch (error) {
      return sendWorkflowExtraError(reply, error);
    }
  });

}

export function completeScenarioReview(
  interaction: AuthoringInteraction,
  response: JsonObject,
): { completed: boolean; failed: boolean; remainingCount: number } {
  if (interaction.type !== 'scenario_review') {
    return { completed: false, failed: false, remainingCount: 0 };
  }
  const progress = new WorkflowStepProgressRepo();
  const state = getAuthoringState();
  const adapter = getPiAuthoringAdapter();
  const current = progress.get(interaction.projectId, 'scenario');
  const prior = Array.isArray(current?.output.decisions) ? current.output.decisions : [];
  const action = String(response.action ?? '');
  const failed = action !== 'accept';
  const anyFailed = failed || prior.some((item: any) => item?.action !== 'accept');
  const remaining = state.snapshot(interaction.projectId).pendingInteractions
    .filter((item) => item.runId === interaction.runId && item.type === 'scenario_review');
  const status = anyFailed ? 'failed' : remaining.length ? 'waiting_review' : 'completed';
  const recorded = prior.some((item: any) => item?.interactionId === interaction.id);
  if (!recorded || current?.status !== status) {
    progress.save({
      projectId: interaction.projectId,
      stepId: 'scenario',
      status,
      output: {
        ...(current?.output ?? {}),
        decisions: recorded ? prior : [...prior, {
          interactionId: interaction.id,
          action,
          ...(response.note ? { note: response.note } : {}),
        }],
      },
      revision: current?.revision,
    });
  }
  if (!remaining.length) adapter.complete(interaction.projectId, interaction.runId, { accepted: !anyFailed });
  if (!recorded || current?.status !== status) {
    state.apply({
      kind: 'publish_event', projectId: interaction.projectId, type: 'workflow.step.updated',
      payload: { stepId: 'scenario', status },
    });
  }
  return {
    completed: !anyFailed && remaining.length === 0,
    failed: anyFailed,
    remainingCount: remaining.length,
  };
}
