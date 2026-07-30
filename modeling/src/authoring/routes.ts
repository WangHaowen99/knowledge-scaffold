import type { FastifyInstance, FastifyReply } from 'fastify';

import {
  AiConstructionTaskEditor,
  type AiConstructionTaskEditRequest,
} from '../ai-construction/task-editing.js';
import {
  AiConstructionTaskGraph,
  AiConstructionTaskGraphError,
  type AiConstructionTaskAdjustmentAction,
  type AiConstructionTaskGraphCommand,
} from '../ai-construction/task-graph.js';
import { getProject } from '../projects/repo.js';
import {
  ProjectLeaseError,
  ProjectOperationLease,
  runWithProjectLease,
  SHORT_WRITE_LEASE_TTL_MS,
} from './lease.js';
import { getAuthoringState, onProjectEventAppended } from './state.js';
import { AuthoringStateError, type JsonObject, type ProjectEvent } from './types.js';

const EVENT_POLL_FALLBACK_MS = 2_000;
const HEARTBEAT_MS = 15_000;

function ownedProject(id: string, username: string) {
  const project = getProject(id);
  return project?.owner === username ? project : null;
}

function sendStateError(reply: FastifyReply, error: unknown) {
  if (error instanceof ProjectLeaseError) {
    const status = error.code === 'project_not_found' ? 404
      : error.code === 'busy' || error.code === 'expired' || error.code === 'not_owner' ? 409 : 400;
    return reply.code(status).send({
      error: error.message,
      code: error.code,
      ...(error.activeOperation ? { activeOperation: error.activeOperation } : {}),
    });
  }
  if (error instanceof AuthoringStateError) {
    const status = error.code === 'PROJECT_NOT_FOUND'
      || error.code === 'RUN_NOT_FOUND'
      || error.code === 'INTERACTION_NOT_FOUND'
      ? 404
      : error.code === 'INVALID_INPUT' ? 400 : 409;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  if (error instanceof AiConstructionTaskGraphError) {
    const status = error.code === 'PROJECT_NOT_FOUND'
      || error.code === 'GRAPH_NOT_FOUND'
      || error.code === 'TASK_NOT_FOUND'
      || error.code === 'MILESTONE_NOT_FOUND'
      ? 404
      : error.code === 'INVALID_TASK_PLAN' ? 400 : 409;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  throw error;
}

function writeEvent(stream: NodeJS.WritableStream, event: ProjectEvent): void {
  stream.write(`id: ${event.seq}\n`);
  stream.write(`event: ${event.type}\n`);
  stream.write(`data: ${JSON.stringify(event)}\n\n`);
}

function commandEvent(command: AiConstructionTaskGraphCommand) {
  return {
    command: command.type,
    ...('taskId' in command ? { taskId: command.taskId } : {}),
    ...('milestoneId' in command ? { milestoneId: command.milestoneId } : {}),
  };
}

export function registerAuthoringRoutes(app: FastifyInstance): void {
  const state = getAuthoringState();
  const graph = new AiConstructionTaskGraph();
  const editor = new AiConstructionTaskEditor();
  const leases = new ProjectOperationLease();

  app.get('/api/projects/:id/session', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const authoring = state.snapshot(project.id);
    return {
      project,
      authoring,
      aiConstruction: graph.snapshot(project.id),
      cursor: authoring.lastEventSeq,
    };
  });

  app.get('/api/projects/:id/ai-construction', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    return { aiConstruction: graph.snapshot(project.id) };
  });

  app.post('/api/projects/:id/ai-construction/commands', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const rawCommand = (req.body as any)?.command as AiConstructionTaskGraphCommand | undefined;
    if (!rawCommand || typeof rawCommand !== 'object' || typeof rawCommand.type !== 'string') {
      return reply.code(400).send({ error: '任务图命令不完整', code: 'INVALID_INPUT' });
    }
    const requestedRole = rawCommand.type === 'confirm_milestone' ? rawCommand.actor?.role : undefined;
    const actorRole = ['project_owner', 'modeler', 'domain_expert', 'validator'].includes(String(requestedRole))
      ? requestedRole
      : 'project_owner';
    const command = rawCommand.type === 'confirm_milestone'
      ? {
        ...rawCommand,
        actor: {
          username: req.user!.username,
          role: actorRole,
        },
      } as AiConstructionTaskGraphCommand
      : rawCommand;
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '更新建模任务图',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, () => {
        const aiConstruction = graph.apply(project.id, command);
        state.apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'ai_construction.graph.updated',
          payload: { ...commandEvent(command), revision: aiConstruction.revision },
        });
        return { aiConstruction, cursor: state.snapshot(project.id).lastEventSeq };
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.get('/api/projects/:id/ai-construction/tasks/:taskId/adjustment-preview', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const action = String((req.query as any)?.action ?? '') as AiConstructionTaskAdjustmentAction;
    if (!['skip', 'resume', 'defer', 'redo'].includes(action)) {
      return reply.code(400).send({ error: '任务调整动作不正确', code: 'INVALID_INPUT' });
    }
    try {
      return {
        preview: graph.previewAdjustment(project.id, String((req.params as any).taskId), action),
      };
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/tasks/:taskId/adjust', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const body = (req.body ?? {}) as any;
    const action = String(body.action ?? '') as AiConstructionTaskAdjustmentAction;
    const expectedRevision = Number(body.expectedRevision);
    if (!['skip', 'resume', 'defer', 'redo'].includes(action)
      || !Number.isInteger(expectedRevision) || expectedRevision < 1) {
      return reply.code(400).send({ error: '任务调整参数不正确', code: 'INVALID_INPUT' });
    }
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '调整建模任务',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, () => {
        const result = graph.adjust(project.id, {
          expectedRevision,
          taskId: String((req.params as any).taskId),
          action,
          actor: req.user!.username,
          reason: String(body.reason ?? ''),
        });
        state.apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'ai_construction.task.adjusted',
          payload: {
            adjustmentId: result.adjustment.id,
            taskId: result.adjustment.taskId,
            action,
            revision: result.graph.revision,
          },
        });
        return { ...result, cursor: state.snapshot(project.id).lastEventSeq };
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/task-edit-preview', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const edit = (req.body as any)?.edit as AiConstructionTaskEditRequest | undefined;
    if (!edit || !['add', 'reorder', 'split', 'merge'].includes(String(edit.action ?? ''))) {
      return reply.code(400).send({ error: '任务编辑参数不正确', code: 'INVALID_INPUT' });
    }
    try {
      return { preview: editor.preview(project.id, edit) };
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/task-edit', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const body = (req.body ?? {}) as any;
    const edit = body.edit as AiConstructionTaskEditRequest | undefined;
    const expectedRevision = Number(body.expectedRevision);
    if (!edit || !['add', 'reorder', 'split', 'merge'].includes(String(edit.action ?? ''))
      || !Number.isInteger(expectedRevision) || expectedRevision < 1) {
      return reply.code(400).send({ error: '任务编辑参数不正确', code: 'INVALID_INPUT' });
    }
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '编辑建模任务图',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, () => {
        const result = editor.apply(project.id, {
          expectedRevision,
          actor: req.user!.username,
          reason: String(body.reason ?? ''),
          edit,
        });
        state.apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'ai_construction.task.edited',
          payload: {
            editId: result.edit.id,
            action: result.edit.action,
            revision: result.graph.revision,
          },
        });
        return { ...result, cursor: state.snapshot(project.id).lastEventSeq };
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/interactions/:interactionId/respond', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const { requestId, response } = (req.body ?? {}) as {
      requestId?: string;
      response?: JsonObject;
    };
    if (!requestId?.trim() || !response || typeof response !== 'object' || Array.isArray(response)) {
      return reply.code(400).send({ error: '回答内容不完整', code: 'INVALID_INPUT' });
    }
    try {
      const result = state.apply({
        kind: 'respond_interaction',
        projectId: project.id,
        interactionId: String((req.params as any).interactionId),
        responseId: requestId,
        response,
      });
      if (result.kind !== 'interaction') throw new Error('待办回答结果不正确');
      return {
        interaction: result.interaction,
        run: result.run,
        replayed: result.replayed,
        cursor: state.snapshot(project.id).lastEventSeq,
      };
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.get('/api/projects/:id/events', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const queryAfter = Number((req.query as any)?.after ?? req.headers['last-event-id'] ?? 0);
    const after = Number.isSafeInteger(queryAfter) && queryAfter >= 0 ? queryAfter : 0;

    if (!String(req.headers.accept ?? '').includes('text/event-stream')) {
      return state.replay(project.id, after);
    }

    reply.hijack();
    reply.raw.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    reply.raw.write('retry: 2000\n\n');

    let cursor = after;
    let closed = false;
    const flush = () => {
      if (closed) return;
      try {
        let page = state.replay(project.id, cursor);
        while (page.events.length) {
          for (const event of page.events) writeEvent(reply.raw, event);
          cursor = page.nextSeq;
          if (!page.hasMore) break;
          page = state.replay(project.id, cursor);
        }
      } catch {
        cleanup();
      }
    };
    const unsubscribe = onProjectEventAppended(project.id, flush);
    const poll = setInterval(flush, EVENT_POLL_FALLBACK_MS);
    const heartbeat = setInterval(() => {
      if (!closed) reply.raw.write(': keep-alive\n\n');
    }, HEARTBEAT_MS);
    poll.unref?.();
    heartbeat.unref?.();
    const cleanup = () => {
      if (closed) return;
      closed = true;
      unsubscribe();
      clearInterval(poll);
      clearInterval(heartbeat);
      if (!reply.raw.writableEnded) reply.raw.end();
    };
    req.raw.once('close', cleanup);
    reply.raw.once('error', cleanup);
    flush();
  });
}
