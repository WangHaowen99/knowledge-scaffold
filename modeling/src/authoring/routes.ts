import { randomUUID } from 'node:crypto';
import type { FastifyInstance, FastifyReply } from 'fastify';

import { AiConstructionOrchestrator } from '../ai-construction/orchestrator.js';
import { generateProcessDraft } from '../ai-construction/process-draft-generator.js';
import { buildInterviewOutcomeFromCandidates } from '../ai-construction/process-interview.js';
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
import { ArtifactRepositoryError } from '../artifacts/repository.js';
import {
  getPackageArtifactStore,
  PackageArtifactStoreError,
} from '../artifacts/index.js';
import type { PackageArtifactStore } from '../artifacts/index.js';
import {
  appendBlankFlowchartDiagram,
  appendFlowchartDiagram,
  normalizeFlowchartDocument,
} from '../flowchart/document.js';
import { FlowchartDraftError, FlowchartDraftStore } from '../flowchart/draft-store.js';
import { renderDiagramFiles } from '../flowchart/projection.js';
import type { FlowchartDiagram, FlowchartDocument } from '../flowchart/types.js';
import { getProject } from '../projects/repo.js';
import {
  ControlledRepairController,
  ControlledRepairControllerError,
} from './controlled-repair-controller.js';
import { PiAuthoringAdapter } from './pi-adapter.js';
import { ProcessReviewFinalizer } from './process-review-finalizer.js';
import {
  ASSISTED_WRITE_LEASE_TTL_MS,
  ProjectLeaseError,
  ProjectOperationLease,
  runWithProjectLease,
  SHORT_WRITE_LEASE_TTL_MS,
} from './lease.js';
import { AuthoringState, getAuthoringState, onProjectEventAppended } from './state.js';
import { AuthoringStateError, type JsonObject, type ProjectEvent } from './types.js';
import { buildFlowchart } from './workflow-extras.js';

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
  if (error instanceof ControlledRepairControllerError) {
    return reply.code(error.code === 'revision_conflict' ? 409 : 400)
      .send({ error: error.message, code: error.code });
  }
  if (error instanceof ArtifactRepositoryError) {
    const status = error.code === 'revision_conflict' ? 409
      : error.code === 'revision_not_found' || error.code === 'not_initialized' ? 404 : 400;
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
  if (error instanceof FlowchartDraftError) {
    const status = error.code === 'not_found' ? 404
      : error.code === 'invalid_document' ? 400 : 409;
    return reply.code(status).send({
      error: error.message,
      code: error.code,
      ...(error.issues.length ? { issues: error.issues } : {}),
    });
  }
  if (error instanceof PackageArtifactStoreError) {
    const status = error.code === 'project_not_found'
      || error.code === 'package_not_found'
      || error.code === 'file_not_found'
      || error.code === 'revision_has_no_package'
      ? 404
      : error.code === 'commit_in_progress' ? 409 : 400;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  throw error;
}

function rekeyFlowchartDiagram(diagram: FlowchartDiagram): FlowchartDiagram {
  const nodeIds = new Map(diagram.nodes.map((node) => [node.id, `node-${randomUUID()}`]));
  return {
    ...diagram,
    id: `diagram-${randomUUID()}`,
    nodes: diagram.nodes.map((node) => ({ ...node, id: nodeIds.get(node.id)! })),
    edges: diagram.edges.map((edge) => ({
      ...edge,
      id: `edge-${randomUUID()}`,
      from: nodeIds.get(edge.from) ?? edge.from,
      to: nodeIds.get(edge.to) ?? edge.to,
    })),
    ...(diagram.layout ? {
      layout: {
        ...diagram.layout,
        positions: Object.fromEntries(Object.entries(diagram.layout.positions).flatMap(([nodeId, position]) => {
          const nextId = nodeIds.get(nodeId);
          return nextId ? [[nextId, position]] : [];
        })),
      },
    } : {}),
  };
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

export interface AuthoringRouteOptions {
  state?: AuthoringState;
  graph?: AiConstructionTaskGraph;
  orchestrator?: Pick<AiConstructionOrchestrator,
    'latestMaterialAnalysis' | 'latestConfirmedModelingBrief' | 'completeProcessReview'>;
  packages?: Pick<PackageArtifactStore, 'current' | 'readPackageFiles' | 'saveFiles'>;
  flowchartDrafts?: FlowchartDraftStore;
  processDraftGenerator?: typeof generateProcessDraft;
}

export function registerAuthoringRoutes(app: FastifyInstance, options: AuthoringRouteOptions = {}): void {
  const state = options.state ?? getAuthoringState();
  const graph = options.graph ?? new AiConstructionTaskGraph();
  const editor = new AiConstructionTaskEditor();
  const leases = new ProjectOperationLease();
  const controlledRepair = new ControlledRepairController({ state, graph });
  const packages = options.packages ?? getPackageArtifactStore();
  const flowchartDrafts = options.flowchartDrafts ?? new FlowchartDraftStore();
  const adapter = new PiAuthoringAdapter(state);
  const orchestrator = options.orchestrator ?? new AiConstructionOrchestrator({ graph, state });
  const processDraftGenerator = options.processDraftGenerator ?? generateProcessDraft;
  const processReviewFinalizer = new ProcessReviewFinalizer({
    drafts: flowchartDrafts,
    adapter,
    orchestrator,
  });

  const ensureFlowchartDraft = (
    projectId: string,
    interactionId: string,
    actorUsername: string,
  ) => {
    const existing = flowchartDrafts.get(projectId, interactionId);
    if (existing) return existing;
    const interaction = state.interaction(projectId, interactionId);
    if (!interaction || interaction.status !== 'pending'
      || interaction.type !== 'flowchart_review'
      || interaction.payload.purpose !== 'process_review') {
      throw new FlowchartDraftError('not_found', '流程图确认交互不存在');
    }
    return flowchartDrafts.create({
      projectId,
      interactionId,
      actorUsername,
      document: { diagrams: Array.isArray(interaction.payload.diagrams) ? interaction.payload.diagrams : [] },
    });
  };

  const availableFlowchartCandidates = (projectId: string, document: FlowchartDocument) => {
    const existingTitles = new Set(document.diagrams.map((diagram) => diagram.title.trim()));
    const usedCandidateIds = new Set(document.diagrams
      .map((diagram) => diagram.sourceCandidateId?.trim())
      .filter((candidateId): candidateId is string => Boolean(candidateId)));
    return (orchestrator.latestMaterialAnalysis(projectId)?.candidateProcesses ?? [])
      .filter((candidate) => candidate.candidateId
        ? !usedCandidateIds.has(candidate.candidateId) && !existingTitles.has(candidate.title.trim())
        : !existingTitles.has(candidate.title.trim()));
  };

  const publicFlowchartDraft = (projectId: string, snapshot: NonNullable<ReturnType<FlowchartDraftStore['get']>>) => ({
    draftRevision: snapshot.revision,
    document: snapshot.document,
    warnings: snapshot.warnings,
    submittedRevision: snapshot.submittedRevision ?? null,
    availableCandidates: availableFlowchartCandidates(projectId, snapshot.document),
    modelingGoal: orchestrator.latestConfirmedModelingBrief(projectId),
    updatedAt: snapshot.updatedAt,
  });

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

  app.post('/api/projects/:id/ai-construction/knowledge/repair', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '受控修复知识包校验问题',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
      }, () => controlledRepair.run(project));
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.get('/api/projects/:id/interactions/:interactionId/flowchart-draft', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      const interactionId = String((req.params as any).interactionId ?? '');
      return publicFlowchartDraft(
        project.id,
        ensureFlowchartDraft(project.id, interactionId, req.user!.username),
      );
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.put('/api/projects/:id/interactions/:interactionId/flowchart-draft', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const body = (req.body ?? {}) as Record<string, unknown>;
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '保存流程图草稿',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, () => {
        const interactionId = String((req.params as any).interactionId ?? '');
        ensureFlowchartDraft(project.id, interactionId, req.user!.username);
        const saved = flowchartDrafts.save({
          projectId: project.id,
          interactionId,
          actorUsername: req.user!.username,
          expectedRevision: Number(body.expectedRevision),
          document: body.document,
        });
        state.apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'flowchart.draft.saved',
          payload: {
            interactionId,
            draftRevision: saved.revision,
            warningCount: saved.warnings.length,
          },
        });
        return publicFlowchartDraft(project.id, saved);
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/interactions/:interactionId/flowchart-draft/diagrams', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const body = (req.body ?? {}) as Record<string, unknown>;
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '继续添加流程图',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
      }, async () => {
        const interactionId = String((req.params as any).interactionId ?? '');
        const draft = ensureFlowchartDraft(project.id, interactionId, req.user!.username);
        const expectedRevision = Number(body.expectedRevision);
        if (!Number.isInteger(expectedRevision) || expectedRevision !== draft.revision) {
          throw new FlowchartDraftError('conflict', '流程图草稿已被其他修改更新');
        }
        const mode = String(body.mode ?? '');
        if (!['candidate', 'ai', 'blank'].includes(mode)) {
          return reply.code(400).send({ error: '流程图添加方式不正确', code: 'INVALID_INPUT' });
        }

        let document = draft.document;
        if (mode === 'blank') {
          document = appendBlankFlowchartDiagram(document, {
            id: `diagram-${randomUUID()}`,
            title: String(body.title ?? '').trim() || '未命名流程',
          });
        } else {
          const modelingBrief = orchestrator.latestConfirmedModelingBrief(project.id);
          if (!modelingBrief) {
            return reply.code(409).send({
              error: '请先确认建模目标，再继续添加流程图',
              code: 'MODELING_GOAL_NOT_CONFIRMED',
            });
          }
          const candidates = availableFlowchartCandidates(project.id, document);
          const candidateId = String(body.candidateId ?? '').trim();
          const candidateTitle = String(body.candidateTitle ?? '').trim();
          const sourceCandidate = mode === 'candidate'
            ? candidates.find((candidate) => candidateId
              ? candidate.candidateId === candidateId
              : candidate.title === candidateTitle)
            : undefined;
          if (mode === 'candidate' && !sourceCandidate) {
            return reply.code(409).send({
              error: '该候选流程不存在或已经添加',
              code: 'FLOWCHART_CANDIDATE_UNAVAILABLE',
            });
          }
          const instruction = String(body.instruction ?? '').trim();
          const description = sourceCandidate
            ? [
              '请根据以下资料分析候选，围绕已确认建模目标整理成完整流程草稿。',
              JSON.stringify(sourceCandidate),
              ...(instruction ? [`补充要求：${instruction}`] : []),
            ].join('\n')
            : instruction
              || `请围绕“${modelingBrief.problem}”及场景“${modelingBrief.scenarios}”推荐一个尚未覆盖的核心业务流程。`;
          const candidate = await processDraftGenerator({
            description,
            ...(sourceCandidate ? { candidateProcess: sourceCandidate } : {}),
            existingProcessTitles: document.diagrams.map((diagram) => diagram.title),
            modelingBrief,
          });
          const outcome = buildInterviewOutcomeFromCandidates([candidate]);
          const generated = normalizeFlowchartDocument(buildFlowchart(undefined, undefined, outcome.processes));
          const diagram = generated.diagrams[0];
          if (!diagram) throw new Error('没有生成可编辑的流程图');
          document = appendFlowchartDiagram(document, {
            ...rekeyFlowchartDiagram(diagram),
            ...(sourceCandidate?.candidateId ? { sourceCandidateId: sourceCandidate.candidateId } : {}),
          });
        }

        const saved = flowchartDrafts.save({
          projectId: project.id,
          interactionId,
          actorUsername: req.user!.username,
          expectedRevision,
          document,
        });
        state.apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'flowchart.diagram.added',
          payload: { interactionId, draftRevision: saved.revision, mode },
        });
        return publicFlowchartDraft(project.id, saved);
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
    const interactionId = String((req.params as any).interactionId);
    try {
      const repairInteraction = controlledRepair.interaction(project.id, interactionId);
      if (repairInteraction) {
        const approving = repairInteraction.type === 'change_plan' && response.action === 'approve';
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: repairInteraction.type === 'question_batch'
            ? '确认知识包修复业务决策'
            : approving ? '应用知识包修复方案' : '取消知识包修复方案',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: approving ? ASSISTED_WRITE_LEASE_TTL_MS : SHORT_WRITE_LEASE_TTL_MS,
        }, () => controlledRepair.respond(
          project,
          repairInteraction,
          requestId,
          response,
          req.user!.username,
        ));
      }
      const interaction = state.interaction(project.id, interactionId);
      if (interaction?.type === 'flowchart_review' && interaction.payload.purpose === 'process_review') {
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: '提交业务流程图',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: SHORT_WRITE_LEASE_TTL_MS,
        }, async () => {
          if (String(response.action ?? '') !== 'accept') {
            return reply.code(409).send({
              error: '请直接在流程图画布中修改并保存，再提交确认',
              code: 'FLOWCHART_DIRECT_EDIT_REQUIRED',
            });
          }
          const draftRevision = Number(response.draftRevision);
          if (!Number.isInteger(draftRevision) || draftRevision < 1) {
            return reply.code(400).send({ error: '请先保存流程图草稿', code: 'INVALID_INPUT' });
          }
          const currentDraft = ensureFlowchartDraft(project.id, interactionId, req.user!.username);
          if (currentDraft.revision !== draftRevision) {
            return reply.code(409).send({
              error: '流程图草稿有更新，请提交最新已保存版本',
              code: 'FLOWCHART_DRAFT_OUTDATED',
            });
          }
          if (!currentDraft.document.diagrams.length) {
            return reply.code(409).send({
              error: '请至少新建并保存一个业务流程后再提交',
              code: 'FLOWCHART_EMPTY_DOCUMENT',
            });
          }
          if (currentDraft.warnings.length && response.acknowledgeWarnings !== true) {
            return reply.code(409).send({
              error: '流程图草稿仍有业务完整性提醒，请确认后再提交',
              code: 'FLOWCHART_WARNINGS_NOT_ACKNOWLEDGED',
              warnings: currentDraft.warnings,
            });
          }

          const attempt = processReviewFinalizer.begin({
            projectId: project.id,
            interactionId,
            draftRevision,
            requestId,
            response,
            actorUsername: req.user!.username,
          });
          if (attempt.status === 'completed') {
            return {
              interaction: state.interaction(project.id, interactionId),
              replayed: true,
              packageRevision: attempt.packageRevisionOid,
              aiConstruction: graph.snapshot(project.id),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }

          let packageRevisionOid = attempt.packageRevisionOid;
          if (!packageRevisionOid) {
            const currentPackage = await packages.current(project.id);
            if (!currentPackage) {
              throw new AuthoringStateError('INVALID_TRANSITION', '当前项目没有知识包草稿');
            }
            if (interaction.baseRevision && interaction.baseRevision !== currentPackage.revision.oid) {
              throw new AuthoringStateError('INVALID_TRANSITION', '知识包已更新，请重新生成并确认流程图');
            }
            const projected = renderDiagramFiles(currentDraft.document);
            const files = Object.fromEntries(Object.entries(projected)
              .map(([path, content]) => [`package/${path}`, content]));
            const currentFiles = await packages.readPackageFiles(project.id, currentPackage.revision.oid);
            const projectedPaths = new Set(Object.keys(projected));
            const deletePaths = Object.keys(currentFiles.files)
              .filter((path) => /^diagrams\/process_\d+\.mmd$/u.test(path) && !projectedPaths.has(path))
              .map((path) => `package/${path}`);
            const saved = await packages.saveFiles(project.id, {
              expectedRevision: currentPackage.revision.oid,
              files,
              deletePaths,
              action: {
                cause: 'flowchart.confirm',
                summary: '确认可编辑业务流程',
                actorKind: 'user',
                actorName: req.user!.username,
              },
            });
            packageRevisionOid = saved.revision.oid;
            processReviewFinalizer.recordPackageSaved(
              project.id,
              interactionId,
              draftRevision,
              packageRevisionOid,
            );
          }

          const finalized = processReviewFinalizer.finalize({
            projectId: project.id,
            interactionId,
            draftRevision,
            requestId,
            response,
            actorUsername: req.user!.username,
            packageRevisionOid,
            taskId: String(interaction.payload.taskId ?? ''),
            document: currentDraft.document,
          });
          state.apply({
            kind: 'publish_event',
            projectId: project.id,
            type: 'flowchart.review.completed',
            payload: { interactionId, draftRevision, packageRevisionOid },
          });
          return {
            interaction: finalized.answered.interaction,
            run: finalized.completedRun,
            replayed: finalized.answered.replayed,
            aiConstruction: finalized.construction.aiConstruction,
            aiConstructionMessage: finalized.construction.message,
            packageRevision: packageRevisionOid,
            cursor: state.snapshot(project.id).lastEventSeq,
          };
        });
      }
      const result = state.apply({
        kind: 'respond_interaction',
        projectId: project.id,
        interactionId,
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
