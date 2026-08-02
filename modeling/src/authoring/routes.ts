import { randomUUID } from 'node:crypto';
import type { FastifyInstance, FastifyReply } from 'fastify';

import {
  AiConstructionMessageStore,
  notifyAiConstructionMessageWritten,
} from '../ai-construction/messages.js';
import {
  buildScriptGenerationRequirement,
  identifyScriptCandidates,
  parseScriptContractResponse,
  parseScriptDecisionResponse,
  type ScriptCandidate,
} from '../ai-construction/deterministic-capability.js';
import {
  KnowledgeStructureRunner,
} from '../ai-construction/knowledge-structure-runner.js';
import {
  ModelingGoalInputError,
  resolveModelingBriefSubmission,
} from '../ai-construction/modeling-goal-submission.js';
import {
  PackageChatConversation,
  shouldRequestConstructionCompletion,
} from '../ai-construction/package-chat-conversation.js';
import { AiConstructionOrchestrator } from '../ai-construction/orchestrator.js';
import { generateProcessDraft } from '../ai-construction/process-draft-generator.js';
import {
  buildInterviewOutcome,
  buildInterviewOutcomeFromCandidates,
  isCompleteProcessCandidate,
  parseProcessInterviewCoreResponse,
  parseProcessInterviewDetailsResponse,
  parseProcessSelectionResponse,
  ProcessInterviewInputError,
  processInterviewQuestions,
  readProcessInterviewCore,
} from '../ai-construction/process-interview.js';
import {
  AiConstructionTaskEditor,
  type AiConstructionTaskEditRequest,
} from '../ai-construction/task-editing.js';
import {
  AiConstructionTaskGraph,
  AiConstructionTaskGraphError,
  aiConstructionGraphSourceForProjectKind,
  type AiConstructionTaskAdjustmentAction,
  type AiConstructionTaskGraphCommand,
} from '../ai-construction/task-graph.js';
import { ArtifactRepositoryError } from '../artifacts/repository.js';
import { ArtifactRevisionRepo } from '../artifacts/revision-repo.js';
import {
  getPackageArtifactStore,
  PackageArtifactStoreError,
} from '../artifacts/index.js';
import type { PackageArtifactStore } from '../artifacts/index.js';
import { getDb } from '../db/index.js';
import { listDocuments } from '../documents/repo.js';
import { getProjectDataSource } from '../evaluation/data-source-repo.js';
import { listFailureDiagnoses } from '../evaluation/failure-diagnosis-repo.js';
import {
  appendBlankFlowchartDiagram,
  appendFlowchartDiagram,
  normalizeFlowchartDocument,
} from '../flowchart/document.js';
import { FlowchartDraftError, FlowchartDraftStore } from '../flowchart/draft-store.js';
import { renderDiagramFiles } from '../flowchart/projection.js';
import type { FlowchartDiagram, FlowchartDocument } from '../flowchart/types.js';
import { getProject } from '../projects/repo.js';
import { listNodes } from '../workflow/engine.js';
import { findKnowledgeEvidenceProblems } from '../workflow/knowledge-evidence.js';
import * as m3repo from '../workflow/m3-repo.js';
import * as m4repo from '../workflow/m4-repo.js';
import {
  getCheck,
  runFormatChecks,
  runRuleChecks,
  runSemanticCheck,
  saveCheck,
  semanticCheckFailureFinding,
  type CheckFinding,
} from '../workflow/package-check.js';
import { deferPackageErrors, getPackageErrorDeferral } from '../workflow/package-error-deferral.js';
import { nextValidationCandidateVersion } from '../workflow/package-version-lifecycle.js';
import { packageIntentQuestions } from '../workflow/spec-validation.js';
import { buildReleaseReadiness } from '../workflow/release.js';
import { getPackageWarningReview } from '../workflow/warning-acceptance.js';
import { assertNoActivePackageDraft, PackageDraftError } from '../workflow/package-draft.js';
import {
  PackageCandidateRejectedError,
  packageChatApply,
  packageChatPlan,
  type ChangePlan,
} from '../workflow/package-chat.js';
import { resolveGenerationContinuation } from '../workflow/generation-gate.js';
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
import {
  blockingPendingInteractions,
  evidenceProblemActions,
  pendingInteractionActions,
} from './construction-completion.js';
import { RepairTargetUnresolvedError } from '../ai-construction/controlled-repair.js';
import { errorNextActions } from '../error-next-actions.js';
import { AuthoringState, getAuthoringState, onProjectEventAppended } from './state.js';
import { InFlightOperations } from '../in-flight-operations.js';
import { AuthoringStateError, type JsonObject, type ProjectEvent } from './types.js';
import { registerWorkflowExtraRoutes } from './workflow-extra-routes.js';
import { WorkflowStepProgressRepo } from './workflow-progress.js';
import { buildProjectWorkflow } from './workflow-session.js';
import { buildFlowchart } from './workflow-extras.js';

const EVENT_POLL_FALLBACK_MS = 2_000;
const HEARTBEAT_MS = 15_000;

function ownedProject(id: string, username: string) {
  const project = getProject(id);
  return project?.owner === username ? project : null;
}

function publicRevision(revision: ReturnType<ArtifactRevisionRepo['latest']>) {
  if (!revision) return null;
  return {
    id: revision.oid,
    sequence: revision.sequence,
    packageVersion: revision.packageVersion,
    cause: revision.cause,
    summary: revision.summary,
    actor: { kind: revision.actorKind, displayName: revision.actorName },
    changedPaths: revision.changedPaths,
    createdAt: revision.createdAt,
  };
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
  // 定位不到唯一目标不是「操作失败」，而是需要人工介入；给出可执行的下一步
  if (error instanceof RepairTargetUnresolvedError) {
    return reply.code(409).send({
      error: error.message,
      code: 'REPAIR_TARGET_UNRESOLVED',
      candidates: error.candidates.map((item) => ({
        targetKind: item.targetKind, targetKey: item.targetKey,
      })),
      nextActions: [
        ...(error.candidates.length
          ? [{
            id: 'open_package_file', label: `打开 ${error.candidates[0].targetKind} 文件`,
            style: 'primary' as const,
            path: `${error.candidates[0].targetKind === 'object' ? 'objects' : `${error.candidates[0].targetKind}s`}/index.md`,
          }]
          : [{ id: 'review_knowledge_files', label: '查看知识文件', style: 'primary' as const }]),
        {
          id: 'defer_validation_errors', label: '暂缓这条错误', style: 'secondary' as const,
          description: '登记豁免理由，先推进后续步骤',
        },
        { id: 'view_validation', label: '查看校验结果', style: 'secondary' as const },
      ],
    });
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
  if (error instanceof PackageDraftError) {
    return reply.code(error.statusCode).send({
      error: error.message,
      code: error.code,
      ...error.details,
    });
  }
  if (error instanceof ModelingGoalInputError) {
    return reply.code(400).send({ error: error.message, code: error.code });
  }
  if (error instanceof ProcessInterviewInputError) {
    return reply.code(400).send({ error: error.message, code: error.code });
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
    'latestMaterialAnalysis' | 'latestConfirmedModelingBrief' | 'completeProcessReview'
    | 'latestModelingBrief'
    | 'deferKnowledgeErrors' | 'advanceAfterMilestoneConfirmation'
    | 'completeModelingBrief' | 'reviseModelingBrief' | 'completeProcessInterview'
    | 'completeScriptDecision'
    | 'startScriptGeneration' | 'waitForScriptApproval' | 'completeScriptGeneration'
    | 'failScriptGeneration' | 'deferScriptGeneration' | 'retryScriptGeneration'>;
  packages?: Pick<PackageArtifactStore,
    'ensureProject' | 'initializeDraftPackage' | 'current' | 'readPackageFiles'
    | 'saveFiles' | 'saveCurrentAsPackageVersion' | 'countPendingCandidateRevisions'>;
  flowchartDrafts?: FlowchartDraftStore;
  processDraftGenerator?: typeof generateProcessDraft;
  /** 后台作业登记表。与 registerRoutes 共用同一个实例，跨路由去重才有意义 */
  jobs?: InFlightOperations;
  packageChatApply?: typeof packageChatApply;
  packageChatPlan?: typeof packageChatPlan;
  knowledgeRunner?: Pick<KnowledgeStructureRunner, 'run'>;
  modelingBriefResolver?: typeof resolveModelingBriefSubmission;
}

export function registerAuthoringRoutes(app: FastifyInstance, options: AuthoringRouteOptions = {}): void {
  // 与 registerRoutes 同款：错误响应按 code 统一挂上可执行的下一步。
  // 两处路由分开注册，钩子也要各挂一次。
  app.addHook('onSend', async (_req, reply, payload) => {
    if (reply.statusCode < 400 || typeof payload !== 'string') return payload;
    if (!String(reply.getHeader('content-type') ?? '').includes('application/json')) return payload;
    let body: unknown;
    try { body = JSON.parse(payload); } catch { return payload; }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return payload;
    const record = body as Record<string, unknown>;
    if (record.nextActions !== undefined) return payload;
    const actions = errorNextActions(record.code);
    if (!actions) return payload;
    const next = JSON.stringify({ ...record, nextActions: actions });
    reply.header('content-length', Buffer.byteLength(next));
    return next;
  });

  registerWorkflowExtraRoutes(app);
  const state = options.state ?? getAuthoringState();
  const graph = options.graph ?? new AiConstructionTaskGraph();
  const ensureProjectGraph = (project: NonNullable<ReturnType<typeof ownedProject>>) => (
    graph.snapshot(project.id)
    ?? graph.initialize(project.id, aiConstructionGraphSourceForProjectKind(project.kind))
  );
  const editor = new AiConstructionTaskEditor();
  const leases = new ProjectOperationLease();
  const jobs = options.jobs ?? new InFlightOperations();
  const controlledRepair = new ControlledRepairController({ state, graph });
  const packages = options.packages ?? getPackageArtifactStore();
  const flowchartDrafts = options.flowchartDrafts ?? new FlowchartDraftStore();
  const adapter = new PiAuthoringAdapter(state);
  const messages = new AiConstructionMessageStore(
    getDb(),
    notifyAiConstructionMessageWritten((command) => state.apply(command)),
  );
  const packageChatConversation = new PackageChatConversation(messages);
  const applyPackageChat = options.packageChatApply ?? packageChatApply;
  const planPackageChat = options.packageChatPlan ?? packageChatPlan;
  const orchestrator = options.orchestrator ?? new AiConstructionOrchestrator({
    graph,
    state,
    messages,
  });
  const revisions = new ArtifactRevisionRepo(getDb());
  const workflowProgress = new WorkflowStepProgressRepo(getDb());
  const processDraftGenerator = options.processDraftGenerator ?? generateProcessDraft;
  const resolveModelingBrief = options.modelingBriefResolver ?? resolveModelingBriefSubmission;
  const knowledgeRunner = options.knowledgeRunner ?? new KnowledgeStructureRunner({
    orchestrator: orchestrator as AiConstructionOrchestrator,
  });
  const readConfirmedFlowchart = async (projectId: string) => {
    const currentPackage = await packages.current(projectId);
    if (!currentPackage) throw new Error('当前项目没有知识包草稿');
    const construction = graph.snapshot(projectId);
    const confirmation = construction?.confirmations.find(
      (item) => item.milestoneId === 'process_confirmed',
    );
    if (!confirmation) {
      throw new AuthoringStateError('INVALID_TRANSITION', '业务流程尚未确认，请先确认流程图');
    }
    const snapshot = await packages.readPackageFiles(projectId, currentPackage.revision.oid);
    // 早先这里比对包修订 OID，但生成知识结构、自动修复、人工改包都会产生新修订，
    // 于是流程内容没动也会被判为「未确认」——「重新生成知识结构」因此必然失败。
    // 改为比对流程文档本身：只有流程真的被改过才要求重新确认。
    if (confirmation.packageRevisionOid !== currentPackage.revision.oid) {
      const confirmed = await packages
        .readPackageFiles(projectId, confirmation.packageRevisionOid)
        .catch(() => null);
      const confirmedProcesses = confirmed?.files['diagrams/processes.json'];
      if (!confirmedProcesses) {
        throw new AuthoringStateError(
          'INVALID_TRANSITION',
          '已确认流程所在的知识包修订已不可读，请重新确认流程图',
        );
      }
      if (confirmedProcesses !== snapshot.files['diagrams/processes.json']) {
        throw new AuthoringStateError(
          'INVALID_TRANSITION',
          '流程图在确认之后被修改过，请重新确认流程图',
        );
      }
    }
    const source = snapshot.files['diagrams/processes.json'];
    if (!source) throw new Error('当前知识包修订缺少已确认流程文档');
    let document: FlowchartDocument;
    try {
      document = normalizeFlowchartDocument(JSON.parse(source));
    } catch (error) {
      throw new Error(`已确认流程文档格式不正确：${error instanceof Error ? error.message : String(error)}`);
    }
    if (!document.diagrams.length) throw new Error('已确认流程文档中没有可用流程');
    return { currentPackage, flowchart: { diagrams: document.diagrams } };
  };
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

  const readProjectSession = async (
    project: NonNullable<ReturnType<typeof ownedProject>>,
    { advance }: { advance: boolean },
  ) => {
    const documents = listDocuments(project.id);
    const nodes = listNodes(project.id);
    const dataSource = getProjectDataSource(project.id);

    await packages.ensureProject(project.id);
    let currentPackage = await packages.current(project.id);
    if (advance && !currentPackage && project.kind !== 'package') {
      currentPackage = await packages.initializeDraftPackage(project.id, {
        id: `project-${project.id.replace(/^pj_/, '')}`,
        name: project.name,
        description: project.background || `${project.name} 的 AI 辅助构建草稿`,
      });
    }

    const latestRevision = revisions.latest(project.id);
    const initialDraft = Boolean(
      currentPackage
      && latestRevision?.cause === 'draft_initialized'
      && currentPackage.packageVersion === '0.1.0'
      && currentPackage.spec.status === 'draft'
      && currentPackage.spec.objects.length === 0
      && currentPackage.spec.operations.length === 0,
    );
    const workflowPackage = initialDraft ? null : currentPackage;

    let constructionSnapshot = advance ? ensureProjectGraph(project) : graph.snapshot(project.id);
    if (advance && constructionSnapshot && [
      'goal_confirmed', 'materials_ready', 'process_confirmed',
      'knowledge_confirmed', 'deterministic_confirmed', 'construction_completed',
    ].includes(String(constructionSnapshot.progress.currentMilestoneId ?? ''))) {
      const currentMilestoneId = String(constructionSnapshot.progress.currentMilestoneId ?? '');
      constructionSnapshot = orchestrator.advanceAfterMilestoneConfirmation(
        project.id,
        constructionSnapshot,
        currentMilestoneId === 'deterministic_confirmed' && currentPackage
          ? {
            scriptCandidates: identifyScriptCandidates(
              currentPackage.spec.operations,
              currentPackage.spec.metrics ?? [],
            ),
          }
          : {},
      ).aiConstruction;
    }
    if (advance && constructionSnapshot) messages.ensureWelcome(project.id, constructionSnapshot);

    const authoring = state.snapshot(project.id);
    const rounds = m3repo.listRounds(project.id);
    const latestRound = rounds[0];
    const latestResults = latestRound ? m3repo.listResults(latestRound.id) : [];
    const testcases = m3repo.listTestcases(project.id);
    const requiredTestcases = testcases.filter((testcase) => testcase.source === 'manual');
    const packageCheck = getCheck(project.id);
    const warningReview = workflowPackage
      ? getPackageWarningReview(project.id, workflowPackage.revision.oid)
      : null;
    const progress = workflowProgress.list(project.id);
    const flowchartConfirmed = project.kind !== 'package'
      ? Boolean(constructionSnapshot?.milestones.some((milestone) => (
        milestone.id === 'process_confirmed' && milestone.status === 'confirmed'
      )))
      : false;
    const pendingRequiredTasks = constructionSnapshot?.tasks.filter((task) => (
      task.required && !['completed', 'skipped', 'cancelled'].includes(task.status)
    )).length ?? 0;
    const pendingCandidateChanges = await packages.countPendingCandidateRevisions(project.id);
    const constructionRequired = project.kind !== 'package';
    const releaseReadiness = buildReleaseReadiness({
      currentRevision: workflowPackage?.revision.oid,
      packageCheck,
      warningReview: warningReview ?? undefined,
      flowchartConfirmed,
      flowchartRequired: constructionRequired,
      constructionRequired,
      constructionCompleted: constructionSnapshot?.status === 'completed'
        && constructionSnapshot.milestones.some((milestone) => (
          milestone.id === 'construction_completed' && milestone.status === 'confirmed'
        )),
      pendingRequiredTasks,
      pendingCandidateChanges,
      latestRound,
      latestResults,
      requiredTestcases,
    });
    const releaseAvailable = Boolean(project.workflowProfile.releaseRequested && workflowPackage);
    const workflow = buildProjectWorkflow({
      project,
      documents,
      nodes,
      currentPackage: workflowPackage,
      packageCheck,
      dataSource,
      testcases,
      rounds,
      latestResults,
      diagnoses: latestRound ? listFailureDiagnoses(project.id, latestRound.id) : [],
      suggestions: latestRound ? m4repo.listSuggestions(project.id, latestRound.id) : [],
      authoring,
      progress,
      aiConstruction: constructionSnapshot,
      releaseAvailable,
      releaseReady: releaseReadiness.ready,
    });
    const messagePage = messages.page(project.id);
    return {
      project,
      documents,
      nodes,
      dataSource,
      authoring,
      workflow,
      aiConstruction: constructionSnapshot,
      aiConstructionMessages: messagePage.messages,
      aiConstructionMessagesPage: {
        hasMore: messagePage.hasMore,
        oldestSeq: messagePage.oldestSeq,
        newestSeq: messagePage.newestSeq,
      },
      release: {
        available: releaseAvailable,
        released: currentPackage?.spec.status === 'released',
        version: currentPackage?.packageVersion ?? null,
        revision: currentPackage?.revision.oid ?? null,
        warningReview,
        ...releaseReadiness,
      },
      currentRevision: publicRevision(latestRevision),
      // 后端正在跑的长耗时操作。生成脚本方案这类操作全程在请求里跑，期间待办仍是
      // pending，刷新页面会看到一个「像是没提交过」的表单，再点就撞租约报「项目正在处理中」。
      // 把它如实告诉界面，待办才能显示成「已提交，正在处理」。
      activeOperation: (() => {
        const lease = leases.current(project.id);
        return lease ? { operation: lease.operation, since: lease.acquiredAt } : null;
      })(),
      cursor: authoring.lastEventSeq,
    };
  };

  app.get('/api/projects/:id/session', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    return readProjectSession(project, { advance: false });
  });

  app.post('/api/projects/:id/session/advance', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    return readProjectSession(project, { advance: true });
  });

  app.get('/api/projects/:id/ai-construction/messages', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const query = (req.query ?? {}) as { beforeSeq?: string; limit?: string };
    const beforeSeq = query.beforeSeq === undefined ? undefined : Number(query.beforeSeq);
    const limit = query.limit === undefined ? 100 : Number(query.limit);
    if ((beforeSeq !== undefined && (!Number.isSafeInteger(beforeSeq) || beforeSeq < 1))
      || !Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
      return reply.code(400).send({ error: '消息读取范围不正确', code: 'INVALID_INPUT' });
    }
    return messages.page(project.id, { beforeSeq, limit });
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
      }, async () => {
        ensureProjectGraph(project);
        let appliedCommand: AiConstructionTaskGraphCommand = command;
        let validationCandidate: Awaited<ReturnType<PackageArtifactStore['current']>> = null;
        let confirmedPackage: Awaited<ReturnType<PackageArtifactStore['current']>> = null;
        if (command.type === 'confirm_milestone') {
          assertNoActivePackageDraft(project.id, `milestone-confirm:${command.milestoneId}`);
          const currentGraph = graph.snapshot(project.id);
          if (!currentGraph || currentGraph.revision !== command.expectedRevision) {
            throw new AiConstructionTaskGraphError(
              currentGraph ? 'REVISION_CONFLICT' : 'GRAPH_NOT_FOUND',
              currentGraph
                ? `任务图已更新，请基于 revision ${currentGraph.revision} 重试`
                : `项目尚未初始化 AI 辅助构建：${project.id}`,
            );
          }
          confirmedPackage = await packages.current(project.id);
          if (!confirmedPackage) {
            return reply.code(409).send({
              error: '当前项目没有可确认的知识包修订', code: 'PACKAGE_NOT_FOUND',
            });
          }
          appliedCommand = { ...command, packageRevisionOid: confirmedPackage.revision.oid };
        }
        if (appliedCommand.type === 'confirm_milestone'
          && appliedCommand.milestoneId === 'construction_completed') {
          const currentPackage = confirmedPackage!;
          const check = getCheck(project.id);
          if (!check || check.revision !== currentPackage.revision.oid) {
            return reply.code(409).send({
              error: '当前知识包修订尚未完成检查，请先重新执行知识包校验',
              code: 'PACKAGE_CHECK_REQUIRED',
              nextActions: [
                { id: 'run_package_check', label: '立即执行知识包校验', style: 'primary' },
              ],
            });
          }
          const errorCount = check.findings.filter((finding) => finding.severity === '错误').length;
          if (errorCount > 0) {
            const deferral = getPackageErrorDeferral(project.id, currentPackage.revision.oid);
            if (!deferral?.valid) {
              return reply.code(409).send({
                error: `知识包检查还有 ${errorCount} 个错误，请修复或明确暂缓后再完成建模`,
                code: 'PACKAGE_CHECK_FAILED',
                errorCount,
                nextActions: [
                  { id: 'repair_knowledge_structure', label: '逐项修缮这些错误', style: 'primary' },
                  { id: 'defer_validation_errors', label: '说明理由并暂缓', style: 'secondary' },
                  { id: 'view_validation', label: '查看校验详情', style: 'secondary' },
                ],
              });
            }
          }
          const warningReview = getPackageWarningReview(project.id, currentPackage.revision.oid);
          if (!warningReview.readyForValidation) {
            return reply.code(409).send({
              error: `还有 ${warningReview.pendingValidation} 条非阻断警告尚未查看影响并接受`,
              code: 'PACKAGE_WARNINGS_UNREVIEWED',
              warningReview,
            });
          }
          const evidenceProblems = findKnowledgeEvidenceProblems(currentPackage.spec);
          if (evidenceProblems.length > 0) {
            return reply.code(409).send({
              error: `还有 ${evidenceProblems.length} 项确认知识缺少可追溯证据`,
              code: 'KNOWLEDGE_EVIDENCE_REQUIRED',
              problems: evidenceProblems,
              nextActions: evidenceProblemActions(evidenceProblems),
            });
          }
          // 与助手栏的渲染规则保持一致：修订变化后失效的受控修复待办不再阻塞，
          // 否则会出现「被拦住但界面上找不到要处理的事项」的死路。
          const blockingInteractions = blockingPendingInteractions(
            state.snapshot(project.id).pendingInteractions,
            currentPackage.revision.oid,
          );
          if (blockingInteractions.length > 0) {
            return reply.code(409).send({
              error: `还有 ${blockingInteractions.length} 个待确认事项未处理`,
              code: 'PENDING_INTERACTIONS',
              pendingInteractions: blockingInteractions,
              nextActions: pendingInteractionActions(blockingInteractions),
            });
          }
          const version = nextValidationCandidateVersion({
            currentVersion: currentPackage.packageVersion,
            currentStatus: currentPackage.spec.status ?? 'draft',
          });
          validationCandidate = await packages.saveCurrentAsPackageVersion(project.id, {
            expectedRevision: currentPackage.revision.oid,
            version,
            status: 'review',
            note: 'AI 辅助构建完成，创建验证基线',
            action: {
              cause: 'validation_candidate',
              summary: `创建验证候选 ${version}`,
              actorKind: 'user',
              actorName: req.user!.username,
            },
          });
          appliedCommand = { ...appliedCommand, packageRevisionOid: validationCandidate.revision.oid };
        }

        let aiConstruction = graph.apply(project.id, appliedCommand);
        let aiConstructionMessage;
        if (appliedCommand.type === 'confirm_milestone'
          && appliedCommand.milestoneId !== 'construction_completed') {
          const currentPackage = confirmedPackage!;
          const advanced = orchestrator.advanceAfterMilestoneConfirmation(
            project.id,
            aiConstruction,
            appliedCommand.milestoneId === 'knowledge_confirmed'
              ? {
                scriptCandidates: identifyScriptCandidates(
                  currentPackage.spec.operations,
                  currentPackage.spec.metrics ?? [],
                ),
              }
              : {},
          );
          aiConstruction = advanced.aiConstruction;
          aiConstructionMessage = advanced.message;
        }
        if (validationCandidate) {
          state.apply({
            kind: 'publish_event',
            projectId: project.id,
            type: 'package.revision.created',
            payload: {
              revisionId: validationCandidate.revision.oid,
              cause: 'validation_candidate',
              packageVersion: validationCandidate.packageVersion,
            },
          });
        }
        state.apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'ai_construction.graph.updated',
          payload: { ...commandEvent(appliedCommand), revision: aiConstruction.revision },
        });
        return {
          aiConstruction,
          ...(aiConstructionMessage ? { aiConstructionMessage } : {}),
          ...(validationCandidate ? {
            package: {
              version: validationCandidate.packageVersion,
              revision: validationCandidate.revision.oid,
              status: validationCandidate.spec.status ?? 'draft',
            },
          } : {}),
          cursor: state.snapshot(project.id).lastEventSeq,
        };
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
        ensureProjectGraph(project);
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
        ensureProjectGraph(project);
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

  app.post('/api/projects/:id/ai-construction/modeling-brief/revise', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '重新填写建模目标',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => {
        ensureProjectGraph(project);
        const currentPackage = await packages.current(project.id);
        if (!currentPackage) {
          return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
        }
        const brief = orchestrator.latestModelingBrief(project.id);
        if (!brief) {
          return reply.code(409).send({ error: '当前没有可修改的建模目标', code: 'MODELING_BRIEF_NOT_FOUND' });
        }
        const result = orchestrator.reviseModelingBrief(project.id, {
          packageRevisionOid: currentPackage.revision.oid,
          brief,
        });
        return { ...result, cursor: state.snapshot(project.id).lastEventSeq };
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/knowledge/generate', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '生成知识结构',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
      }, async () => {
        ensureProjectGraph(project);
        assertNoActivePackageDraft(project.id, 'knowledge-generate');
        const { flowchart } = await readConfirmedFlowchart(project.id);
        const documents = listDocuments(project.id);
        const context = {
          tables: documents.flatMap((document) => (
            document.tables?.map((table) => ({ ...table, documentId: document.id })) ?? []
          )),
          documents: documents.map((document) => ({
            id: document.id,
            filename: document.filename,
            ...(document.textContent ? { textContent: document.textContent } : {}),
          })),
        };
        const result = await knowledgeRunner.run(project, context, flowchart);
        return { ...result, cursor: state.snapshot(project.id).lastEventSeq };
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/scripts/defer', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '本轮暂缓确定性脚本',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => {
        ensureProjectGraph(project);
        const currentPackage = await packages.current(project.id);
        if (!currentPackage) {
          return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
        }
        const result = orchestrator.deferScriptGeneration(project.id, currentPackage.revision.oid);
        return { ...result, cursor: state.snapshot(project.id).lastEventSeq };
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/scripts/retry', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '重新生成确定性脚本',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => {
        ensureProjectGraph(project);
        const currentPackage = await packages.current(project.id);
        if (!currentPackage) {
          return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
        }
        const priorContracts = (getDb().prepare(`SELECT result_json FROM authoring_runs
          WHERE project_id = ? AND result_json IS NOT NULL ORDER BY created_at DESC, rowid DESC`)
          .all(project.id) as Array<{ result_json: string }>).flatMap((row) => {
          try {
            const result = JSON.parse(row.result_json);
            return result?.purpose === 'script_contract' ? [result] : [];
          } catch {
            return [];
          }
        })[0];
        const contractByOperation = new Map<string, string>(
          (Array.isArray(priorContracts?.contracts) ? priorContracts.contracts : [])
            .map((contract: any): [string, string] => [
              String(contract?.operationKey ?? ''), String(contract?.contract ?? ''),
            ]),
        );
        const storedCandidates = Array.isArray(priorContracts?.candidates)
          ? priorContracts.candidates as ScriptCandidate[] : [];
        const candidates = storedCandidates.length
          ? storedCandidates.map((candidate) => ({
            ...candidate,
            suggestedContract: contractByOperation.get(candidate.operationKey)
              || candidate.suggestedContract,
          }))
          : identifyScriptCandidates(
            currentPackage.spec.operations,
            currentPackage.spec.metrics ?? [],
          );
        const result = orchestrator.retryScriptGeneration(project.id, {
          packageRevisionOid: currentPackage.revision.oid,
          candidates,
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
      }, () => {
        ensureProjectGraph(project);
        assertNoActivePackageDraft(project.id, 'controlled-repair');
        return controlledRepair.run(project);
      });
    } catch (error) {
      return sendStateError(reply, error);
    }
  });

  app.post('/api/projects/:id/ai-construction/knowledge/defer-errors', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const reason = String((req.body as any)?.reason ?? '').trim();
    if (reason.length < 4 || reason.length > 2_000) {
      return reply.code(400).send({ error: '请填写有效的暂缓原因', code: 'INVALID_INPUT' });
    }
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '暂缓知识包阻断错误',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => {
        ensureProjectGraph(project);
        const currentPackage = await packages.current(project.id);
        if (!currentPackage) {
          return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
        }
        const check = getCheck(project.id);
        if (!check || check.revision !== currentPackage.revision.oid) {
          return reply.code(409).send({
            error: '当前知识包修订尚未完成检查，请先重新执行知识包校验',
            code: 'PACKAGE_CHECK_REQUIRED',
          });
        }
        const errorCount = check.findings.filter((finding) => finding.severity === '错误').length;
        if (!errorCount) {
          return reply.code(409).send({ error: '当前知识包没有阻断错误', code: 'NO_BLOCKING_ERRORS' });
        }
        const db = getDb();
        db.exec('BEGIN IMMEDIATE');
        try {
          const result = orchestrator.deferKnowledgeErrors(project.id, {
            packageRevisionOid: currentPackage.revision.oid,
            errorCount,
            reason,
            actor: req.user!.username,
          });
          const deferral = deferPackageErrors({
            projectId: project.id,
            revision: currentPackage.revision.oid,
            reason,
            actor: req.user!.username,
            actorRole: 'project_owner',
          });
          state.apply({
            kind: 'publish_event',
            projectId: project.id,
            type: 'package.validation.errors.deferred',
            payload: { revision: deferral.revision, errorCount: deferral.errorCount },
          });
          db.exec('COMMIT');
          return { ...result, deferral };
        } catch (error) {
          if (db.isTransaction) db.exec('ROLLBACK');
          throw error;
        }
      });
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
        ensureProjectGraph(project);
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
        ensureProjectGraph(project);
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
      ensureProjectGraph(project);
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
        }, () => {
          if (approving) assertNoActivePackageDraft(project.id, 'controlled-repair-apply');
          return controlledRepair.respond(
            project,
            repairInteraction,
            requestId,
            response,
            req.user!.username,
          );
        });
      }
      const interaction = state.interaction(project.id, interactionId);
      if (interaction?.type === 'question_batch' && interaction.payload.purpose === 'modeling_brief') {
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: '确认建模目标',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
        }, async () => {
          if (interaction.status !== 'pending') {
            const replayed = adapter.respond(project.id, interactionId, requestId, response);
            return {
              interaction: replayed.interaction,
              run: replayed.run,
              replayed: replayed.replayed,
              aiConstruction: graph.snapshot(project.id),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }
          const currentPackage = await packages.current(project.id);
          if (!currentPackage) {
            return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
          }
          if (interaction.baseRevision && interaction.baseRevision !== currentPackage.revision.oid) {
            return reply.code(409).send({
              error: '知识包修订已经变化，请重新填写建模目标',
              code: 'REVISION_CONFLICT',
            });
          }
          const brief = await resolveModelingBrief(response);
          const db = getDb();
          db.exec('BEGIN IMMEDIATE');
          try {
            const answered = adapter.respond(project.id, interactionId, requestId, response);
            const construction = orchestrator.completeModelingBrief(project.id, {
              taskId: String(interaction.payload.taskId ?? ''),
              packageRevisionOid: currentPackage.revision.oid,
              messageKey: `modeling.brief.${interaction.id}`,
              brief,
            });
            const run = adapter.complete(project.id, answered.run.id, {
              purpose: 'modeling_brief',
              problem: brief.problem,
              targetUsers: brief.targetUsers,
              scenarios: brief.scenarios,
              successCriteria: brief.successCriteria,
              packageRevisionOid: currentPackage.revision.oid,
            }).run;
            db.exec('COMMIT');
            return {
              interaction: answered.interaction,
              run,
              replayed: answered.replayed,
              ...construction,
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          } catch (error) {
            if (db.isTransaction) db.exec('ROLLBACK');
            throw error;
          }
        });
      }
      if (interaction?.type === 'question_batch' && interaction.payload.purpose === 'process_interview') {
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: '整理并提交业务流程访谈',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
        }, async () => {
          if (interaction.status !== 'pending') {
            const replayed = adapter.respond(project.id, interactionId, requestId, response);
            return {
              interaction: replayed.interaction,
              run: replayed.run,
              replayed: replayed.replayed,
              aiConstruction: graph.snapshot(project.id),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }
          const currentPackage = await packages.current(project.id);
          if (!currentPackage) {
            return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
          }
          if (interaction.baseRevision && interaction.baseRevision !== currentPackage.revision.oid) {
            return reply.code(409).send({
              error: '知识包修订已经变化，请重新确认候选业务流程',
              code: 'REVISION_CONFLICT',
            });
          }
          const stage = String(interaction.payload.stage ?? 'core');
          const questions = Array.isArray(interaction.payload.questions)
            ? interaction.payload.questions : [];
          const hasCandidateSelection = questions.some((question) => (
            question && typeof question === 'object' && !Array.isArray(question)
            && question.id === 'selected_processes'
          ));

          if (stage === 'core' && !hasCandidateSelection) {
            const core = parseProcessInterviewCoreResponse(response);
            const db = getDb();
            db.exec('BEGIN IMMEDIATE');
            try {
              const answered = adapter.respond(project.id, interactionId, requestId, response);
              const progressed = adapter.progress(project.id, answered.run.id, {
                ...answered.run.state,
                purpose: 'process_interview',
                stage: 'details',
                taskId: String(interaction.payload.taskId ?? ''),
                core: core as unknown as JsonObject,
              });
              const opened = adapter.request(
                project.id,
                progressed.run.id,
                'question_batch',
                {
                  purpose: 'process_interview',
                  stage: 'details',
                  taskId: String(interaction.payload.taskId ?? ''),
                  title: `补充“${core.title}”的流程细节`,
                  description: '请确认流程输入、关键步骤、异常处理和角色交接。只有流程步骤为必填项。',
                  core: core as unknown as JsonObject,
                  questions: processInterviewQuestions('details', core) as unknown as JsonObject[],
                },
                currentPackage.revision.oid,
              );
              db.exec('COMMIT');
              return {
                interaction: answered.interaction,
                nextInteraction: opened.interaction,
                run: opened.run,
                replayed: answered.replayed,
                aiConstruction: graph.snapshot(project.id),
                cursor: state.snapshot(project.id).lastEventSeq,
              };
            } catch (error) {
              if (db.isTransaction) db.exec('ROLLBACK');
              throw error;
            }
          }

          let outcome;
          if (stage === 'details') {
            const core = readProcessInterviewCore(interaction.payload.core);
            const details = parseProcessInterviewDetailsResponse(response);
            outcome = buildInterviewOutcome(core, details);
          } else if (hasCandidateSelection) {
          const candidates = Array.isArray(interaction.payload.candidates)
            ? interaction.payload.candidates as any[] : [];
          const selection = parseProcessSelectionResponse(response, candidates);
          const modelingBrief = orchestrator.latestConfirmedModelingBrief(project.id)
            ?? orchestrator.latestModelingBrief(project.id)
            ?? undefined;
          const resolvedCandidates = [];
          for (const candidate of selection.candidates) {
            if (isCompleteProcessCandidate(candidate)) {
              resolvedCandidates.push(candidate);
              continue;
            }
            resolvedCandidates.push(await processDraftGenerator({
              description: [
                '请补全资料候选中缺失的流程信息，不得编造数值或制度。',
                JSON.stringify(candidate),
              ].join('\n'),
              candidateProcess: candidate,
              existingProcessTitles: resolvedCandidates.map((item) => item.title),
              modelingBrief,
            }));
          }
          if (selection.customNote) {
            resolvedCandidates.push(await processDraftGenerator({
              description: selection.customNote,
              existingProcessTitles: resolvedCandidates.map((item) => item.title),
              modelingBrief,
            }));
          }
            outcome = buildInterviewOutcomeFromCandidates(resolvedCandidates);
          } else {
            throw new ProcessInterviewInputError('流程访谈阶段不正确，请刷新后重试');
          }
          const answered = adapter.respond(project.id, interactionId, requestId, response);
          const construction = orchestrator.completeProcessInterview(project.id, {
            taskId: String(interaction.payload.taskId ?? ''),
            packageRevisionOid: currentPackage.revision.oid,
            messageKey: `process.interview.${interaction.id}`,
            outcome,
          });
          const run = adapter.complete(project.id, answered.run.id, {
            purpose: 'process_interview',
            packageRevisionOid: currentPackage.revision.oid,
            processCount: outcome.processes.length,
          }).run;
          return {
            interaction: answered.interaction,
            run,
            replayed: answered.replayed,
            ...construction,
            cursor: state.snapshot(project.id).lastEventSeq,
          };
        });
      }
      if (interaction?.type === 'decision' && interaction.payload.purpose === 'generation_gate_skip') {
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: '继续生成知识结构',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
        }, async () => {
          assertNoActivePackageDraft(project.id, 'knowledge-generation-resume');
          const answered = adapter.respond(project.id, interactionId, requestId, response);
          if (answered.replayed && answered.run.status === 'completed') {
            return {
              interaction: answered.interaction,
              run: answered.run,
              replayed: true,
              aiConstruction: graph.snapshot(project.id),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }
          const resolvedContinuation = resolveGenerationContinuation(answered.interaction);
          const { currentPackage, flowchart } = await readConfirmedFlowchart(project.id);
          let continuation = resolvedContinuation;
          if (answered.run.status === 'failed') {
            const resumed = adapter.start(
              project.id,
              '生成知识包',
              currentPackage.revision.oid,
              {
                purpose: 'knowledge_generation_resume',
                priorRunId: answered.run.id,
                skipFailedPhases: resolvedContinuation.skipFailedPhases,
              },
            );
            continuation = { ...resolvedContinuation, resumeRunId: resumed.run.id };
          } else if (answered.run.status !== 'running') {
            return reply.code(409).send({
              error: '原知识生成任务已结束，请重新发起知识生成',
              code: 'GENERATION_RUN_NOT_RESUMABLE',
            });
          }
          const documents = listDocuments(project.id);
          const context = {
            tables: documents.flatMap((document) => (
              document.tables?.map((table) => ({ ...table, documentId: document.id })) ?? []
            )),
            documents: documents.map((document) => ({
              id: document.id,
              filename: document.filename,
              ...(document.textContent ? { textContent: document.textContent } : {}),
            })),
          };
          const result = await knowledgeRunner.run(project, context, flowchart, continuation);
          return {
            interaction: answered.interaction,
            run: answered.run,
            replayed: false,
            ...result,
            cursor: state.snapshot(project.id).lastEventSeq,
          };
        });
      }
      if (interaction?.type === 'question_batch' && interaction.payload.purpose === 'script_decision') {
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: '确认确定性脚本需求',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: SHORT_WRITE_LEASE_TTL_MS,
        }, async () => {
          if (interaction.status !== 'pending') {
            const replayed = adapter.respond(project.id, interactionId, requestId, response);
            return {
              interaction: replayed.interaction,
              run: replayed.run,
              replayed: replayed.replayed,
              aiConstruction: graph.snapshot(project.id),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }
          const currentPackage = await packages.current(project.id);
          if (!currentPackage) {
            return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
          }
          if (interaction.baseRevision && interaction.baseRevision !== currentPackage.revision.oid) {
            return reply.code(409).send({
              error: '知识包修订已经变化，请基于当前修订重新选择脚本候选',
              code: 'REVISION_CONFLICT',
            });
          }
          const candidates = (Array.isArray(interaction.payload.candidates)
            ? interaction.payload.candidates : []) as unknown as ScriptCandidate[];
          const decision = parseScriptDecisionResponse(response, candidates);
          const selected = candidates.filter((candidate) => (
            decision.selectedOperationKeys.includes(candidate.operationKey)
          ));
          const answered = adapter.respond(project.id, interactionId, requestId, response);
          const construction = orchestrator.completeScriptDecision(project.id, {
            taskId: String(interaction.payload.taskId ?? ''),
            packageRevisionOid: currentPackage.revision.oid,
            selectedCandidates: selected,
            messageKey: `script.decision.${interaction.id}`,
          });
          const run = adapter.complete(project.id, answered.run.id, {
            selectedOperationKeys: decision.selectedOperationKeys,
          }).run;
          return {
            interaction: answered.interaction,
            run,
            replayed: answered.replayed,
            ...construction,
            cursor: state.snapshot(project.id).lastEventSeq,
          };
        });
      }
      if (interaction?.type === 'question_batch' && interaction.payload.purpose === 'script_contract') {
        if (interaction.status !== 'pending') {
          const replayed = adapter.respond(project.id, interactionId, requestId, response);
          return {
            interaction: replayed.interaction,
            run: replayed.run,
            replayed: replayed.replayed,
            aiConstruction: graph.snapshot(project.id),
            cursor: state.snapshot(project.id).lastEventSeq,
          };
        }
        const currentPackage = await packages.current(project.id);
        if (!currentPackage) {
          return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
        }
        if (interaction.baseRevision && interaction.baseRevision !== currentPackage.revision.oid) {
          return reply.code(409).send({
            error: '知识包修订已经变化，请重新确认脚本契约',
            code: 'REVISION_CONFLICT',
          });
        }
        const candidates = (Array.isArray(interaction.payload.candidates)
          ? interaction.payload.candidates : []) as unknown as ScriptCandidate[];
        // 入参解析要同步做：格式不对必须当场告诉用户，不能受理完再在后台失败
        const contracts = parseScriptContractResponse(response, candidates);
        const requirement = buildScriptGenerationRequirement(candidates, contracts);

        // 先受理再干活。原先 respond 排在十几分钟的生成之后，那段时间待办在库里
        // 仍是 pending：刷新页面它原样回来，看着像没提交过，再点就撞上租约。
        // 提前 respond 之后失败就不能靠原待办重试了——出口改由 failScriptGeneration
        // 给的「重新生成脚本」承担，那是条独立路由，不依赖待办状态。
        const answered = adapter.respond(project.id, interactionId, requestId, response);
        const construction = orchestrator.startScriptGeneration(project.id);
        const runId = answered.run.id;

        void jobs.run(`${project.id}:script-generation`, '生成确定性脚本方案', async () => {
          await runWithProjectLease(leases, {
            projectId: project.id,
            operation: '生成确定性脚本方案',
            owner: `${req.user!.username}:${req.id}`,
            ttlMs: ASSISTED_WRITE_LEASE_TTL_MS,
          }, async () => {
            const planned = await planPackageChat(project, [], requirement, {
              purpose: 'script_generation',
              intent: '生成确定性脚本',
              interactionTitle: '确认确定性脚本变更方案',
              metadata: {
                scriptTaskId: String(interaction.payload.taskId ?? ''),
                scriptCount: candidates.length,
                contractInteractionId: interaction.id,
              },
            });
            if (!planned.interaction) {
              throw new Error(planned.reply?.trim() || '未能生成可审阅的脚本变更方案');
            }
            adapter.complete(project.id, runId, {
              purpose: 'script_contract',
              scriptPlanInteractionId: planned.interaction.id,
              packageRevisionOid: currentPackage.revision.oid,
              candidates: candidates as unknown as JsonObject[],
              contracts: contracts as unknown as JsonObject[],
              requirement,
            });
            orchestrator.waitForScriptApproval(project.id);
          });
        }).catch((error) => {
          const reason = error instanceof Error ? error.message : String(error);
          // 失败收尾必须自己兜住：这时已经没有 HTTP 响应可以承载错误了
          try { orchestrator.failScriptGeneration(project.id, reason); } catch { /* 收尾失败不再抛 */ }
          try { adapter.fail(project.id, runId, reason); } catch { /* 同上 */ }
        });

        return reply.code(202).send({
          accepted: true,
          interaction: answered.interaction,
          run: answered.run,
          replayed: answered.replayed,
          ...construction,
          cursor: state.snapshot(project.id).lastEventSeq,
        });
      }
      if (interaction?.type === 'change_plan'
        && ['package_chat', 'script_generation'].includes(String(interaction.payload.purpose ?? ''))) {
        const scriptGeneration = interaction.payload.purpose === 'script_generation';
        const action = String(response.action ?? '');
        const allowedActions = scriptGeneration ? ['approve', 'reject'] : ['approve', 'reject', 'revise'];
        if (!allowedActions.includes(action)) {
          return reply.code(400).send({ error: '方案处理动作不正确', code: 'INVALID_INPUT' });
        }
        if (interaction.status !== 'pending') {
          const replayed = adapter.respond(project.id, interactionId, requestId, response);
          return {
            interaction: replayed.interaction,
            run: replayed.run,
            replayed: replayed.replayed,
            cursor: state.snapshot(project.id).lastEventSeq,
          };
        }

        const respondToPackagePlan = async () => {
          if (action === 'reject') {
            const answered = adapter.respond(project.id, interactionId, requestId, response);
            const run = adapter.complete(project.id, answered.run.id, { cancelled: true }).run;
            const conversation = scriptGeneration ? { plan: undefined, result: undefined }
              : packageChatConversation.settlePlan(
              project.id, interactionId, 'rejected',
              String(response.note ?? '').trim() || '本轮建模调整已取消。',
            );
            const construction = scriptGeneration
              ? orchestrator.failScriptGeneration(project.id, '脚本变更方案已取消，可以重新生成或暂缓本轮脚本。')
              : undefined;
            const completionMessage = !scriptGeneration && shouldRequestConstructionCompletion(
              graph.snapshot(project.id),
              state.snapshot(project.id).pendingInteractions.length,
            )
              ? packageChatConversation.requestConstructionCompletion(project.id, interactionId)
              : undefined;
            return {
              interaction: answered.interaction,
              run,
              replayed: answered.replayed,
              ...(construction ?? {}),
              aiConstructionMessages: [
                conversation.plan, conversation.result, completionMessage,
              ].filter(Boolean),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }

          if (action === 'revise') {
            const note = String(response.note ?? '').trim();
            if (!note) {
              return reply.code(400).send({ error: '请填写方案调整意见', code: 'INVALID_INPUT' });
            }
            const current = await packages.current(project.id);
            if (!current) {
              return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
            }
            if (interaction.baseRevision && interaction.baseRevision !== current.revision.oid) {
              return reply.code(409).send({
                error: '知识包修订已经变化，请基于当前版本重新规划修改',
                code: 'REVISION_CONFLICT',
              });
            }
            assertNoActivePackageDraft(project.id, 'package-chat-plan-revise');
            const validationFeedback = packageChatConversation.planFailure(project.id, interactionId);
            const previousPlan = {
              summary: String(interaction.payload.summary ?? '修改知识包'),
              changes: Array.isArray(interaction.payload.changes) ? interaction.payload.changes : [],
              risks: Array.isArray(interaction.payload.risks) ? interaction.payload.risks : [],
            };
            const revisionMessage = [
              '请调整尚未应用的知识包修改方案。',
              `原始诉求：${String(interaction.payload.message ?? '')}`,
              `原方案：${JSON.stringify(previousPlan)}`,
              validationFeedback ? `上次候选校验反馈：${validationFeedback}` : '',
              `专家调整意见：${note}`,
              '请基于当前规格重新提交一份完整、可审阅的变更方案，不要直接修改知识包。',
            ].filter(Boolean).join('\n');
            const revisionTurnId = `${requestId}:revise`;
            const history = packageChatConversation.history(project.id);
            const userMessage = packageChatConversation.beginTurn(project.id, revisionTurnId, note);
            let planned;
            try {
              planned = await planPackageChat(project, history, revisionMessage, {
                purpose: 'package_chat',
                intent: '调整知识包修改方案',
                interactionTitle: '确认调整后的建模方案',
                metadata: { previousInteractionId: interactionId },
              });
              if (!planned.interaction || !planned.plan?.changes.length) {
                throw new Error(planned.reply?.trim() || '未能生成可审阅的调整方案');
              }
            } catch (error) {
              packageChatConversation.failTurn(
                project.id, revisionTurnId, '调整方案失败', error,
              );
              throw error;
            }
            const answered = adapter.respond(project.id, interactionId, requestId, response);
            const run = adapter.complete(project.id, answered.run.id, {
              revisionRequested: true,
              nextInteractionId: planned.interaction.id,
            }).run;
            const settled = packageChatConversation.settlePlan(
              project.id, interactionId, 'rejected', '已按专家意见退回原方案，并重新生成待确认方案。',
            );
            const revisedPlanMessage = packageChatConversation.finishTurn(
              project.id, revisionTurnId, planned,
            );
            return {
              interaction: answered.interaction,
              run,
              replayed: answered.replayed,
              revisedPlan: planned,
              aiConstructionMessages: [
                userMessage, settled.plan, settled.result, revisedPlanMessage,
              ].filter(Boolean),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          }

          assertNoActivePackageDraft(project.id, 'package-chat-apply');
          const current = await packages.current(project.id);
          if (!current) {
            return reply.code(409).send({ error: '当前项目没有知识包草稿', code: 'PACKAGE_NOT_FOUND' });
          }
          if (interaction.baseRevision && interaction.baseRevision !== current.revision.oid) {
            return reply.code(409).send({
              error: '知识包修订已经变化，请基于当前版本重新规划修改',
              code: 'REVISION_CONFLICT',
            });
          }
          const allChanges = Array.isArray(interaction.payload.changes)
            ? interaction.payload.changes
            : [];
          const selected = new Set(Array.isArray(response.selectedChangeIds)
            ? response.selectedChangeIds.map(String)
            : []);
          const changes = allChanges
            .filter((change: any) => !selected.size || selected.has(String(change?.id)))
            .map(({ id: _id, ...change }: any) => change);
          if (!changes.length) {
            return reply.code(400).send({ error: '请至少选择一项修改', code: 'INVALID_INPUT' });
          }
          const plan: ChangePlan = {
            summary: String(interaction.payload.summary ?? '修改知识包'),
            changes,
            risks: Array.isArray(interaction.payload.risks)
              ? interaction.payload.risks.map(String)
              : [],
          };
          const documents = listDocuments(project.id);
          const tables = documents.flatMap((document) => (
            document.tables?.map((table) => ({ ...table, documentId: document.id })) ?? []
          ));
          let validatedScriptFindings: CheckFinding[] | undefined;
          const validateScriptCandidate = async (candidate: {
            spec: Parameters<typeof runFormatChecks>[0];
            files: Record<string, string>;
          }) => {
            const findings: CheckFinding[] = [
              ...runFormatChecks(candidate.spec, candidate.files),
              ...runRuleChecks(candidate.spec, tables, packageIntentQuestions(candidate.spec)),
            ];
            if (!findings.some((finding) => finding.severity === '错误')) {
              try {
                findings.push(...await runSemanticCheck(project, candidate.spec));
              } catch (error) {
                findings.push(semanticCheckFailureFinding(error));
              }
            }
            findings.sort((left, right) => ['错误', '警告', '提示'].indexOf(left.severity)
              - ['错误', '警告', '提示'].indexOf(right.severity));
            validatedScriptFindings = findings;
            const errors = findings.filter((finding) => finding.severity === '错误');
            return {
              accepted: errors.length === 0,
              feedback: errors.length
                ? `脚本候选仍有 ${errors.length} 个阻断错误：${errors.slice(0, 8)
                  .map((finding) => `${finding.category}：${finding.message}`).join('；')}`
                : '',
              disposition: errors.length ? 'rejected' as const : 'accepted' as const,
            };
          };
          try {
            if (scriptGeneration) orchestrator.startScriptGeneration(project.id);
            const applied = await applyPackageChat(
              project,
              Array.isArray(interaction.payload.history) ? interaction.payload.history as any : [],
              String(interaction.payload.message ?? ''),
              plan,
              tables,
              scriptGeneration ? { validateCandidate: validateScriptCandidate } : {},
            );
            const answered = adapter.respond(project.id, interactionId, requestId, response);
            const run = adapter.complete(project.id, answered.run.id, {
              ...(applied.version ? { version: applied.version } : {}),
              ...(applied.revision ? { revision: applied.revision } : {}),
            }).run;
            const conversation = scriptGeneration ? { plan: undefined, result: undefined }
              : packageChatConversation.settlePlan(
              project.id, interactionId, 'applied', applied.reply || '建模调整已应用。',
            );
            let construction;
            let validation;
            if (scriptGeneration && applied.revision) {
              const snapshot = await packages.readPackageFiles(project.id, applied.revision);
              if (!validatedScriptFindings) await validateScriptCandidate(snapshot);
              const findings = validatedScriptFindings ?? [];
              validation = {
                errors: findings.filter((finding) => finding.severity === '错误').length,
                warnings: findings.filter((finding) => finding.severity === '警告').length,
                hints: findings.filter((finding) => finding.severity === '提示').length,
              };
              saveCheck(project.id, applied.version ?? snapshot.spec.version, findings, applied.revision);
              construction = orchestrator.completeScriptGeneration(project.id, {
                packageRevisionOid: applied.revision,
                messageKey: `script.generated.${applied.revision}`,
                scriptCount: snapshot.spec.scripts?.length ?? 0,
                validation,
              });
            }
            return {
              interaction: answered.interaction,
              run,
              replayed: answered.replayed,
              result: applied,
              ...(validation ? { validation } : {}),
              ...(construction ?? {}),
              aiConstructionMessages: [conversation.plan, conversation.result].filter(Boolean),
              cursor: state.snapshot(project.id).lastEventSeq,
            };
          } catch (error) {
            if (scriptGeneration) {
              try {
                orchestrator.failScriptGeneration(
                  project.id,
                  error instanceof Error ? error.message : String(error),
                );
              } catch { /* 保留原始失败 */ }
            } else if (error instanceof PackageCandidateRejectedError) {
              const feedbackMessage = packageChatConversation.recordPlanFailure(
                project.id, interactionId, error.feedback,
              );
              const feedback = error.feedback.trim().slice(0, 180);
              return reply.code(422).send({
                error: `候选未通过校验，请调整方案：${feedback}`,
                code: error.code,
                feedback: error.feedback,
                interactionId,
                aiConstructionMessages: [feedbackMessage],
                cursor: state.snapshot(project.id).lastEventSeq,
              });
            } else {
              packageChatConversation.failTurn(
                project.id, requestId, '应用建模调整失败', error,
              );
            }
            throw error;
          }
        };

        const revising = action === 'revise';
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: action === 'approve'
            ? scriptGeneration ? '应用确定性脚本方案' : '应用建模调整方案'
            : revising ? '调整建模修改方案'
            : scriptGeneration ? '取消确定性脚本方案' : '取消建模调整方案',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: action === 'reject' ? SHORT_WRITE_LEASE_TTL_MS : ASSISTED_WRITE_LEASE_TTL_MS,
        }, respondToPackagePlan);
      }
      if (interaction?.type === 'flowchart_review' && interaction.payload.purpose === 'process_review') {
        return await runWithProjectLease(leases, {
          projectId: project.id,
          operation: '提交业务流程图',
          owner: `${req.user!.username}:${req.id}`,
          ttlMs: SHORT_WRITE_LEASE_TTL_MS,
        }, async () => {
          assertNoActivePackageDraft(project.id, 'flowchart-confirm');
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
      if (interaction?.type === 'scenario_review') {
        return reply.code(409).send({
          error: '场景审核必须通过专用接口处理',
          code: 'SCENARIO_REVIEW_ENDPOINT_REQUIRED',
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
