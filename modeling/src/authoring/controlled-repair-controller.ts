import { ControlledRepairRunner } from '../ai-construction/controlled-repair.js';
import { AiConstructionOrchestrator } from '../ai-construction/orchestrator.js';
import { AiConstructionTaskGraph } from '../ai-construction/task-graph.js';
import type { PackageArtifactStore } from '../artifacts/package-store.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { listDocuments } from '../documents/repo.js';
import type { Project } from '../types.js';
import {
  packageChatApply,
  type ChangePlan,
  type ChatApplyResult,
} from '../workflow/package-chat.js';
import { PiAuthoringAdapter } from './pi-adapter.js';
import { AuthoringState, getAuthoringState } from './state.js';
import type { AuthoringInteraction, JsonObject } from './types.js';

type PackageChatApply = typeof packageChatApply;

export interface ControlledRepairControllerOptions {
  state?: AuthoringState;
  graph?: AiConstructionTaskGraph;
  orchestrator?: AiConstructionOrchestrator;
  packages?: PackageArtifactStore;
  adapter?: PiAuthoringAdapter;
  runner?: ControlledRepairRunner;
  applyPlan?: PackageChatApply;
  contextForProject?: typeof contextFor;
}

export class ControlledRepairControllerError extends Error {
  constructor(
    public readonly code: 'invalid_input' | 'revision_conflict',
    message: string,
  ) {
    super(message);
    this.name = 'ControlledRepairControllerError';
  }
}

export function isControlledRepairInteraction(interaction: AuthoringInteraction): boolean {
  return interaction.type === 'change_plan' && interaction.payload.purpose === 'controlled_repair'
    || interaction.type === 'question_batch'
      && interaction.payload.purpose === 'controlled_repair_expert';
}

function contextFor(projectId: string) {
  const documents = listDocuments(projectId);
  return {
    tables: documents.flatMap((document) => (
      document.tables?.map((table) => ({ ...table, documentId: document.id })) ?? []
    )),
    documents: documents.map((document) => ({
      id: document.id,
      filename: document.filename,
      ...(document.textContent ? { textContent: document.textContent } : {}),
    })),
  };
}

function planFromInteraction(interaction: AuthoringInteraction, response: JsonObject): ChangePlan {
  const payload = interaction.payload;
  const allChanges = Array.isArray(payload.changes) ? payload.changes : [];
  const selected = new Set(Array.isArray(response.selectedChangeIds)
    ? response.selectedChangeIds.map(String)
    : []);
  const changes = allChanges
    .filter((change: any) => !selected.size || selected.has(String(change?.id)))
    .map(({ id: _id, ...change }: any) => change);
  if (!changes.length) {
    throw new ControlledRepairControllerError('invalid_input', '请至少选择一项修改');
  }
  return {
    summary: String(payload.summary ?? '修复知识包'),
    changes: changes as ChangePlan['changes'],
    risks: Array.isArray(payload.risks) ? payload.risks.map(String) : [],
  };
}

/**
 * 连接受控修复、专家交互与知识包原子应用，路由层只处理鉴权和项目租约。
 */
export class ControlledRepairController {
  private readonly state: AuthoringState;
  private readonly graph: AiConstructionTaskGraph;
  private readonly orchestrator: AiConstructionOrchestrator;
  private readonly packages: PackageArtifactStore;
  private readonly adapter: PiAuthoringAdapter;
  private readonly runner: ControlledRepairRunner;
  private readonly applyPlan: PackageChatApply;
  private readonly contextForProject: typeof contextFor;

  constructor(options: ControlledRepairControllerOptions = {}) {
    this.state = options.state ?? getAuthoringState();
    this.graph = options.graph ?? new AiConstructionTaskGraph();
    this.packages = options.packages ?? getPackageArtifactStore();
    this.adapter = options.adapter ?? new PiAuthoringAdapter(this.state);
    this.orchestrator = options.orchestrator ?? new AiConstructionOrchestrator({
      graph: this.graph,
      state: this.state,
    });
    this.runner = options.runner ?? new ControlledRepairRunner({
      orchestrator: this.orchestrator,
      packages: this.packages,
      adapter: this.adapter,
      state: this.state,
    });
    this.applyPlan = options.applyPlan ?? packageChatApply;
    this.contextForProject = options.contextForProject ?? contextFor;
  }

  interaction(projectId: string, interactionId: string): AuthoringInteraction | null {
    const interaction = this.state.interaction(projectId, interactionId);
    return interaction && isControlledRepairInteraction(interaction) ? interaction : null;
  }

  async run(project: Project) {
    const result = await this.runner.run(project, this.contextForProject(project.id));
    this.state.apply({
      kind: 'publish_event',
      projectId: project.id,
      type: result.status === 'stopped'
        ? 'ai_construction.knowledge.repair_stopped'
        : result.interaction
          ? 'ai_construction.knowledge.repair_waiting'
          : 'ai_construction.knowledge.repaired',
      payload: {
        status: result.status,
        graphRevision: result.aiConstruction.revision,
        packageRevision: result.revision,
        validationErrors: result.validation.errors,
        attempts: result.attempts.length,
        ...(result.interaction ? { interactionId: result.interaction.id } : {}),
      },
    });
    return {
      status: result.status,
      revision: result.revision,
      validation: result.validation,
      attempts: result.attempts,
      ...(result.interaction ? { interaction: result.interaction } : {}),
      aiConstruction: result.aiConstruction,
      aiConstructionMessage: result.message,
      cursor: this.state.snapshot(project.id).lastEventSeq,
    };
  }

  async respond(
    project: Project,
    interaction: AuthoringInteraction,
    requestId: string,
    response: JsonObject,
    actor: string,
  ) {
    if (!isControlledRepairInteraction(interaction)) {
      throw new ControlledRepairControllerError('invalid_input', '待办不是受控修复交互');
    }
    if (interaction.status !== 'pending') {
      const replayed = this.adapter.respond(project.id, interaction.id, requestId, response);
      return {
        interaction: replayed.interaction,
        run: replayed.run,
        replayed: replayed.replayed,
        aiConstruction: this.graph.snapshot(project.id),
        cursor: this.state.snapshot(project.id).lastEventSeq,
      };
    }

    const current = await this.packages.current(project.id);
    if (!current) throw new ControlledRepairControllerError('revision_conflict', '当前项目没有知识包草稿');
    if (interaction.baseRevision && interaction.baseRevision !== current.revision.oid) {
      throw new ControlledRepairControllerError(
        'revision_conflict',
        '知识包已更新，请重新发起受控修复',
      );
    }

    if (interaction.type === 'question_batch') {
      try {
        const next = await this.runner.answerExpertDecision(
          project,
          interaction,
          requestId,
          response,
          { username: actor, role: 'domain_expert' },
        );
        return {
          interaction: next.interaction,
          run: next.run,
          replayed: next.replayed,
          aiConstruction: this.graph.snapshot(project.id),
          cursor: this.state.snapshot(project.id).lastEventSeq,
        };
      } catch (error) {
        if (error instanceof Error
          && /(明确填写业务决策|明确要修改的资源 key)/.test(error.message)) {
          throw new ControlledRepairControllerError('invalid_input', error.message);
        }
        throw error;
      }
    }

    const action = String(response.action ?? '');
    if (!['approve', 'reject'].includes(action)) {
      throw new ControlledRepairControllerError('invalid_input', '修复方案处理动作不正确');
    }
    const attemptId = String(interaction.payload.repairAttemptId ?? '').trim();
    if (!attemptId) {
      throw new ControlledRepairControllerError('invalid_input', '受控修复审计上下文已失效');
    }

    if (action === 'reject') {
      const update = this.runner.cancelPlan(
        project.id,
        attemptId,
        String(response.note ?? '').trim() || '用户取消了本轮语义修复方案',
      );
      const answered = this.adapter.respond(project.id, interaction.id, requestId, response);
      const run = this.adapter.complete(project.id, answered.run.id, { cancelled: true }).run;
      return {
        interaction: answered.interaction,
        run,
        replayed: answered.replayed,
        aiConstruction: update.aiConstruction,
        aiConstructionMessage: update.message,
        cursor: this.state.snapshot(project.id).lastEventSeq,
      };
    }

    const plan = planFromInteraction(interaction, response);
    const context = this.contextForProject(project.id);
    const inputRevision = current.revision.oid;
    this.orchestrator.startKnowledgeRepair(project.id, {
      inputRevisionOid: inputRevision,
      messageKey: `repair.apply.${interaction.id}`,
    });
    let applied: ChatApplyResult;
    try {
      applied = await this.applyPlan(
        project,
        Array.isArray(interaction.payload.history) ? interaction.payload.history as any : [],
        String(interaction.payload.message ?? ''),
        plan,
        context.tables,
      );
      const completed = await this.runner.completeApprovedPlan(project, context, attemptId);
      const answered = this.adapter.respond(project.id, interaction.id, requestId, response);
      const run = this.adapter.complete(project.id, answered.run.id, {
        ...(applied.version ? { version: applied.version } : {}),
        revision: completed.current.revision.oid,
      }).run;
      return {
        interaction: answered.interaction,
        run,
        replayed: answered.replayed,
        result: applied,
        revision: completed.current.revision.oid,
        validation: completed.validation,
        aiConstruction: completed.aiConstruction,
        aiConstructionMessage: completed.message,
        cursor: this.state.snapshot(project.id).lastEventSeq,
      };
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      const latest = await this.packages.current(project.id).catch(() => null);
      if (latest && latest.revision.oid !== inputRevision) {
        try { this.adapter.respond(project.id, interaction.id, requestId, response); } catch { /* 已收敛 */ }
        try { this.adapter.fail(project.id, interaction.runId, reason); } catch { /* 已收敛 */ }
        this.runner.failAppliedPlan(project.id, attemptId, reason, latest.revision.oid);
      } else {
        this.runner.failPlan(project.id, attemptId, reason);
      }
      throw error;
    }
  }
}
