import { createHash } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { AnalysisResult, DocumentRecord, InterviewOutcome } from '../types.js';
import { getDb } from '../db/index.js';
import { AuthoringState, getAuthoringState } from '../authoring/state.js';
import type { AuthoringInteraction, JsonObject } from '../authoring/types.js';
import { buildFlowchart } from '../authoring/workflow-extras.js';
import type { GenerationGateFailure } from '../workflow/generation-gate.js';
import {
  buildMaterialAnalysisFailureBlocks,
  buildMaterialAnalysisProgressBlocks,
} from './material-analysis-progress.js';
import { AiConstructionMessageStore, type AiConstructionMessage } from './messages.js';
import type { ScriptCandidate } from './deterministic-capability.js';
import { buildModelingBriefQuestions } from './interaction-options.js';
import { ensureMaterialHypotheses } from './material-hypotheses.js';
import { summarizeModelingBrief, type ModelingBrief } from './modeling-brief.js';
import { buildProcessDiagramFiles, processInterviewQuestions } from './process-interview.js';
import {
  AiConstructionTaskGraph,
  type AiConstructionTaskGraphSnapshot,
  type AiConstructionTaskSnapshot,
} from './task-graph.js';

export interface AiConstructionMaterialUpdate {
  aiConstruction: AiConstructionTaskGraphSnapshot;
  message: AiConstructionMessage;
}

export interface CompleteMaterialAnalysisInput {
  packageRevisionOid: string;
  messageKey: string;
  analysis: AnalysisResult;
}

function latestConfirmedPackageRevision(
  snapshot: AiConstructionTaskGraphSnapshot,
): string | undefined {
  const activeMilestones = new Set(snapshot.milestones
    .filter((milestone) => milestone.status === 'confirmed')
    .map((milestone) => milestone.id));
  let latest: AiConstructionTaskGraphSnapshot['confirmations'][number] | undefined;
  for (const confirmation of snapshot.confirmations) {
    if (!activeMilestones.has(confirmation.milestoneId)) continue;
    if (!latest || confirmation.graphRevision > latest.graphRevision) latest = confirmation;
  }
  return latest?.packageRevisionOid;
}

export interface MaterialAnalysisProgressInput {
  title: string;
  summary?: string;
  percent: number;
  elapsedSeconds: number;
}

export interface AiConstructionAdvanceResult {
  aiConstruction: AiConstructionTaskGraphSnapshot;
  message?: AiConstructionMessage;
  interaction?: AuthoringInteraction;
}

export interface CompleteModelingBriefInput {
  taskId: string;
  packageRevisionOid: string;
  messageKey: string;
  brief: ModelingBrief;
}

export interface ReviseModelingBriefInput {
  packageRevisionOid: string;
  brief: ModelingBrief;
}

export interface CompleteProcessInterviewInput {
  taskId: string;
  packageRevisionOid: string;
  messageKey: string;
  outcome: InterviewOutcome;
}

export interface CompleteProcessReviewInput {
  taskId: string;
  packageRevisionOid: string;
  messageKey: string;
}

export interface CompleteKnowledgeStructureInput {
  packageRevisionOid: string;
  messageKey: string;
  version: string;
  counts: {
    operations: number;
    objects: number;
    relations: number;
    rules: number;
    metrics: number;
  };
  validation: {
    errors: number;
    warnings: number;
    hints: number;
    findings?: Array<{ severity: 'error' | 'warning' | 'hint'; title: string; summary: string; target?: string }>;
  };
}

export interface StartKnowledgeStructureInput {
  messageKey?: string;
}

export interface StartKnowledgeStructureValidationInput {
  packageRevisionOid: string;
  version: string;
  messageKey: string;
}

export interface WaitForKnowledgeStructureDecisionInput {
  failure: GenerationGateFailure;
  interactionId: string;
}

export interface EvaluationDefect {
  id: string;
  title: string;
  summary: string;
  target?: string;
  suggestedAction?: string;
}

export interface RecordEvaluationDefectsInput {
  roundId: string;
  packageVersion: string;
  packageRevisionOid?: string;
  defects: EvaluationDefect[];
}

export interface StartKnowledgeRepairInput {
  inputRevisionOid: string;
  messageKey?: string;
}

export interface CompleteKnowledgeRepairInput {
  packageRevisionOid: string;
  messageKey: string;
  version: string;
  summary: string;
  validation: {
    errors: number;
    warnings: number;
    hints: number;
    findings?: Array<{
      severity: '错误' | '警告' | '提示';
      category: string;
      message: string;
      target?: string;
    }>;
  };
}

export interface WaitForKnowledgeRepairInput {
  messageKey: string;
  authority: 'approval_required' | 'expert_decision';
  finding: {
    category: string;
    message: string;
    target?: string;
  };
}

export interface StopKnowledgeRepairInput {
  messageKey: string;
  reason: string;
  attempts?: string[];
  retryable?: boolean;
}

export interface DeferKnowledgeErrorsInput {
  packageRevisionOid: string;
  errorCount: number;
  reason: string;
  actor: string;
}

/** 体检面板里点开一条校验发现时，助手要展示的内容。 */
export interface CheckFindingCardInput {
  packageRevisionOid: string;
  findingId: string;
  severity: string;
  category: string;
  target: string;
  message: string;
  suggestion?: string;
  /** 包内文件路径或知识资源语义目标，用来生成「打开这个文件」按钮 */
  filePath?: string;
  /** 最近一次需要重新呈现操作卡的对话序号；只参与幂等 key，不进入文案 */
  attentionKey?: string;
}

export interface CheckFindingIgnoreRequest {
  aiConstruction: AiConstructionTaskGraphSnapshot;
  interaction: AuthoringInteraction;
}

/** 忽略一条发现前，先向专家收集理由。错误只能整批暂缓，警告按 fingerprint 逐条接受。 */
export interface CheckFindingIgnoreInput extends CheckFindingCardInput {
  scope: 'errors' | 'warning';
  /** scope='warning' 时必须提供 */
  fingerprint?: string;
  /** scope='errors' 时表示会被一起暂缓的错误总数 */
  errorCount?: number;
}

export interface AiConstructionAdvanceContext {
  scriptCandidates?: ScriptCandidate[];
}

export interface CompleteScriptDecisionInput {
  taskId: string;
  packageRevisionOid: string;
  selectedCandidates: ScriptCandidate[];
  messageKey: string;
}

export interface CompleteScriptGenerationInput {
  packageRevisionOid: string;
  messageKey: string;
  scriptCount: number;
  validation: {
    errors: number;
    warnings: number;
    hints: number;
  };
}

export interface RetryScriptGenerationInput {
  packageRevisionOid: string;
  candidates: ScriptCandidate[];
}

export interface AiConstructionOrchestratorOptions {
  db?: DatabaseSync;
  graph?: AiConstructionTaskGraph;
  messages?: AiConstructionMessageStore;
  state?: AuthoringState;
}

function displayText(value: unknown, maxLength: number, fallback: string): string {
  const text = String(value ?? '').trim() || fallback;
  return text.length <= maxLength ? text : `${text.slice(0, maxLength - 1)}…`;
}

function displayTarget(value: unknown): string | undefined {
  const text = String(value ?? '').trim();
  if (!text) return undefined;
  return displayText(text, 300, 'package');
}

function actionCardKey(prefix: string, ...parts: Array<string | undefined>): string {
  const digest = createHash('sha256').update(parts.filter(Boolean).join('|')).digest('hex').slice(0, 28);
  return `${prefix}.${digest}`.slice(0, 64);
}

function isGoalFirstGraph(graph: AiConstructionTaskGraphSnapshot): boolean {
  const goal = graph.milestones.find((item) => item.id === 'goal_confirmed');
  const materials = graph.milestones.find((item) => item.id === 'materials_ready');
  return Boolean(goal && materials && goal.ordinal < materials.ordinal);
}

function sameModelingBrief(left: ModelingBrief | undefined, right: ModelingBrief): boolean {
  return Boolean(left
    && left.problem.trim() === right.problem.trim()
    && left.targetUsers.trim() === right.targetUsers.trim()
    && left.scenarios.trim() === right.scenarios.trim()
    && left.successCriteria.trim() === right.successCriteria.trim());
}

/**
 * Deterministic controller for user-visible AI construction transitions.
 * Models may propose content, but this module owns which task and actions become active.
 */
export class AiConstructionOrchestrator {
  private readonly graph: AiConstructionTaskGraph;
  private readonly messages: AiConstructionMessageStore;
  private readonly state: AuthoringState;
  private readonly db: DatabaseSync;

  constructor(options: AiConstructionOrchestratorOptions = {}) {
    this.db = options.db ?? getDb();
    this.graph = options.graph ?? new AiConstructionTaskGraph(this.db);
    this.messages = options.messages ?? new AiConstructionMessageStore(this.db);
    this.state = options.state ?? (options.db ? new AuthoringState(this.db) : getAuthoringState());
  }

  latestModelingBrief(projectId: string): ModelingBrief | null {
    const rows = this.db.prepare(`SELECT result_json FROM authoring_runs
      WHERE project_id = ? AND status = 'completed' AND result_json IS NOT NULL
      ORDER BY COALESCE(finished_at, updated_at) DESC, rowid DESC LIMIT 50`)
      .all(projectId) as Array<{ result_json: string }>;
    for (const row of rows) {
      try {
        const value = JSON.parse(row.result_json) as Record<string, unknown>;
        if (value.purpose !== 'modeling_brief') continue;
        const problem = typeof value.problem === 'string' ? value.problem.trim() : '';
        const targetUsers = typeof value.targetUsers === 'string' ? value.targetUsers.trim() : '';
        const scenarios = typeof value.scenarios === 'string' ? value.scenarios.trim() : '';
        const successCriteria = typeof value.successCriteria === 'string' ? value.successCriteria.trim() : '';
        if (problem && targetUsers && scenarios && successCriteria) {
          return { problem, targetUsers, scenarios, successCriteria };
        }
      } catch { /* 忽略旧格式或损坏的非目标任务结果 */ }
    }
    return null;
  }

  latestConfirmedModelingBrief(projectId: string): ModelingBrief | null {
    const graph = this.graph.snapshot(projectId);
    const goal = graph?.milestones.find((item) => item.id === 'goal_confirmed');
    return goal?.status === 'confirmed' ? this.latestModelingBrief(projectId) : null;
  }

  latestMaterialAnalysis(projectId: string): AnalysisResult | null {
    const rows = this.db.prepare(`SELECT output_json FROM wf_nodes
      WHERE project_id = ? AND type = 'analyze' AND output_json IS NOT NULL
      ORDER BY ordinal DESC, rowid DESC LIMIT 20`).all(projectId) as Array<{ output_json: string }>;
    for (const row of rows) {
      try {
        const value = JSON.parse(row.output_json) as AnalysisResult;
        if (typeof value.domainOverview === 'string'
          && Array.isArray(value.candidateObjects)
          && Array.isArray(value.openQuestions)) {
          return ensureMaterialHypotheses(value);
        }
      } catch { /* 忽略旧格式或损坏的分析结果 */ }
    }
    return null;
  }

  materialsUploaded(
    projectId: string,
    uploaded: DocumentRecord,
    documents: DocumentRecord[],
  ): AiConstructionMaterialUpdate | null {
    const graph = this.graph.snapshot(projectId);
    if (!graph) return null;

    const task = graph.tasks.find((item) => (
      ['material_analysis', 'package_baseline_review'].includes(item.kind)
        && item.milestoneId === graph.progress.currentMilestoneId
        && !['stale', 'cancelled'].includes(item.status)
    )) ?? graph.tasks.find((item) => item.id === graph.progress.currentTaskId);
    const parsed = documents.filter((item) => item.parseStatus === 'parsed');
    const failed = documents.filter((item) => item.parseStatus === 'failed');
    const terminalCount = parsed.length + failed.length;
    const materialProgress = documents.length
      ? Math.round((terminalCount / documents.length) * 100)
      : 0;
    const analysisRunning = task?.status === 'running'
      && ['material_analysis', 'package_baseline_review'].includes(task.kind);
    const tableCount = parsed.reduce((total, item) => total + (item.tables?.length ?? 0), 0);
    const analysisAction = task?.kind === 'package_baseline_review'
      ? { id: 'review_package', label: '检查现有知识包', style: 'primary' as const }
      : { id: 'analyze_materials', label: '开始分析资料', style: 'primary' as const };
    const parseNote = uploaded.parseStatus === 'parsed'
      ? `《${uploaded.filename}》已解析成功。`
      : `《${uploaded.filename}》解析失败，请替换文件或补充其他资料。`;
    const inventory = [
      `当前共 ${documents.length} 份资料`,
      `${parsed.length} 份可读取`,
      ...(tableCount > 0 ? [`包含 ${tableCount} 张数据表`] : []),
      ...(failed.length > 0 ? [`${failed.length} 份解析失败`] : []),
    ].join('，');
    // 资料清单卡固定用同一 key 更新，避免每次上传都追加一张重复卡片
    const message = this.messages.upsert(projectId, {
      key: 'materials.inventory',
      role: 'assistant',
      taskId: task?.id,
      blocks: [
        {
          type: 'status',
          title: '已接收建模资料',
          summary: inventory,
          milestoneId: graph.progress.currentMilestoneId,
          taskId: task?.id,
          progress: materialProgress,
          eta: {
            minMinutes: materialProgress === 100 ? 0 : null,
            maxMinutes: materialProgress === 100 ? 0 : null,
            confidence: materialProgress === 100 ? 'high' : 'low',
          },
        },
        { type: 'text', text: analysisRunning
          ? `${parseNote}${inventory}。资料分析正在进行，您仍可以继续补充资料。`
          : `${parseNote}${inventory}。您可以继续补充，或让我开始分析当前资料。` },
        {
          type: 'next_actions',
          title: '下一步',
          actions: [
            { id: 'upload_materials', label: '继续上传资料', style: 'secondary' },
            ...(!analysisRunning ? [analysisAction] : []),
          ],
        },
      ],
    });
    return { aiConstruction: graph, message };
  }

  startMaterialAnalysis(
    projectId: string,
    messageKey = 'analysis.progress.current',
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const confirmedBrief = isGoalFirstGraph(current)
        ? this.latestConfirmedModelingBrief(projectId)
        : null;
      if (isGoalFirstGraph(current) && !confirmedBrief) {
        throw new Error('请先确认建模目标，再分析资料');
      }
      const currentMilestoneId = current.progress.currentMilestoneId;
      let task = current.tasks.find((item) => ['material_analysis', 'package_baseline_review'].includes(item.kind)
        && item.milestoneId === currentMilestoneId
        && !['completed', 'skipped', 'cancelled', 'stale'].includes(item.status))
        ?? current.tasks.find((item) => ['material_analysis', 'package_baseline_review'].includes(item.kind)
          && item.milestoneId === currentMilestoneId
          && !['skipped', 'cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('当前任务图没有可执行的资料分析任务');
      const milestone = current.milestones.find((item) => item.id === task!.milestoneId);
      if (task.status === 'completed') {
        if (milestone?.status !== 'active') throw new Error('资料范围已确认，不能直接覆盖历史分析');
        current = this.graph.apply(projectId, {
          type: 'reopen_task', expectedRevision: current.revision, taskId: task.id,
        });
        task = current.tasks.find((item) => item.id === task!.id)!;
      }
      if (task.status !== 'running') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const activeTask = current.tasks.find((item) => item.id === task.id)!;
      const message = this.messages.upsert(projectId, {
        key: messageKey, role: 'assistant', taskId: activeTask.id,
        blocks: [
          {
            type: 'status', title: '开始分析建模资料',
            summary: confirmedBrief
              ? `正在围绕已确认目标“${displayText(confirmedBrief.problem, 200, '建模目标')}”读取资料`
              : '正在读取资料，并准备提取候选业务目标和核心流程',
            milestoneId: activeTask.milestoneId, taskId: activeTask.id, progress: 3,
            eta: { minMinutes: 1, maxMinutes: 5, confidence: 'low' },
          },
          {
            type: 'text',
            text: confirmedBrief
              ? `本轮分析不会重新发明建模目标，只会识别能支撑目标的证据、核心流程和知识缺口。\n${summarizeModelingBrief(confirmedBrief)}`
              : '分析期间我会持续汇报当前阶段。完成后会给出少量候选业务目标和候选流程，供后续直接选择和补充。',
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  reportMaterialAnalysisProgress(
    projectId: string,
    input: MaterialAnalysisProgressInput,
    messageKey = 'analysis.progress.current',
  ): AiConstructionMaterialUpdate {
    const current = this.graph.snapshot(projectId);
    if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
    const task = current.tasks.find((item) => ['material_analysis', 'package_baseline_review'].includes(item.kind)
      && item.status === 'running');
    if (!task) throw new Error('当前没有正在执行的资料分析任务');
    const blocks = buildMaterialAnalysisProgressBlocks({
      phase: 'manual', title: input.title, summary: input.summary, percent: input.percent,
    }, Math.max(1, input.elapsedSeconds) * 1_000);
    const status = blocks[0];
    if (status.type === 'status') {
      status.milestoneId = task.milestoneId;
      status.taskId = task.id;
    }
    if (input.percent < 100 && blocks[1]?.type === 'text') {
      blocks[1].text = `${blocks[1].text} 当前正在${input.title.trim().replace(/^正在/u, '') || '分析资料'}，并会同步整理候选业务目标和流程。`;
    }
    const message = this.messages.upsert(projectId, {
      key: messageKey, role: 'assistant', taskId: task.id, progressOnly: true,
      blocks,
    });
    return { aiConstruction: current, message };
  }

  failMaterialAnalysis(
    projectId: string,
    reason: string,
    messageKey = 'analysis.progress.current',
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => ['material_analysis', 'package_baseline_review'].includes(item.kind)
        && item.status === 'running');
      if (!task) throw new Error('当前没有正在执行的资料分析任务');
      current = this.graph.apply(projectId, {
        type: 'fail_task', expectedRevision: current.revision, taskId: task.id, reason,
      });
      const message = this.messages.upsert(projectId, {
        key: messageKey, role: 'assistant', taskId: task.id,
        blocks: buildMaterialAnalysisFailureBlocks({
          reason,
          milestoneId: task.milestoneId,
          taskId: task.id,
          progress: current.progress.percent,
        }),
      });
      return { aiConstruction: current, message };
    });
  }

  completeMaterialAnalysis(
    projectId: string,
    input: CompleteMaterialAnalysisInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const existing = this.messages.findByKey(projectId, input.messageKey);
      if (existing) {
        const current = this.graph.snapshot(projectId);
        if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
        return { aiConstruction: current, message: existing };
      }

      const before = this.graph.snapshot(projectId);
      if (!before) throw new Error('项目尚未初始化 AI 辅助构建');
      if (isGoalFirstGraph(before)) {
        const confirmedBrief = this.latestConfirmedModelingBrief(projectId);
        if (!confirmedBrief || !sameModelingBrief(input.analysis.modelingBrief, confirmedBrief)) {
          throw new Error('资料分析结果未绑定当前已确认建模目标');
        }
      }
      const task = before.tasks.find((item) => (
        (item.kind === 'material_analysis' || item.kind === 'package_baseline_review')
        && item.milestoneId === before.progress.currentMilestoneId
        && item.status === 'running'
      )) ?? before.tasks.find((item) => (
        (item.kind === 'material_analysis' || item.kind === 'package_baseline_review')
        && item.milestoneId === before.progress.currentMilestoneId
        && !['stale', 'cancelled'].includes(item.status)
      ));
      if (!task) throw new Error('当前任务图没有资料分析任务');
      const impactReanalysis = task.milestoneId !== 'materials_ready';
      const after = task.status === 'completed'
        ? before
        : this.graph.apply(projectId, {
          type: 'complete_task',
          expectedRevision: before.revision,
          taskId: task.id,
          outputRevisionOid: input.packageRevisionOid,
        });
      const candidateCount = input.analysis.candidateObjects.length;
      const goalCount = input.analysis.candidateGoals?.length ?? 0;
      const processCount = input.analysis.candidateProcesses?.length ?? 0;
      const questionCount = input.analysis.openQuestions.length;
      const evidenceItems = (input.analysis.materialAssessments ?? []).slice(0, 6).map((item) => ({ source: item.source, summary: item.rationale }));
      const message = this.messages.ensure(projectId, {
        key: input.messageKey,
        role: 'assistant',
        taskId: task.id,
        blocks: [
          {
            type: 'status',
            title: impactReanalysis ? '资料变更影响复核已完成' : '资料分析已完成',
            summary: impactReanalysis ? '已更新受影响知识的分析依据' : '资料范围和分析结果等待您确认',
            milestoneId: task.milestoneId,
            taskId: task.id,
            progress: after.progress.percent,
            eta: {
              minMinutes: after.progress.eta.minMinutes,
              maxMinutes: after.progress.eta.maxMinutes,
              confidence: after.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: impactReanalysis
              ? `${input.analysis.domainOverview}\n\n已根据新资料修订重新整理分析依据。接下来只重新生成受影响知识，未受影响的目标和流程确认继续保留。`
              : `${input.analysis.domainOverview}\n\n已整理 ${goalCount} 个候选业务目标、${processCount} 个候选核心流程、${candidateCount} 个候选对象，以及 ${questionCount} 个待确认问题。确认资料范围后，我会优先让您从这些候选中选择，仅在没有合适选项时再补充输入。`,
          },
          ...(evidenceItems.length ? [{ type: 'evidence' as const, title: '资料分析依据', items: evidenceItems }] : []),
          {
            type: 'next_actions',
            title: '请确认下一步',
            actions: impactReanalysis
              ? [
                { id: 'generate_knowledge_structure', label: '重建受影响知识', style: 'primary' },
                { id: 'review_materials', label: '查看影响分析', style: 'secondary' },
              ]
              : [
                { id: 'confirm_materials', label: '确认资料范围', style: 'primary' },
                { id: 'upload_materials', label: '继续补充资料', style: 'secondary' },
              ],
          },
        ],
      });
      return { aiConstruction: after, message };
    });
  }

  completeModelingBrief(
    projectId: string,
    input: CompleteModelingBriefInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const existing = this.messages.findByKey(projectId, input.messageKey);
      const before = this.graph.snapshot(projectId);
      if (!before) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = before.tasks.find((item) => item.id === input.taskId);
      if (!task || task.kind !== 'modeling_brief') throw new Error('建模目标任务不存在');
      const after = task.status === 'completed'
        ? before
        : this.graph.apply(projectId, {
          type: 'complete_task',
          expectedRevision: before.revision,
          taskId: task.id,
          outputRevisionOid: input.packageRevisionOid,
        });
      const message = existing ?? this.messages.ensure(projectId, {
        key: input.messageKey,
        role: 'assistant',
        taskId: task.id,
        blocks: [
          {
            type: 'status',
            title: '建模目标说明已整理',
            summary: '请确认目标说明，确认后将围绕目标上传并分析资料',
            milestoneId: 'goal_confirmed',
            taskId: task.id,
            progress: after.progress.percent,
            eta: {
              minMinutes: after.progress.eta.minMinutes,
              maxMinutes: after.progress.eta.maxMinutes,
              confidence: after.progress.eta.confidence,
            },
          },
          { type: 'text', text: summarizeModelingBrief(input.brief) },
          {
            type: 'next_actions',
            title: '请确认建模目标',
            actions: [
              { id: 'confirm_goal', label: '确认建模目标', style: 'primary' },
              { id: 'revise_goal', label: '修改目标说明', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: after, message };
    });
  }

  reviseModelingBrief(
    projectId: string,
    input: ReviseModelingBriefInput,
  ): AiConstructionAdvanceResult {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      let task = current.tasks.find((item) => item.kind === 'modeling_brief'
        && item.milestoneId === 'goal_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('建模目标任务不存在');
      const existing = this.state.snapshot(projectId).pendingInteractions.find(
        (item) => item.type === 'question_batch' && item.payload.purpose === 'modeling_brief',
      );
      if (task.status === 'completed') {
        const taskId = task.id;
        const goalMilestone = current.milestones.find((item) => item.id === 'goal_confirmed');
        if (goalMilestone?.status !== 'active') throw new Error('已确认建模目标不能在当前阶段修改');
        current = this.graph.apply(projectId, {
          type: 'reopen_task', expectedRevision: current.revision, taskId,
        });
        const reopenedTask = current.tasks.find((item) => item.id === taskId);
        if (!reopenedTask) throw new Error('建模目标任务状态刷新失败');
        task = reopenedTask;
      }
      if (existing) return { aiConstruction: current, interaction: existing };
      if (task.status !== 'waiting_user') {
        throw new Error('当前建模目标不能重新填写');
      }
      const started = this.state.apply({
        kind: 'start_run',
        projectId,
        intent: '修改建模目标',
        baseRevision: input.packageRevisionOid,
        state: { purpose: 'modeling_brief', taskId: task.id, revision: true },
      });
      if (started.kind !== 'run') throw new Error('建模目标修改任务创建失败');
      const opened = this.state.apply({
        kind: 'open_interaction',
        projectId,
        runId: started.run.id,
        type: 'question_batch',
        baseRevision: input.packageRevisionOid,
        payload: {
          purpose: 'modeling_brief',
          taskId: task.id,
          title: '修改知识包的建模目标',
          description: '优先从资料分析给出的假设中选择；没有合适选项时再填写“其他”。修改后会重新生成目标摘要。',
          questions: buildModelingBriefQuestions(this.latestMaterialAnalysis(projectId), input.brief),
        },
      });
      if (opened.kind !== 'interaction') throw new Error('建模目标修改问题创建失败');
      const message = this.messages.append(projectId, {
        role: 'assistant',
        taskId: task.id,
        blocks: [
          {
            type: 'status', title: '建模目标退回修改',
            summary: '请调整目标说明并重新提交', milestoneId: 'goal_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: '我已把上次答案设为默认选项。您只需调整不准确的部分，没有合适选项时再补充。' },
        ],
      });
      return { aiConstruction: current, message, interaction: opened.interaction };
    });
  }

  completeProcessInterview(
    projectId: string,
    input: CompleteProcessInterviewInput,
  ): AiConstructionAdvanceResult {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const interviewTask = current.tasks.find((item) => item.id === input.taskId);
      if (!interviewTask || interviewTask.kind !== 'process_interview') {
        throw new Error('业务流程访谈任务不存在');
      }
      if (interviewTask.status !== 'completed') {
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: interviewTask.id, outputRevisionOid: input.packageRevisionOid,
        });
      }
      let diagramTask = current.tasks.find((item) => item.kind === 'process_diagram'
        && item.milestoneId === 'process_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!diagramTask) throw new Error('流程图任务不存在');
      if (diagramTask.status !== 'completed') {
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: diagramTask.id, outputRevisionOid: input.packageRevisionOid,
        });
        diagramTask = current.tasks.find((item) => item.id === diagramTask!.id)!;
      }
      let reviewTask = current.tasks.find((item) => item.kind === 'process_review'
        && item.milestoneId === 'process_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!reviewTask) throw new Error('流程图确认任务不存在');
      if (reviewTask.status !== 'waiting_user') {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: reviewTask.id,
        });
        reviewTask = current.tasks.find((item) => item.id === reviewTask!.id)!;
      }

      const output = buildFlowchart(undefined, undefined, input.outcome.processes);
      if (!output.diagrams.some((diagram) => diagram.nodes.length)) {
        throw new Error('流程访谈没有形成可绘制的业务流程');
      }
      const candidateFiles = buildProcessDiagramFiles(input.outcome);
      const existing = this.state.snapshot(projectId).pendingInteractions.find(
        (item) => item.type === 'flowchart_review' && item.payload.purpose === 'process_review',
      );
      let interaction = existing;
      if (!interaction) {
        const started = this.state.apply({
          kind: 'start_run', projectId, intent: '确认业务流程图',
          baseRevision: input.packageRevisionOid,
          state: { purpose: 'process_review', taskId: reviewTask.id },
        });
        if (started.kind !== 'run') throw new Error('流程图确认任务创建失败');
        const opened = this.state.apply({
          kind: 'open_interaction', projectId, runId: started.run.id,
          type: 'flowchart_review', baseRevision: input.packageRevisionOid,
          payload: {
            purpose: 'process_review', taskId: reviewTask.id,
            title: '确认核心业务流程图',
            description: '请核对流程触发、步骤、判断分支、异常、输出和交接。通过后才会写入知识包。',
            diagrams: output.diagrams as unknown as JsonObject[],
            candidateFiles,
            outcome: input.outcome as unknown as JsonObject,
          },
        });
        if (opened.kind !== 'interaction') throw new Error('流程图确认项创建失败');
        interaction = opened.interaction;
      }
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: reviewTask.id,
        blocks: [
          {
            type: 'status', title: '流程图草稿已生成',
            summary: `已生成 ${output.diagrams.length} 个核心业务流程，请人工确认`,
            milestoneId: 'process_confirmed', taskId: reviewTask.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: input.outcome.summary },
        ],
      });
      return { aiConstruction: current, message, interaction };
    });
  }

  completeProcessReview(
    projectId: string,
    input: CompleteProcessReviewInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const before = this.graph.snapshot(projectId);
      if (!before) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = before.tasks.find((item) => item.id === input.taskId);
      if (!task || task.kind !== 'process_review') throw new Error('流程图确认任务不存在');
      const after = task.status === 'completed'
        ? before
        : this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: before.revision,
          taskId: task.id, outputRevisionOid: input.packageRevisionOid,
        });
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '业务流程图已确认',
            summary: '流程图已经写入知识包，可以确认业务流程阶段',
            milestoneId: 'process_confirmed', taskId: task.id,
            progress: after.progress.percent,
            eta: {
              minMinutes: after.progress.eta.minMinutes,
              maxMinutes: after.progress.eta.maxMinutes,
              confidence: after.progress.eta.confidence,
            },
          },
          { type: 'text', text: '后续将以已确认流程为主线提取业务操作、对象关系、规则和确定性能力。' },
          {
            type: 'next_actions', title: '请确认下一步',
            actions: [{ id: 'confirm_process', label: '确认业务流程', style: 'primary' }],
          },
        ],
      });
      return { aiConstruction: after, message };
    });
  }

  completeKnowledgeStructure(
    projectId: string,
    input: CompleteKnowledgeStructureInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const existing = this.messages.findByKey(projectId, input.messageKey);
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const requiredKinds = ['operation_extraction', 'knowledge_extraction'] as const;
      for (const kind of requiredKinds) {
        let task = current.tasks.find((item) => item.kind === kind
          && item.milestoneId === 'knowledge_confirmed'
          && !['cancelled', 'stale'].includes(item.status));
        if (!task) throw new Error(`${kind === 'operation_extraction' ? '业务操作' : '知识'}提取任务不存在`);
        if (task.status !== 'completed') {
          if (task.status !== 'running') {
            current = this.graph.apply(projectId, {
              type: 'start_task', expectedRevision: current.revision, taskId: task.id,
            });
            task = current.tasks.find((item) => item.id === task!.id)!;
          }
          current = this.graph.apply(projectId, {
            type: 'complete_task', expectedRevision: current.revision,
            taskId: task.id, outputRevisionOid: input.packageRevisionOid,
          });
        }
      }
      const validationTask = current.tasks.find((item) => item.kind === 'package_validation'
        && item.milestoneId === 'knowledge_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!validationTask) throw new Error('知识包校验任务不存在');
      if (validationTask.status !== 'completed') {
        if (validationTask.status !== 'running') {
          current = this.graph.apply(projectId, {
            type: 'start_task', expectedRevision: current.revision, taskId: validationTask.id,
          });
        }
        current = this.graph.apply(projectId, input.validation.errors === 0 ? {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: validationTask.id, outputRevisionOid: input.packageRevisionOid,
        } : {
          type: 'fail_task', expectedRevision: current.revision, taskId: validationTask.id,
        });
      }
      if (existing) return { aiConstruction: current, message: existing };

      const validationSummary = `错误 ${input.validation.errors} · 警告 ${input.validation.warnings} · 提示 ${input.validation.hints}`;
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: validationTask.id,
        blocks: [
          {
            type: 'status',
            title: input.validation.errors === 0 ? '知识结构草案已生成' : '知识结构草案校验未通过',
            summary: `v${input.version} · ${validationSummary}`,
            milestoneId: 'knowledge_confirmed', taskId: validationTask.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: [
              `已生成 operation ${input.counts.operations} 个、对象 ${input.counts.objects} 个、关系 ${input.counts.relations} 条、规则 ${input.counts.rules} 条、指标 ${input.counts.metrics} 个。`,
              `知识包校验结果：${validationSummary}。`,
              input.validation.errors === 0
                ? '请在中间工作区逐文件审阅；确认后再进入确定性脚本判断。'
                : '当前存在必须修复的错误，请重新生成并校验后再确认知识结构。',
            ].join('\n'),
          },
          { type: 'validation', title: '知识结构阶段校验', scope: 'milestone',
            counts: { errors: input.validation.errors, warnings: input.validation.warnings, hints: input.validation.hints },
            ...(input.validation.findings?.length ? {
              findings: input.validation.findings.slice(0, 8).map((finding) => ({
                severity: finding.severity,
                title: displayText(finding.title, 120, '校验发现'),
                summary: displayText(finding.summary, 1_000, '详细说明见完整校验记录'),
                ...(displayTarget(finding.target) ? { target: displayTarget(finding.target) } : {}),
              })),
            } : {}) },
          {
            type: 'next_actions', title: '选择下一步',
            actions: input.validation.errors === 0
              ? [
                { id: 'confirm_knowledge', label: '确认知识结构', style: 'primary' },
                { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
              ]
              : [
                // 修复能力一直都在，早先这里只给「重新生成」，让人误以为出错就只能推倒重来
                {
                  id: 'repair_knowledge_structure', label: '逐项修复校验错误', style: 'primary',
                  description: '保留现有知识结构，按校验发现逐条修复；需要业务判断的会来问你',
                },
                {
                  id: 'generate_knowledge_structure', label: '重新生成知识结构', style: 'secondary',
                  description: '丢弃当前知识结构，基于流程图和资料整体重新生成',
                },
                {
                  id: 'defer_validation_errors', label: '暂缓错误并继续验证', style: 'secondary',
                  description: '不修改内容，登记豁免理由后让这些错误暂不阻断建模完成',
                },
                { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
              ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  startKnowledgeStructure(
    projectId: string,
    input: StartKnowledgeStructureInput = {},
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.milestoneId === 'knowledge_confirmed'
        && ['operation_extraction', 'knowledge_extraction', 'package_validation'].includes(item.kind)
        && !['completed', 'skipped', 'cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('知识结构任务已经完成或尚未规划');
      if (task.status !== 'running') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const activeTask = current.tasks.find((item) => item.id === task.id)!;
      const message = input.messageKey
        ? this.messages.ensure(projectId, {
          key: input.messageKey, role: 'assistant', taskId: activeTask.id,
          blocks: this.knowledgeRunningBlocks(current, activeTask.id, activeTask.title),
        })
        : this.messages.append(projectId, {
          role: 'assistant', taskId: activeTask.id,
          blocks: this.knowledgeRunningBlocks(current, activeTask.id, activeTask.title),
        });
      return { aiConstruction: current, message };
    });
  }

  startKnowledgeStructureValidation(
    projectId: string,
    input: StartKnowledgeStructureValidationInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');

      for (const kind of ['operation_extraction', 'knowledge_extraction'] as const) {
        let task = current.tasks.find((item) => item.kind === kind
          && item.milestoneId === 'knowledge_confirmed'
          && !['cancelled', 'stale'].includes(item.status));
        if (!task) throw new Error(`${kind === 'operation_extraction' ? '业务操作' : '知识'}提取任务不存在`);
        if (task.status === 'completed') continue;
        if (task.status !== 'running') {
          current = this.graph.apply(projectId, {
            type: 'start_task', expectedRevision: current.revision, taskId: task.id,
          });
          task = current.tasks.find((item) => item.id === task!.id)!;
        }
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: task.id, outputRevisionOid: input.packageRevisionOid,
        });
      }

      let validationTask = current.tasks.find((item) => item.kind === 'package_validation'
        && item.milestoneId === 'knowledge_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!validationTask) throw new Error('知识包校验任务不存在');
      if (validationTask.status !== 'running' && validationTask.status !== 'completed') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: validationTask.id,
        });
        validationTask = current.tasks.find((item) => item.id === validationTask!.id)!;
      }

      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: validationTask.id,
        blocks: [
          {
            type: 'status', title: '正在校验知识结构',
            summary: `v${input.version} 草案已写入，正在检查结构、映射和语义完整性`,
            milestoneId: 'knowledge_confirmed', taskId: validationTask.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: '知识文件已经生成。当前正在执行确定性检查和语义体检；复杂知识包的语义体检可能持续一到数分钟，完成后会给出可审阅的校验结果。',
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  waitForKnowledgeStructureDecision(
    projectId: string,
    input: WaitForKnowledgeStructureDecisionInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.milestoneId === 'knowledge_confirmed'
        && item.status === 'running');
      if (!task) throw new Error('当前没有正在执行的知识结构任务');
      current = this.graph.apply(projectId, {
        type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
      });
      const message = this.messages.ensure(projectId, {
        key: `knowledge.generation.decision.${input.interactionId}`,
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '知识生成等待专家确认',
            summary: input.failure.title,
            milestoneId: 'knowledge_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: `模型已完成两次自动补交，仍有 ${input.failure.issues.length} 个问题。系统不会直接判定整轮失败，请由专家选择重新生成或跳过该批继续。`,
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  failKnowledgeStructure(projectId: string, reason: string): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.milestoneId === 'knowledge_confirmed'
        && item.status === 'running');
      if (!task) throw new Error('当前没有正在执行的知识结构任务');
      current = this.graph.apply(projectId, {
        type: 'fail_task', expectedRevision: current.revision, taskId: task.id, reason,
      });
      const message = this.messages.append(projectId, {
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '知识结构生成未完成',
            summary: `任务“${task.title}”已保留为可重试状态`,
            milestoneId: 'knowledge_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: reason.trim().slice(0, 3000) || '生成过程意外中断，请重新执行当前任务。' },
          {
            type: 'next_actions', title: '可以这样继续', actions: [
              { id: 'generate_knowledge_structure', label: '重试知识结构生成', style: 'primary' },
              { id: 'review_process_files', label: '先检查流程文件', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  /** 未结束的受控修复任务全局只会有一条；跟随它所在的里程碑，不再假定是 knowledge_confirmed */
  private findRepairTask(
    graph: AiConstructionTaskGraphSnapshot,
    statuses?: ReadonlySet<AiConstructionTaskSnapshot['status']>,
  ): AiConstructionTaskSnapshot | undefined {
    return [...graph.tasks].reverse().find((item) => item.kind === 'package_repair'
      && (statuses
        ? statuses.has(item.status)
        : !['completed', 'skipped', 'cancelled', 'stale'].includes(item.status)));
  }

  /**
   * 找到可以承载受控修复的活动里程碑。
   * 任务图要求里程碑 active 才能规划/推进任务，而知识结构确认之后知识包仍会出阻断错误
   * （人工改包、确定性阶段校验）。此时重做最近一次已完成的校验任务把该阶段重新打开——
   * 校验之后发现的错误意味着当时的「已确认」不再成立。
   */
  private ensureRepairMilestone(
    projectId: string,
    initial: AiConstructionTaskGraphSnapshot,
  ): { graph: AiConstructionTaskGraphSnapshot; milestoneId: string } {
    let current = initial;
    let milestoneId = current.milestones.find((item) => item.status === 'active')?.id;
    if (milestoneId) return { graph: current, milestoneId };

    const reworkTask = [...current.tasks].reverse().find((item) => (
      item.kind === 'package_validation' && item.status === 'completed'
    ));
    if (!reworkTask) throw new Error('当前没有可执行逐项修复的活动阶段');
    current = this.graph.adjust(projectId, {
      expectedRevision: current.revision,
      taskId: reworkTask.id,
      action: 'redo',
      actor: 'system-controlled-repair',
      reason: '知识包校验发现新的必须修复的错误，需要重新打开修复与校验阶段',
    }).graph;
    milestoneId = current.milestones.find((item) => item.status === 'active')?.id;
    if (!milestoneId) throw new Error('当前没有可执行逐项修复的活动阶段');
    return { graph: current, milestoneId };
  }

  startKnowledgeRepair(
    projectId: string,
    input: StartKnowledgeRepairInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const initial = this.graph.snapshot(projectId);
      if (!initial) throw new Error('项目尚未初始化 AI 辅助构建');
      let task = this.findRepairTask(initial);
      let current = initial;
      if (!task) {
        const repairMilestone = this.ensureRepairMilestone(projectId, initial);
        current = repairMilestone.graph;
        const milestoneId = repairMilestone.milestoneId;
        const knowledgeTask = current.tasks.find((item) => item.kind === 'knowledge_extraction'
          && item.milestoneId === 'knowledge_confirmed'
          && item.status === 'completed');
        current = this.graph.apply(projectId, {
          type: 'plan_task', expectedRevision: current.revision,
          task: {
            kind: 'package_repair', milestoneId,
            title: '逐项修复知识包校验问题',
            goal: '按各自的处理权限把问题修掉，然后重新跑一次检查。',
            required: false,
            reason: '知识包校验存在必须修复的错误；原校验任务继续承担阶段门禁',
            inputRevisionOid: input.inputRevisionOid,
            dependsOn: milestoneId === 'knowledge_confirmed' && knowledgeTask ? [knowledgeTask.id] : [],
          },
        });
        task = this.findRepairTask(current);
      }
      if (!task) throw new Error('知识包修复任务创建失败');
      if (task.status !== 'running') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const activeTask = current.tasks.find((item) => item.id === task!.id)!;
      const blocks = [
        {
          type: 'status' as const,
          title: '正在逐项修复知识包',
          summary: '先处理可唯一确定的结构问题，再按权限暂停在人工确认点',
          milestoneId: 'knowledge_confirmed', taskId: activeTask.id,
          progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        {
          type: 'text' as const,
          text: '安全结构问题会单独形成一次修改；知识语义修改先展示方案；阈值、分支、口径和冲突必须由专家明确回答。',
        },
      ];
      const message = input.messageKey
        ? this.messages.ensure(projectId, {
          key: input.messageKey, role: 'assistant', taskId: activeTask.id, blocks,
        })
        : this.messages.append(projectId, {
          role: 'assistant', taskId: activeTask.id, blocks,
        });
      return { aiConstruction: current, message };
    });
  }

  waitForKnowledgeRepair(
    projectId: string,
    input: WaitForKnowledgeRepairInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = this.findRepairTask(current, new Set(['running', 'waiting_user']));
      if (!task) throw new Error('当前没有正在执行的知识包修复任务');
      if (task.status === 'running') {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const title = input.authority === 'expert_decision'
        ? '知识包修复等待专家决策'
        : '知识包语义修复方案等待确认';
      const summary = input.authority === 'expert_decision'
        ? '系统不会代替专家选择业务口径、阈值或流程分支'
        : '确认方案前不会修改当前知识包';
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title, summary,
            milestoneId: 'knowledge_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: `${input.finding.category}${input.finding.target ? ` · ${input.finding.target}` : ''}\n${input.finding.message}`,
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  completeKnowledgeRepair(
    projectId: string,
    input: CompleteKnowledgeRepairInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      let repairTask = this.findRepairTask(current);
      if (!repairTask) throw new Error('知识包修复任务不存在');
      if (repairTask.status !== 'running') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: repairTask.id,
        });
        repairTask = current.tasks.find((item) => item.id === repairTask!.id)!;
      }
      current = this.graph.apply(projectId, {
        type: 'complete_task', expectedRevision: current.revision,
        taskId: repairTask.id, outputRevisionOid: input.packageRevisionOid,
      });

      // 校验任务跟随修复任务所在的里程碑：修复未必发生在知识结构阶段
      const repairMilestoneId = repairTask.milestoneId;
      const findValidationTask = (graph: AiConstructionTaskGraphSnapshot) => (
        [...graph.tasks].reverse().find((item) => item.kind === 'package_validation'
          && item.milestoneId === repairMilestoneId
          && !['cancelled', 'stale'].includes(item.status))
      );
      let validationTask = findValidationTask(current);
      if (!validationTask
        || (validationTask.status === 'completed'
          && validationTask.outputRevisionOid !== input.packageRevisionOid)) {
        current = this.graph.apply(projectId, {
          type: 'plan_task', expectedRevision: current.revision,
          task: {
            kind: 'package_validation', milestoneId: repairMilestoneId,
            title: '重新校验修复后的知识包',
            goal: '重新检查一遍知识包有没有缺内容、引用错、和数据对不上的地方。',
            required: true,
            reason: '知识包内容已经变化，旧校验结果不能继续作为阶段门禁',
            inputRevisionOid: input.packageRevisionOid,
            dependsOn: [repairTask.id],
          },
        });
        validationTask = findValidationTask(current);
        if (!validationTask) throw new Error('修复后知识包校验任务创建失败');
      }
      if (input.validation.errors === 0 && validationTask.status !== 'completed') {
        if (validationTask.status !== 'running') {
          current = this.graph.apply(projectId, {
            type: 'start_task', expectedRevision: current.revision, taskId: validationTask.id,
          });
        }
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: validationTask.id, outputRevisionOid: input.packageRevisionOid,
        });
      } else if (input.validation.errors > 0 && validationTask.status !== 'failed') {
        if (validationTask.status !== 'running') {
          current = this.graph.apply(projectId, {
            type: 'start_task', expectedRevision: current.revision, taskId: validationTask.id,
          });
        }
        current = this.graph.apply(projectId, {
          type: 'fail_task', expectedRevision: current.revision, taskId: validationTask.id,
        });
      }

      const validationSummary = `错误 ${input.validation.errors} · 警告 ${input.validation.warnings} · 提示 ${input.validation.hints}`;
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: repairTask.id,
        blocks: [
          {
            type: 'status',
            title: input.validation.errors === 0 ? '知识包修复已通过校验' : '本轮修复已完成，仍需继续处理',
            summary: `v${input.version} · ${validationSummary}`,
            milestoneId: repairMilestoneId, taskId: repairTask.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: input.summary },
          {
            type: 'validation', title: '修复后阶段校验', scope: 'milestone',
            counts: {
              errors: input.validation.errors,
              warnings: input.validation.warnings,
              hints: input.validation.hints,
            },
            ...(input.validation.findings?.length ? {
              findings: input.validation.findings.slice(0, 8).map((finding) => ({
                severity: finding.severity === '错误' ? 'error' as const
                  : finding.severity === '警告' ? 'warning' as const : 'hint' as const,
                title: displayText(finding.category, 120, '校验发现'),
                summary: displayText(finding.message, 1_000, '详细说明见完整校验记录'),
                ...(displayTarget(finding.target) ? { target: displayTarget(finding.target) } : {}),
              })),
            } : {}),
          },
          {
            type: 'next_actions', title: '选择下一步',
            actions: input.validation.errors === 0
              ? [
                { id: 'confirm_knowledge', label: '确认知识结构', style: 'primary' },
                { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
              ]
              : [
                {
                  id: 'repair_knowledge_structure', label: '继续逐项修复', style: 'primary',
                  description: '接着处理剩余的校验错误',
                },
                {
                  id: 'defer_validation_errors', label: '暂缓错误并继续验证', style: 'secondary',
                  description: '不修改内容，登记豁免理由后让这些错误暂不阻断建模完成',
                },
                { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
              ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  /**
   * 把评测诊断出的知识包缺陷收进建模流程：登记一条 evaluation_repair 任务并在助手对话里给出下一步。
   *
   * 用独立 kind 而不是 package_repair：后者由受控修复处理知识包**校验**发现、校验通过即完成；
   * 评测缺陷来自**失败用例**，只有优化更新能从用例反推资源改动（且带防过拟合校验），
   * 完成判据是重新评测通过——校验通过不代表评测缺陷已修。
   *
   * 只登记不执行：由专家决定何时进入优化更新，避免评测结果直接改写知识包。
   */
  recordEvaluationDefects(
    projectId: string,
    input: RecordEvaluationDefectsInput,
  ): AiConstructionAdvanceResult {
    if (!input.defects.length) throw new Error('没有需要回流建模的评测缺陷');
    return this.transaction(() => {
      const initial = this.graph.snapshot(projectId);
      if (!initial) throw new Error('项目尚未初始化 AI 辅助构建');

      const defectIds = input.defects.map((defect) => defect.id).sort();
      const messageKey = `evaluation.defects.${createHash('sha256')
        .update([input.roundId, ...defectIds].join('|')).digest('hex').slice(0, 24)}`;
      const existing = this.messages.findByKey(projectId, messageKey);
      if (existing) return { aiConstruction: initial, message: existing };

      const findEvaluationRepair = (graph: AiConstructionTaskGraphSnapshot) => (
        [...graph.tasks].reverse().find((item) => item.kind === 'evaluation_repair'
          && !['completed', 'skipped', 'cancelled', 'stale'].includes(item.status))
      );
      let current = initial;
      let task = findEvaluationRepair(current);
      let milestoneId = task?.milestoneId;
      if (!task) {
        const repairMilestone = this.ensureRepairMilestone(projectId, current);
        current = repairMilestone.graph;
        milestoneId = repairMilestone.milestoneId;
        current = this.graph.apply(projectId, {
          type: 'plan_task', expectedRevision: current.revision,
          task: {
            kind: 'evaluation_repair', milestoneId,
            title: '修缮评测发现的知识包缺陷',
            goal: '按评测失败的原因改掉对应的内容，改完重新评测确认已经修好。',
            required: false,
            reason: `来自评测轮次 ${input.roundId} 的 ${input.defects.length} 项失败原因`,
            ...(input.packageRevisionOid ? { inputRevisionOid: input.packageRevisionOid } : {}),
            dependsOn: [],
          },
        });
        task = findEvaluationRepair(current);
      }
      if (!task || !milestoneId) throw new Error('评测缺陷修缮任务创建失败');
      if (['pending', 'ready', 'running'].includes(task.status)) {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
        });
      }

      const defectLines = input.defects.map((defect, index) => {
        const target = displayTarget(defect.target);
        return [
          `${index + 1}. ${displayText(defect.title, 120, '未命名缺陷')}`,
          target ? `（${target}）` : '',
          `\n   现象：${displayText(defect.summary, 300, '评测失败，未给出更多说明')}`,
          defect.suggestedAction ? `\n   建议：${displayText(defect.suggestedAction, 300, '')}` : '',
        ].join('');
      }).join('\n');

      const message = this.messages.ensure(projectId, {
        key: messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status',
            title: `评测发现 ${input.defects.length} 处知识包缺陷`,
            summary: '已登记为建模修缮任务，请在优化更新中处理',
            milestoneId, taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: `这些缺陷来自评测轮次 ${input.roundId}（知识包 v${input.packageVersion}）：\n${defectLines}`
              + '\n\n请在「优化更新」中生成修改建议：它会依据失败用例反推资源改动，并强制通过防过拟合校验，'
              + '避免把单条用例的答案写死成通用规则。',
          },
          {
            type: 'next_actions', title: '选择下一步',
            actions: [
              { id: 'open_optimization', label: '进入优化更新', style: 'primary' },
              { id: 'review_knowledge_files', label: '先查看知识文件', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  deferKnowledgeErrors(
    projectId: string,
    input: DeferKnowledgeErrorsInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const reason = input.reason.trim();
      if (!reason || input.errorCount < 1) throw new Error('请说明需要暂缓的必须修复的错误和原因');
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      let repairTask = this.findRepairTask(current);
      if (repairTask?.status === 'running') {
        current = this.graph.apply(projectId, {
          type: 'fail_task', expectedRevision: current.revision, taskId: repairTask.id, reason,
        });
        repairTask = current.tasks.find((item) => item.id === repairTask!.id);
      }
      if (repairTask
        && ['pending', 'ready', 'failed', 'waiting_user', 'waiting_confirmation'].includes(repairTask.status)) {
        current = this.graph.adjust(projectId, {
          expectedRevision: current.revision,
          taskId: repairTask.id,
          action: 'skip',
          actor: input.actor,
          reason,
        }).graph;
      }
      const validationTask = [...current.tasks].reverse().find((item) => item.kind === 'package_validation'
        && item.milestoneId === 'knowledge_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!validationTask) throw new Error('知识包校验任务不存在');
      if (validationTask.status !== 'completed') {
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: validationTask.id, outputRevisionOid: input.packageRevisionOid,
        });
      }
      for (const run of this.state.snapshot(projectId).activeRuns) {
        if (run.state?.purpose !== 'controlled_repair') continue;
        this.state.apply({
          kind: 'update_run', projectId, runId: run.id, status: 'cancelled',
          result: {
            purpose: 'controlled_repair', deferred: true,
            packageRevisionOid: input.packageRevisionOid, errorCount: input.errorCount,
          },
        });
      }
      const message = this.messages.ensure(projectId, {
        key: `knowledge.errors.deferred.${input.packageRevisionOid.slice(0, 24)}`,
        role: 'assistant', taskId: validationTask.id,
        blocks: [
          {
            type: 'status', title: '已暂缓知识包错误修复',
            summary: `保留 ${input.errorCount} 个必须修复的错误，先进入验证闭环`,
            milestoneId: 'knowledge_confirmed', taskId: validationTask.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: `${reason}\n这些错误仅对当前修订暂缓；验证和改进可以继续，正式发布前仍必须消除必须修复的错误。`,
          },
          {
            type: 'validation', title: '暂缓进入验证的已知阻断项', scope: 'milestone',
            counts: { errors: input.errorCount, warnings: 0, hints: 0 },
          },
          {
            type: 'next_actions', title: '选择下一步', actions: [
              { id: 'confirm_knowledge', label: '确认知识结构', style: 'primary' },
              { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  /**
   * 「去修改」：把一条校验发现讲清楚，并给出可执行的两条路。
   *
   * 原先这个按钮只是往输入框预填一句话，用户还得自己按发送；现在直接落一条助手消息，
   * 和其他受阻路径用同一套卡片渲染。
   */
  reviewCheckFinding(
    projectId: string,
    input: CheckFindingCardInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const taskId = current.progress.currentTaskId ?? undefined;
      const message = this.messages.ensure(projectId, {
        key: actionCardKey(
          'check.finding.review',
          input.packageRevisionOid,
          input.findingId,
          input.attentionKey,
        ),
        role: 'assistant',
        taskId,
        blocks: [
          {
            type: 'status',
            title: `处理校验发现：${input.category}`,
            summary: `${input.severity} · ${input.target}`,
            milestoneId: current.progress.currentMilestoneId ?? undefined,
            taskId,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: [
              input.message,
              input.suggestion ? `建议这样改：${input.suggestion}` : '',
            ].filter(Boolean).join('\n'),
          },
          {
            type: 'next_actions',
            title: '选择一种方式处理',
            actions: [
              {
                id: 'repair_knowledge_structure',
                label: '让助手逐项修复',
                description: '按校验发现逐条修复，需要业务判断时会来问你',
                style: 'primary' as const,
              },
              input.filePath
                ? {
                  id: 'open_package_file' as const,
                  label: '我自己去改这个文件',
                  description: `打开 ${input.filePath}`,
                  path: input.filePath,
                  style: 'secondary' as const,
                }
                : {
                  id: 'review_knowledge_files' as const,
                  label: '我自己去改知识文件',
                  style: 'secondary' as const,
                },
              {
                id: 'run_package_check',
                label: '改好并发布后，重新校验',
                description: '保存并发布人工修改后重新检查，并继续处理下一条提醒',
                style: 'secondary' as const,
              },
              ...(input.severity === '警告' ? [{
                id: 'ignore_check_finding',
                label: '说明原因，先不改',
                description: '登记这条提醒的影响和暂缓理由，然后继续处理下一条',
                findingId: input.findingId,
                style: 'secondary' as const,
              }] : []),
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  /**
   * 当前修订的校验警告已经全部处理后，重新给出一次最终确认出口。
   *
   * 不能复用最初的 construction.completed 消息：警告处理不会改变任务图 revision，
   * ensure 会命中旧消息，而前端只允许最新动作卡操作，最终确认仍会被历史警告卡遮住。
   * 这里按知识包修订生成独立且幂等的一次性消息，刷新不会重复，内容变更后会重新评审。
   */
  resumeAfterCheckFindingReview(
    projectId: string,
    packageRevisionOid: string,
    attentionKey?: string,
  ): AiConstructionAdvanceResult {
    return this.transaction(() => {
      const current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const milestoneId = current.progress.currentMilestoneId;
      if (milestoneId !== 'knowledge_confirmed' && milestoneId !== 'construction_completed') {
        return { aiConstruction: current };
      }
      const taskKind = milestoneId === 'knowledge_confirmed' ? 'package_validation' : 'completion_review';
      const task = [...current.tasks].reverse().find((item) => (
        item.kind === taskKind
          && item.milestoneId === milestoneId
          && item.status === 'completed'
      ));
      if (!task) return { aiConstruction: current };
      const knowledgeStage = milestoneId === 'knowledge_confirmed';
      const message = this.messages.ensure(projectId, {
        key: actionCardKey(
          knowledgeStage ? 'knowledge.validation.ready' : 'construction.validation.ready',
          packageRevisionOid,
          attentionKey,
        ),
        role: 'assistant',
        taskId: task.id,
        blocks: [
          {
            type: 'status',
            title: '知识包校验提醒已处理完',
            summary: knowledgeStage
              ? '当前修订已经完成逐条检查，可以确认知识结构'
              : '当前修订已经完成逐条检查，可以进行建模完成确认',
            milestoneId,
            taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: knowledgeStage
              ? '请确认当前知识结构；确认后将继续判断需要固化的确定性能力。'
              : '请做最后一次整体检查。确认完成后即可进入场景推演、数据源绑定和真实场景评测。',
          },
          {
            type: 'next_actions',
            title: '选择下一步',
            actions: knowledgeStage ? [
              { id: 'confirm_knowledge', label: '确认知识结构', style: 'primary' },
              { id: 'review_knowledge_files', label: '再次查看知识文件', style: 'secondary' },
            ] : [
              { id: 'confirm_construction', label: '确认完成 AI 辅助构建', style: 'primary' },
              { id: 'review_knowledge_files', label: '再次查看知识文件', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  /**
   * 「忽略」：开一条待办收集理由，而不是在体检面板里就地填表。
   *
   * 走待办而不是本地弹窗，是因为忽略会写进审计记录，理由不能因为刷新页面丢掉。
   */
  requestCheckFindingIgnore(
    projectId: string,
    input: CheckFindingIgnoreInput,
  ): CheckFindingIgnoreRequest {
    return this.transaction(() => {
      const current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      if (input.scope === 'warning' && !input.fingerprint) {
        throw new Error('忽略警告需要提供该条提醒的标识');
      }
      const taskId = current.progress.currentTaskId ?? undefined;
      const existing = this.state.snapshot(projectId).pendingInteractions.find(
        (item) => item.type === 'question_batch'
          && item.payload.purpose === 'check_finding_ignore'
          && item.payload.findingId === input.findingId,
      );
      // 同一条发现重复点「忽略」不再开第二个待办，否则助手栏会堆出一串一样的表单
      if (existing) return { aiConstruction: current, interaction: existing };
      const batched = input.scope === 'errors';
      const started = this.state.apply({
        kind: 'start_run',
        projectId,
        intent: batched ? '暂缓知识包必须修复的错误' : '接受一条可暂缓的提醒',
        baseRevision: input.packageRevisionOid,
        state: { purpose: 'check_finding_ignore', findingId: input.findingId },
      });
      if (started.kind !== 'run') throw new Error('忽略待办创建失败');
      const opened = this.state.apply({
        kind: 'open_interaction',
        projectId,
        runId: started.run.id,
        type: 'question_batch',
        baseRevision: input.packageRevisionOid,
        payload: {
          purpose: 'check_finding_ignore',
          ...(taskId ? { taskId } : {}),
          findingId: input.findingId,
          scope: input.scope,
          ...(input.fingerprint ? { fingerprint: input.fingerprint } : {}),
          ...(batched ? { errorCount: input.errorCount ?? 0 } : {}),
          title: batched ? '说明为什么暂缓这些错误' : '说明为什么可以先不改这条',
          // 错误只能整批暂缓，必须在标题下说清影响面，否则用户以为只忽略了点开的那一条
          description: batched
            ? `暂缓会让当前内容里全部 ${input.errorCount ?? 0} 个必须修复的错误一起先不阻断建模，正式发布前仍然必须改掉。`
            : `这条提醒（${input.target}）将记为已确认影响并放行，只对当前知识包内容有效。`,
          questions: [{
            id: 'ignore_reason',
            kind: 'text',
            q: batched
              ? '请说明暂缓原因和后续处理计划'
              : '请说明影响范围和可以先不改的理由',
            required: true,
            allowOther: true,
            options: [],
          }],
        },
      });
      if (opened.kind !== 'interaction') throw new Error('忽略待办创建失败');
      return { aiConstruction: current, interaction: opened.interaction };
    });
  }

  stopKnowledgeRepair(
    projectId: string,
    input: StopKnowledgeRepairInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      let task = this.findRepairTask(current, new Set(['running', 'waiting_user', 'failed']));
      if (!task) throw new Error('当前没有可停止的知识包修复任务');
      if (task.status === 'waiting_user') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
        task = current.tasks.find((item) => item.id === task!.id)!;
      }
      if (task.status === 'running') {
        current = this.graph.apply(projectId, {
          type: 'fail_task', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const attemptText = input.attempts?.length
        ? `\n已尝试动作：\n${input.attempts.map((item, index) => `${index + 1}. ${item}`).join('\n')}`
        : '';
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '逐项修复已停止',
            summary: '当前知识包仍保留原内容和完整尝试记录',
            milestoneId: 'knowledge_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: `${input.reason}${attemptText}` },
          {
            type: 'next_actions', title: '可以这样继续', actions: [
              ...(input.retryable ? [{
                id: 'repair_knowledge_structure', label: '重新执行逐项修复', style: 'primary' as const,
              }] : []),
              {
                id: 'defer_validation_errors', label: '暂缓错误并继续验证', style: 'secondary' as const,
              },
              {
                id: 'review_knowledge_files', label: '人工检查知识文件',
                style: input.retryable ? 'secondary' : 'primary',
              },
              { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  supersedeKnowledgeRepair(
    projectId: string,
    input: { messageKey: string; reason: string },
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = this.findRepairTask(current);
      if (!task) throw new Error('当前没有可失效的知识包修复任务');
      current = this.graph.apply(projectId, {
        type: 'complete_task', expectedRevision: current.revision, taskId: task.id,
      });
      const message = this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '旧修复待办已失效',
            summary: '知识包内容已经变化，将基于最新版本重新分类校验问题',
            milestoneId: 'knowledge_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: input.reason },
          {
            type: 'next_actions', title: '选择下一步', actions: [
              { id: 'repair_knowledge_structure', label: '重新校验并继续修复', style: 'primary' },
              { id: 'defer_validation_errors', label: '暂缓错误并继续验证', style: 'secondary' },
              { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  failKnowledgeRepair(projectId: string, reason: string): AiConstructionMaterialUpdate {
    return this.stopKnowledgeRepair(projectId, {
      messageKey: `knowledge.repair.failed.${Date.now()}`,
      reason: reason.trim().slice(0, 3_000) || '知识包修复意外中断，请检查后重试。',
    });
  }

  /**
   * 开一轮增量补充。
   *
   * 与「重走一遍」的区别全在这里：只把增量阶段设为活动、只规划一条吸收任务，
   * 已确认的 6 个阶段一个都不碰。阶段本身是懒创建的（ensureIncrementMilestone），
   * 所以存量项目不用迁移。
   */
  startIncrementRound(
    projectId: string,
    input: { packageRevisionOid: string },
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const milestoneId = this.graph.ensureIncrementMilestone(projectId);
      this.graph.activateMilestone(projectId, milestoneId);
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');

      // 上一轮没做完就不再开新的：提案的基线会一直在变，专家永远确认不了一个稳定的东西
      const running = current.tasks.find((task) => task.kind === 'incremental_intake'
        && !['completed', 'skipped', 'cancelled'].includes(task.status));
      if (!running) {
        current = this.graph.apply(projectId, {
          type: 'plan_task',
          expectedRevision: current.revision,
          task: {
            kind: 'incremental_intake',
            milestoneId,
            title: '吸收新增资料并提出增量修改',
            goal: '只根据本轮新增或变更的资料提出修改，已确认且未被涉及的内容保持不变。',
            required: true,
            reason: '知识包已完成建模，本轮只处理增量',
            inputRevisionOid: input.packageRevisionOid,
            dependsOn: [],
          },
        });
      }
      const task = current.tasks.find((item) => item.kind === 'incremental_intake'
        && !['completed', 'skipped', 'cancelled'].includes(item.status));

      const message = this.messages.ensure(projectId, {
        key: `increment.started.${input.packageRevisionOid}`,
        role: 'assistant',
        taskId: task?.id,
        blocks: [
          {
            type: 'status',
            title: '开始增量补充',
            summary: '已确认的内容保持不变，本轮只处理新增或变更的资料',
            milestoneId,
            taskId: task?.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: '请上传这次要补充的资料。我只会根据这些资料提出修改建议，'
              + '并优先修改已有内容而不是新增重复的规则；确认之后会生成新版本并需要重新评测。',
          },
          {
            type: 'next_actions',
            title: '可以这样继续',
            actions: [
              { id: 'upload_materials', label: '上传增量资料', style: 'primary' },
              { id: 'propose_increment', label: '生成增量修改方案', style: 'secondary',
                description: '资料传完后点这里，我会只针对这批资料提出修改' },
              { id: 'review_knowledge_files', label: '先查看现有知识', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  advanceAfterMilestoneConfirmation(
    projectId: string,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
    context: AiConstructionAdvanceContext = {},
  ): AiConstructionAdvanceResult {
    return this.transaction(() => {
      const current = this.graph.snapshot(projectId) ?? confirmedSnapshot;
      // 完成评审排在等待态判断之前：等待确认本身就是它要处理的状态
      if (current.progress.currentMilestoneId === 'construction_completed') {
        return this.prepareCompletionReview(projectId, current, confirmedSnapshot);
      }
      if (current.progress.waitingFor === 'milestone_confirmation') {
        return { aiConstruction: current };
      }
      // 认不出的里程碑不推进，保持原状返回
      return this.advanceStepForMilestone(projectId, current, confirmedSnapshot, context)
        ?? { aiConstruction: current };
    });
  }

  /**
   * 里程碑 → 下一步处理器。原先是一条十几个分支的 if 链，新增里程碑要在链条中间
   * 插对位置才生效；摊平成单层分派后「哪个里程碑交给谁」一眼可见，漏接也看得出来。
   */
  private advanceStepForMilestone(
    projectId: string,
    current: AiConstructionTaskGraphSnapshot,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
    context: AiConstructionAdvanceContext,
  ): AiConstructionAdvanceResult | null {
    switch (current.progress.currentMilestoneId) {
      case 'deterministic_confirmed':
        return this.prepareScriptDecision(
          projectId, current, confirmedSnapshot, context.scriptCandidates ?? [],
        );
      case 'knowledge_confirmed':
        return this.prepareKnowledgeStructure(projectId, current, confirmedSnapshot);
      case 'process_confirmed':
        return this.prepareProcessInterview(projectId, current, confirmedSnapshot);
      case 'materials_ready':
        return this.prepareGoalDirectedMaterialAnalysis(projectId, current);
      case 'goal_confirmed':
        return this.prepareModelingBrief(projectId, current, confirmedSnapshot);
      default:
        return null;
    }
  }

  private prepareModelingBrief(
    projectId: string,
    snapshot: AiConstructionTaskGraphSnapshot,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
  ): AiConstructionAdvanceResult {
    let current = snapshot;
    let task = current.tasks.find((item) => item.kind === 'modeling_brief'
      && item.milestoneId === 'goal_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!task) {
      const materialTask = current.tasks.find((item) => item.kind === 'material_analysis'
        || item.kind === 'package_baseline_review');
      current = this.graph.apply(projectId, {
        type: 'plan_task',
        expectedRevision: current.revision,
        task: {
          kind: 'modeling_brief',
          milestoneId: 'goal_confirmed',
          title: '确认建模目标',
          goal: '说清楚这个知识包要解决什么问题、给谁用、管到哪、不管什么、做到什么程度算成功。',
          required: true,
          reason: '资料范围确认后进入建模目标确认',
          dependsOn: materialTask ? [materialTask.id] : [],
        },
      });
      task = current.tasks.find((item) => item.kind === 'modeling_brief'
        && item.milestoneId === 'goal_confirmed');
    }
    const existingInteraction = this.state.snapshot(projectId).pendingInteractions.find(
      (item) => item.type === 'question_batch' && item.payload.purpose === 'modeling_brief',
    );
    const materialAnalysis = this.latestMaterialAnalysis(projectId);
    let interaction = existingInteraction;
    if (!interaction && task) {
      const taskId = task.id;
      if (task.status !== 'waiting_user') {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId,
        });
        const refreshedTask = current.tasks.find((item) => item.id === taskId);
        if (!refreshedTask) throw new Error('建模目标任务状态刷新失败');
        task = refreshedTask;
      }
      const packageRevision = confirmedSnapshot.confirmations.find(
        (item) => item.milestoneId === 'materials_ready',
      )?.packageRevisionOid;
      const started = this.state.apply({
        kind: 'start_run',
        projectId,
        intent: '确认建模目标',
        baseRevision: packageRevision,
        state: { purpose: 'modeling_brief', taskId },
      });
      if (started.kind !== 'run') throw new Error('建模目标任务创建失败');
      const opened = this.state.apply({
        kind: 'open_interaction',
        projectId,
        runId: started.run.id,
        type: 'question_batch',
        baseRevision: packageRevision,
        payload: {
          purpose: 'modeling_brief',
          taskId,
          title: materialAnalysis ? '请选择最接近的建模目标假设' : '请先确认建模目标',
          description: materialAnalysis
            ? '候选来自已上传资料。优先选择，没有合适项时再填写“其他”。'
            : '先确认建模目标，再上传并围绕目标分析资料。请说明要解决的问题、使用者、典型场景和成功标准。',
          questions: buildModelingBriefQuestions(materialAnalysis),
        },
      });
      if (opened.kind !== 'interaction') throw new Error('建模目标问题创建失败');
      interaction = opened.interaction;
    }
    const message = this.messages.ensure(projectId, {
      key: 'goal_confirmed.started',
      role: 'assistant',
      taskId: task?.id,
      blocks: [
        {
          type: 'status',
          title: '进入建模目标确认',
          summary: `当前任务：${task?.title ?? '确认建模目标'}`,
          milestoneId: 'goal_confirmed',
          taskId: task?.id,
          progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        {
          type: 'text',
          text: materialAnalysis
            ? '我已根据建模资料整理出少量目标假设。请直接选择或补充。'
            : '请先确认这次建模要解决什么问题。后续资料分析、候选流程和知识构建都将以该目标为范围。',
        },
        {
          type: 'next_actions',
          title: '选择一种方式继续',
          actions: [
            { id: 'explain_goal', label: materialAnalysis ? '选择建模目标' : '填写建模目标', style: 'primary' },
            ...(materialAnalysis
              ? [{ id: 'review_materials', label: '先回顾资料分析', style: 'secondary' as const }]
              : []),
          ],
        },
      ],
    });
    return { aiConstruction: current, message, ...(interaction ? { interaction } : {}) };
  }

  private prepareGoalDirectedMaterialAnalysis(
    projectId: string,
    initial: AiConstructionTaskGraphSnapshot,
  ): AiConstructionAdvanceResult {
    let current = initial;
    const goalTask = current.tasks.find((item) => item.kind === 'modeling_brief'
      && item.milestoneId === 'goal_confirmed');
    let materialTask = current.tasks.find((item) => item.kind === 'material_analysis'
      && item.milestoneId === 'materials_ready'
      && !['cancelled', 'stale'].includes(item.status));
    if (!materialTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'material_analysis', milestoneId: 'materials_ready',
          title: '围绕建模目标分析资料',
          goal: '读一遍你上传的资料，找出和建模目标相关的业务流程，并列出需要你补充说明的地方。',
          required: true,
          reason: '建模目标已确认，资料分析必须以该目标为范围',
          dependsOn: goalTask ? [goalTask.id] : [],
        },
      });
      materialTask = current.tasks.find((item) => item.kind === 'material_analysis'
        && item.milestoneId === 'materials_ready');
    }
    if (!materialTask) throw new Error('资料分析任务创建失败');
    const message = this.messages.ensure(projectId, {
      key: 'materials_ready.started', role: 'assistant', taskId: materialTask.id,
      blocks: [
        {
          type: 'status', title: '建模目标已确认',
          summary: '接下来围绕该目标补充并分析建模资料',
          milestoneId: 'materials_ready', taskId: materialTask.id,
          progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        {
          type: 'text',
          text: '请上传能支撑建模目标的业务文档和表格。分析会优先识别与目标直接相关的证据、核心流程和知识缺口。',
        },
        {
          type: 'next_actions', title: '选择下一步', actions: [
            { id: 'upload_materials', label: '上传建模资料', style: 'primary' },
            { id: 'analyze_materials', label: '分析当前资料', style: 'secondary' },
          ],
        },
      ],
    });
    return { aiConstruction: current, message };
  }

  private prepareCompletionReview(
    projectId: string,
    initial: AiConstructionTaskGraphSnapshot,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
  ): AiConstructionAdvanceResult {
    let current = initial;
    const inputRevisionOid = confirmedSnapshot.confirmations.find(
      (item) => item.milestoneId === 'deterministic_confirmed',
    )?.packageRevisionOid;
    const deterministicTask = current.tasks.find((item) => item.milestoneId === 'deterministic_confirmed'
      && item.kind === 'script_generation' && item.status === 'completed')
      ?? current.tasks.find((item) => item.milestoneId === 'deterministic_confirmed'
        && item.kind === 'script_decision' && item.status === 'completed');
    let task = [...current.tasks].reverse().find((item) => item.kind === 'completion_review'
      && item.milestoneId === 'construction_completed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!task) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'completion_review', milestoneId: 'construction_completed',
          title: '检查 AI 辅助构建成果',
          goal: '确认流程、知识结构、脚本和检查记录都已经存档，可以往下走了。',
          required: true, reason: 'AI 辅助构建结束前进行最终完成检查',
          inputRevisionOid,
          dependsOn: deterministicTask ? [deterministicTask.id] : [],
        },
      });
      task = [...current.tasks].reverse().find((item) => item.kind === 'completion_review'
        && item.milestoneId === 'construction_completed'
        && !['cancelled', 'stale'].includes(item.status));
    }
    if (!task) throw new Error('建模完成检查任务创建失败');
    if (task.status !== 'completed') {
      current = this.graph.apply(projectId, {
        type: 'complete_task', expectedRevision: current.revision,
        taskId: task.id, outputRevisionOid: inputRevisionOid,
      });
    }
    const message = this.messages.ensure(projectId, {
      key: `construction.completed.${task.id.slice(-12)}.${current.revision}`.slice(0, 64),
      role: 'assistant', taskId: task.id,
      blocks: [
        {
          type: 'status', title: 'AI 辅助构建已完成',
          summary: '流程、知识结构和确定性能力均已形成可追溯版本',
          milestoneId: 'construction_completed', taskId: task.id,
          progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        { type: 'text', text: '请做最后一次整体检查。确认完成后即可进入场景推演、数据源绑定和真实场景评测。' },
        {
          type: 'next_actions', title: '选择下一步', actions: [
            { id: 'confirm_construction', label: '确认完成 AI 辅助构建', style: 'primary' },
            { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
          ],
        },
      ],
    });
    return { aiConstruction: current, message };
  }

  completeScriptDecision(
    projectId: string,
    input: CompleteScriptDecisionInput,
  ): AiConstructionAdvanceResult {
    return this.transaction(() => {
      const existing = this.messages.findByKey(projectId, input.messageKey);
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.id === input.taskId);
      if (!task || task.kind !== 'script_decision') throw new Error('确定性脚本决策任务不存在');
      if (task.status !== 'completed') {
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: task.id, outputRevisionOid: input.packageRevisionOid,
        });
      }
      if (input.selectedCandidates.length > 0) {
        let generationTask = current.tasks.find((item) => item.kind === 'script_generation'
          && item.milestoneId === 'deterministic_confirmed'
          && !['cancelled', 'stale'].includes(item.status));
        if (!generationTask) {
          current = this.graph.apply(projectId, {
            type: 'plan_task', expectedRevision: current.revision,
            task: {
              kind: 'script_generation', milestoneId: 'deterministic_confirmed',
              title: '生成受控确定性脚本',
              goal: '你确认好输入输出和测试用例之后，为选中的业务操作生成脚本并跑通验证。',
              required: true, reason: '选定的业务操作需要用确定性代码保证稳定重复执行',
              inputRevisionOid: input.packageRevisionOid,
              dependsOn: [task.id],
            },
          });
          generationTask = current.tasks.find((item) => item.kind === 'script_generation'
            && item.milestoneId === 'deterministic_confirmed');
        }
        if (!generationTask) throw new Error('确定性脚本生成任务创建失败');
        if (generationTask.status !== 'waiting_user') {
          current = this.graph.apply(projectId, {
            type: 'wait_for_user', expectedRevision: current.revision, taskId: generationTask.id,
          });
        }
        const existingContract = this.state.snapshot(projectId).pendingInteractions.find(
          (item) => item.type === 'question_batch' && item.payload.purpose === 'script_contract',
        );
        let interaction = existingContract;
        if (!interaction) {
          const started = this.state.apply({
            kind: 'start_run', projectId, intent: '确认确定性脚本契约',
            baseRevision: input.packageRevisionOid,
            state: {
              purpose: 'script_contract', taskId: generationTask.id,
              candidates: input.selectedCandidates as unknown as JsonObject[],
            },
          });
          if (started.kind !== 'run') throw new Error('确定性脚本契约任务创建失败');
          const opened = this.state.apply({
            kind: 'open_interaction', projectId, runId: started.run.id,
            type: 'question_batch', baseRevision: input.packageRevisionOid,
            payload: {
              purpose: 'script_contract', taskId: generationTask.id,
              title: '确认脚本输入、输出和测试',
              description: '每个脚本都必须先确认契约和至少一个测试案例，确认后才会生成变更方案。',
              candidates: input.selectedCandidates as unknown as JsonObject[],
              questions: input.selectedCandidates.map((candidate, index) => {
                const suggestedContract = candidate.suggestedContract?.trim() || [
                  '输入：以业务操作定义的输入和关联对象作为受控输入',
                  '输出：以业务操作定义的业务结果作为结构化输出',
                  '测试：同一合法输入重复执行结果一致；缺少必填输入时明确失败；结果满足已确认的业务操作、规则和指标约束。',
                ].join('\n');
                return {
                  id: `script_contract_${index + 1}`, kind: 'single', required: true,
                  q: `请确认“${candidate.title}”脚本的输入、输出和至少一个测试案例`,
                  allowOther: true,
                  options: [{
                    value: suggestedContract,
                    label: '采用系统建议契约',
                    description: suggestedContract,
                  }],
                };
              }),
            },
          });
          if (opened.kind !== 'interaction') throw new Error('确定性脚本契约问题创建失败');
          interaction = opened.interaction;
        }
        const message = existing ?? this.messages.ensure(projectId, {
          key: input.messageKey, role: 'assistant', taskId: generationTask.id,
          blocks: [
            {
              type: 'status', title: '请确认脚本契约和测试',
              summary: `已选择 ${input.selectedCandidates.length} 个 operation`,
              milestoneId: 'deterministic_confirmed', taskId: generationTask.id,
              progress: current.progress.percent,
              eta: {
                minMinutes: current.progress.eta.minMinutes,
                maxMinutes: current.progress.eta.maxMinutes,
                confidence: current.progress.eta.confidence,
              },
            },
            { type: 'text', text: '请为每个脚本补充结构化输入、输出和至少一个可验证案例。确认后我会生成可审阅的代码变更方案。' },
          ],
        });
        return { aiConstruction: current, message, ...(interaction ? { interaction } : {}) };
      }
      const message = existing ?? this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '确定性脚本决策已确认',
            summary: '本轮不生成脚本，现有业务操作、规则和指标保持不变',
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: '当前知识结构没有需要生成确定性脚本的能力，或您选择本轮暂不生成。可以确认确定性能力阶段。' },
          {
            type: 'next_actions', title: '选择下一步', actions: [
              { id: 'confirm_deterministic', label: '确认确定性能力', style: 'primary' },
              { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  startScriptGeneration(projectId: string): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.kind === 'script_generation'
        && item.milestoneId === 'deterministic_confirmed'
        && !['completed', 'cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('确定性脚本生成任务不存在或已经完成');
      if (task.status !== 'running') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const message = this.messages.append(projectId, {
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '正在生成确定性脚本方案',
            summary: '正在根据已确认契约形成受控变更计划',
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: '脚本不会直接写入知识包；生成变更计划后需要您再次确认。' },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  waitForScriptApproval(projectId: string): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.kind === 'script_generation'
        && item.milestoneId === 'deterministic_confirmed'
        && !['completed', 'cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('确定性脚本生成任务不存在或已经完成');
      if (task.status !== 'waiting_user') {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const message = this.messages.append(projectId, {
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '脚本变更方案等待确认',
            summary: '批准前不会修改知识包文件',
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: '请核对脚本文件、业务操作引用、输入输出和测试案例，再决定是否应用。' },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  completeScriptGeneration(
    projectId: string,
    input: CompleteScriptGenerationInput,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      const existing = this.messages.findByKey(projectId, input.messageKey);
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.kind === 'script_generation'
        && item.milestoneId === 'deterministic_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('确定性脚本生成任务不存在');
      if (input.validation.errors > 0) {
        throw new Error(`脚本校验仍有 ${input.validation.errors} 个必须修复的错误`);
      }
      if (task.status !== 'completed') {
        current = this.graph.apply(projectId, {
          type: 'complete_task', expectedRevision: current.revision,
          taskId: task.id, outputRevisionOid: input.packageRevisionOid,
        });
      }
      const summary = `脚本 ${input.scriptCount} 个 · 错误 ${input.validation.errors} · 警告 ${input.validation.warnings} · 提示 ${input.validation.hints}`;
      const message = existing ?? this.messages.ensure(projectId, {
        key: input.messageKey, role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '确定性脚本已生成并通过校验',
            summary,
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          {
            type: 'text',
            text: `${summary}。脚本已经写入知识包并由 operation 显式引用，可以在工作区逐文件检查。`,
          },
          {
            type: 'next_actions', title: '选择下一步', actions: [
              { id: 'confirm_deterministic', label: '确认确定性能力', style: 'primary' },
              { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  failScriptGeneration(projectId: string, reason: string): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      let task = current.tasks.find((item) => item.kind === 'script_generation'
        && item.milestoneId === 'deterministic_confirmed'
        && !['completed', 'cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('确定性脚本生成任务不存在或已经完成');
      if (task.status !== 'running') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
        task = current.tasks.find((item) => item.id === task!.id)!;
      }
      current = this.graph.apply(projectId, {
        type: 'fail_task', expectedRevision: current.revision, taskId: task.id, reason,
      });
      const message = this.messages.append(projectId, {
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '确定性脚本生成未完成',
            summary: '任务已保留为可重试状态',
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: reason.trim().slice(0, 3000) || '生成过程意外中断，请重新执行。' },
          {
            type: 'next_actions', title: '可以这样继续', actions: [
              { id: 'retry_script_generation', label: '重新生成脚本', style: 'primary' },
              { id: 'defer_script_generation', label: '本轮暂缓脚本', style: 'secondary' },
              { id: 'review_knowledge_files', label: '查看校验问题', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  deferScriptGeneration(
    projectId: string,
    packageRevisionOid: string,
  ): AiConstructionMaterialUpdate {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      const task = current.tasks.find((item) => item.kind === 'script_generation'
        && item.milestoneId === 'deterministic_confirmed'
        && !['completed', 'cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('确定性脚本生成任务不存在或已经完成');
      current = this.graph.apply(projectId, {
        type: 'complete_task', expectedRevision: current.revision,
        taskId: task.id, outputRevisionOid: packageRevisionOid,
      });
      const message = this.messages.append(projectId, {
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '本轮暂缓生成确定性脚本',
            summary: '候选能力继续保留在知识结构中',
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: '本轮没有写入新的确定性脚本，可以继续确认当前确定性能力。' },
          {
            type: 'next_actions', title: '选择下一步', actions: [
              { id: 'confirm_deterministic', label: '本轮暂不生成并继续', style: 'primary' },
              { id: 'retry_script_generation', label: '重新生成脚本', style: 'secondary' },
              { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
            ],
          },
        ],
      });
      return { aiConstruction: current, message };
    });
  }

  retryScriptGeneration(
    projectId: string,
    input: RetryScriptGenerationInput,
  ): AiConstructionAdvanceResult {
    return this.transaction(() => {
      let current = this.graph.snapshot(projectId);
      if (!current) throw new Error('项目尚未初始化 AI 辅助构建');
      let task = current.tasks.find((item) => item.kind === 'script_generation'
        && item.milestoneId === 'deterministic_confirmed'
        && !['cancelled', 'stale'].includes(item.status));
      if (!task) throw new Error('确定性脚本生成任务不存在');
      if (!input.candidates.length) throw new Error('当前知识包没有可重新生成的脚本候选');

      if (task.status === 'completed') {
        const explicitlyDeferred = [...this.messages.list(projectId)].reverse().some((message) => (
          message.taskId === task!.id
          && message.blocks.some((block) => block.type === 'status'
            && block.title === '本轮暂缓生成确定性脚本')
        ));
        if (!explicitlyDeferred) throw new Error('已成功生成的脚本不能通过重试入口覆盖');
        current = this.graph.apply(projectId, {
          type: 'reopen_task', expectedRevision: current.revision, taskId: task.id,
        });
      } else if (task.status === 'failed') {
        current = this.graph.apply(projectId, {
          type: 'start_task', expectedRevision: current.revision, taskId: task.id,
        });
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
        });
      } else if (['pending', 'ready', 'running'].includes(task.status)) {
        if (task.status !== 'running') {
          current = this.graph.apply(projectId, {
            type: 'start_task', expectedRevision: current.revision, taskId: task.id,
          });
        }
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
        });
      } else if (task.status !== 'waiting_user') {
        throw new Error(`当前脚本任务不能重新生成：${task.status}`);
      }
      task = current.tasks.find((item) => item.id === task!.id)!;

      const existing = this.state.snapshot(projectId).pendingInteractions.find(
        (item) => item.type === 'question_batch' && item.payload.purpose === 'script_contract',
      );
      let interaction = existing;
      if (!interaction) {
        const started = this.state.apply({
          kind: 'start_run', projectId, intent: '重新确认确定性脚本契约',
          baseRevision: input.packageRevisionOid,
          state: {
            purpose: 'script_contract', taskId: task.id,
            candidates: input.candidates as unknown as JsonObject[],
          },
        });
        if (started.kind !== 'run') throw new Error('脚本契约任务创建失败');
        const opened = this.state.apply({
          kind: 'open_interaction', projectId, runId: started.run.id,
          type: 'question_batch', baseRevision: input.packageRevisionOid,
          payload: {
            purpose: 'script_contract', taskId: task.id,
            title: '重新确认脚本输入、输出和测试',
            description: '知识包内容可能已变化，请重新确认契约后再生成受控脚本方案。',
            candidates: input.candidates as unknown as JsonObject[],
            questions: input.candidates.map((candidate, index) => {
              const suggestedContract = candidate.suggestedContract?.trim() || [
                '输入：以业务操作定义的输入和关联对象作为受控输入',
                '输出：以业务操作定义的业务结果作为结构化输出',
                '测试：同一合法输入重复执行结果一致；缺少必填输入时明确失败。',
              ].join('\n');
              return {
                id: `script_contract_${index + 1}`, kind: 'single', required: true,
                q: `请确认“${candidate.title}”脚本的输入、输出和至少一个测试案例`,
                allowOther: true,
                options: [{
                  value: suggestedContract,
                  label: '采用系统建议契约',
                  description: suggestedContract,
                }],
              };
            }),
          },
        });
        if (opened.kind !== 'interaction') throw new Error('脚本契约问题创建失败');
        interaction = opened.interaction;
      }
      const message = this.messages.append(projectId, {
        role: 'assistant', taskId: task.id,
        blocks: [
          {
            type: 'status', title: '确定性脚本已进入重新生成',
            summary: `请重新确认 ${input.candidates.length} 个 operation 的脚本契约`,
            milestoneId: 'deterministic_confirmed', taskId: task.id,
            progress: current.progress.percent,
            eta: {
              minMinutes: current.progress.eta.minMinutes,
              maxMinutes: current.progress.eta.maxMinutes,
              confidence: current.progress.eta.confidence,
            },
          },
          { type: 'text', text: '契约确认后会重新生成受控变更方案，确认前不会修改知识包。' },
        ],
      });
      return { aiConstruction: current, message, interaction };
    });
  }

  private prepareScriptDecision(
    projectId: string,
    initial: AiConstructionTaskGraphSnapshot,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
    candidates: ScriptCandidate[],
  ): AiConstructionAdvanceResult {
    let current = initial;
    const validationTask = current.tasks.find((item) => item.kind === 'package_validation'
      && item.milestoneId === 'knowledge_confirmed');
    const inputRevisionOid = confirmedSnapshot.confirmations.find(
      (item) => item.milestoneId === 'knowledge_confirmed',
    )?.packageRevisionOid;
    let task = current.tasks.find((item) => item.kind === 'script_decision'
      && item.milestoneId === 'deterministic_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!task) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'script_decision', milestoneId: 'deterministic_confirmed',
          title: '识别确定性脚本需求',
          goal: '找出哪些步骤是算数、排序、分配这类每次都必须算得一模一样的，它们需要用代码而不是让模型临场判断。',
          required: true, reason: '知识结构确认后判断哪些能力需要由脚本保证确定性',
          inputRevisionOid,
          dependsOn: validationTask ? [validationTask.id] : [],
        },
      });
      task = current.tasks.find((item) => item.kind === 'script_decision'
        && item.milestoneId === 'deterministic_confirmed');
    }
    if (!task) throw new Error('确定性脚本决策任务创建失败');

    const existing = this.state.snapshot(projectId).pendingInteractions.find(
      (item) => item.type === 'question_batch' && item.payload.purpose === 'script_decision',
    );
    let interaction = existing;
    if (!interaction && task.status !== 'completed') {
      if (task.status !== 'waiting_user') {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId: task.id,
        });
      }
      const started = this.state.apply({
        kind: 'start_run', projectId, intent: '确认确定性脚本需求',
        baseRevision: inputRevisionOid,
        state: { purpose: 'script_decision', taskId: task.id },
      });
      if (started.kind !== 'run') throw new Error('确定性脚本决策任务创建失败');
      const opened = this.state.apply({
        kind: 'open_interaction', projectId, runId: started.run.id,
        type: 'question_batch', baseRevision: inputRevisionOid,
        payload: {
          purpose: 'script_decision', taskId: task.id,
          title: '确认需要生成脚本的业务操作',
          description: '请选择需要以确定性代码实现的能力。开放式判断继续保留在业务操作、规则或指标中。',
          candidates: candidates as unknown as JsonObject[],
          questions: [{
            id: 'script_candidates', kind: 'multi',
            q: '哪些业务操作需要生成确定性脚本？', required: true,
            options: [
              ...candidates.map((candidate) => ({
                value: candidate.operationKey,
                label: candidate.title,
                description: `${candidate.confidence === 'high' ? '高' : '中'}置信度 · ${candidate.reason}`,
              })),
              {
                value: '__none__', label: '本轮暂不生成脚本',
                description: '保留为业务操作、规则或指标，不生成确定性代码',
              },
            ],
          }],
        },
      });
      if (opened.kind !== 'interaction') throw new Error('确定性脚本决策问题创建失败');
      interaction = opened.interaction;
    }
    const message = this.messages.ensure(projectId, {
      key: 'deterministic_confirmed.started', role: 'assistant', taskId: task.id,
      blocks: [
        {
          type: 'status', title: '进入确定性能力确认',
          summary: candidates.length > 0
            ? `识别到 ${candidates.length} 个可能需要脚本的 operation`
            : '暂未识别到必须生成脚本的业务操作',
          milestoneId: 'deterministic_confirmed', taskId: task.id,
          progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        { type: 'text', text: '请确认哪些能力必须通过确定性代码实现。选择后还会单独确认输入、输出和测试案例，不会直接生成或执行脚本。' },
      ],
    });
    return { aiConstruction: current, message, ...(interaction ? { interaction } : {}) };
  }

  private prepareKnowledgeStructure(
    projectId: string,
    initial: AiConstructionTaskGraphSnapshot,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
  ): AiConstructionAdvanceResult {
    let current = initial;
    const processTask = current.tasks.find((item) => item.kind === 'process_review'
      && item.milestoneId === 'process_confirmed');
    const inputRevisionOid = confirmedSnapshot.confirmations.find(
      (item) => item.milestoneId === 'process_confirmed',
    )?.packageRevisionOid;
    let operationTask = current.tasks.find((item) => item.kind === 'operation_extraction'
      && item.milestoneId === 'knowledge_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!operationTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'operation_extraction', milestoneId: 'knowledge_confirmed',
          title: '从流程提取业务操作',
          goal: '按确认好的流程，逐个写清楚每步业务操作要什么输入、产出什么、怎么走分支和例外。',
          required: true, reason: '业务流程确认后先沉淀可执行的业务操作',
          inputRevisionOid,
          dependsOn: processTask ? [processTask.id] : [],
        },
      });
      operationTask = current.tasks.find((item) => item.kind === 'operation_extraction'
        && item.milestoneId === 'knowledge_confirmed');
    }
    if (!operationTask) throw new Error('业务操作提取任务创建失败');

    let knowledgeTask = current.tasks.find((item) => item.kind === 'knowledge_extraction'
      && item.milestoneId === 'knowledge_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!knowledgeTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'knowledge_extraction', milestoneId: 'knowledge_confirmed',
          title: '提取对象、关系和规则',
          goal: '从流程、资料和访谈里整理出涉及哪些业务对象、它们之间什么关系、有哪些判断规则和计算口径，并标明每条的出处。',
          required: true, reason: '业务操作需要可追溯的对象、关系、规则和指标支撑',
          inputRevisionOid,
          dependsOn: [operationTask.id],
        },
      });
      knowledgeTask = current.tasks.find((item) => item.kind === 'knowledge_extraction'
        && item.milestoneId === 'knowledge_confirmed');
    }
    if (!knowledgeTask) throw new Error('知识提取任务创建失败');

    let validationTask = current.tasks.find((item) => item.kind === 'package_validation'
      && item.milestoneId === 'knowledge_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!validationTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'package_validation', milestoneId: 'knowledge_confirmed',
          title: '校验知识结构草案',
          goal: '检查知识包有没有缺内容、引用错、和数据对不上的地方。标为必须修复的要处理完才能继续。',
          required: true, reason: '每次确认知识结构前必须通过知识包校验',
          inputRevisionOid,
          dependsOn: [knowledgeTask.id],
        },
      });
      validationTask = current.tasks.find((item) => item.kind === 'package_validation'
        && item.milestoneId === 'knowledge_confirmed');
    }
    if (!validationTask) throw new Error('知识包校验任务创建失败');

    const message = this.messages.ensure(projectId, {
      key: 'knowledge_confirmed.started', role: 'assistant', taskId: operationTask.id,
      blocks: [
        {
          type: 'status', title: '进入知识结构构建',
          summary: '将从已确认流程生成业务操作、对象关系、规则和指标',
          milestoneId: 'knowledge_confirmed', taskId: operationTask.id,
          progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        {
          type: 'text',
          text: '我会先把流程转成可执行的业务操作，再从资料和访谈证据中提取相关对象、关系、规则和指标。生成结果会写入知识包工作区，供您逐文件审阅后确认。',
        },
        {
          type: 'next_actions', title: '选择下一步',
          actions: [
            { id: 'generate_knowledge_structure', label: '生成知识结构', style: 'primary' },
            { id: 'review_process_files', label: '先查看流程文件', style: 'secondary' },
          ],
        },
      ],
    });
    return { aiConstruction: current, message };
  }

  private prepareProcessInterview(
    projectId: string,
    initial: AiConstructionTaskGraphSnapshot,
    confirmedSnapshot: AiConstructionTaskGraphSnapshot,
  ): AiConstructionAdvanceResult {
    let current = initial;
    const goalTask = current.tasks.find((item) => item.kind === 'modeling_brief');
    let interviewTask = current.tasks.find((item) => item.kind === 'process_interview'
      && item.milestoneId === 'process_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!interviewTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'process_interview', milestoneId: 'process_confirmed',
          title: '业务流程访谈',
          goal: '把核心业务流程问清楚：什么情况下触发、谁来做、需要什么、分几步、什么时候会走例外、最后交给谁。',
          required: true, reason: '建模目标确认后进入流程访谈',
          dependsOn: goalTask ? [goalTask.id] : [],
        },
      });
      interviewTask = current.tasks.find((item) => item.kind === 'process_interview'
        && item.milestoneId === 'process_confirmed');
    }
    if (!interviewTask) throw new Error('业务流程访谈任务创建失败');

    let diagramTask = current.tasks.find((item) => item.kind === 'process_diagram'
      && item.milestoneId === 'process_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!diagramTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'process_diagram', milestoneId: 'process_confirmed',
          title: '生成 Mermaid 流程图',
          goal: '把访谈结果画成流程图，供你逐张查看和修改。',
          required: true, reason: '流程访谈结果必须形成可视化流程图',
          dependsOn: [interviewTask.id],
        },
      });
      diagramTask = current.tasks.find((item) => item.kind === 'process_diagram'
        && item.milestoneId === 'process_confirmed');
    }
    if (!diagramTask) throw new Error('流程图任务创建失败');

    let reviewTask = current.tasks.find((item) => item.kind === 'process_review'
      && item.milestoneId === 'process_confirmed'
      && !['cancelled', 'stale'].includes(item.status));
    if (!reviewTask) {
      current = this.graph.apply(projectId, {
        type: 'plan_task', expectedRevision: current.revision,
        task: {
          kind: 'process_review', milestoneId: 'process_confirmed',
          title: '人工确认业务流程图',
          goal: '请你确认流程图画得对：触发条件、步骤顺序、分支和例外情况有没有问题。',
          required: true, reason: '流程图必须人工确认后才能驱动知识生成',
          dependsOn: [diagramTask.id],
        },
      });
      reviewTask = current.tasks.find((item) => item.kind === 'process_review'
        && item.milestoneId === 'process_confirmed');
    }
    if (!reviewTask) throw new Error('流程图确认任务创建失败');

    const existingInteraction = this.state.snapshot(projectId).pendingInteractions.find(
      (item) => item.type === 'question_batch' && item.payload.purpose === 'process_interview',
    );
    let interaction = existingInteraction;
    if (!interaction && !['completed', 'skipped', 'cancelled'].includes(interviewTask.status)) {
      const taskId = interviewTask.id;
      if (interviewTask.status !== 'waiting_user') {
        current = this.graph.apply(projectId, {
          type: 'wait_for_user', expectedRevision: current.revision, taskId,
        });
      }
      const packageRevision = latestConfirmedPackageRevision(confirmedSnapshot);
      const started = this.state.apply({
        kind: 'start_run', projectId, intent: '业务流程访谈', baseRevision: packageRevision,
        state: { purpose: 'process_interview', stage: 'core', taskId },
      });
      if (started.kind !== 'run') throw new Error('业务流程访谈创建失败');
      const candidateProcesses = this.latestMaterialAnalysis(projectId)?.candidateProcesses ?? [];
      const opened = this.state.apply({
        kind: 'open_interaction', projectId, runId: started.run.id,
        type: 'question_batch', baseRevision: packageRevision,
        payload: {
          purpose: 'process_interview', stage: 'core', taskId,
          title: '选择需要建模的核心业务流程',
          description: '候选来自资料分析。信息完整的候选可一次多选并直接形成流程图；只有关键内容缺失时才会追加一小批确认。',
          candidates: candidateProcesses as unknown as JsonObject[],
          questions: processInterviewQuestions(
            'core',
            {},
            candidateProcesses,
          ),
        },
      });
      if (opened.kind !== 'interaction') throw new Error('业务流程访谈问题创建失败');
      interaction = opened.interaction;
    }

    const message = this.messages.ensure(projectId, {
      key: 'process_confirmed.started', role: 'assistant', taskId: interviewTask.id,
      blocks: [
        {
          type: 'status', title: '进入业务流程访谈',
          summary: '当前任务：选择并确认核心业务流程', milestoneId: 'process_confirmed',
          taskId: interviewTask.id, progress: current.progress.percent,
          eta: {
            minMinutes: current.progress.eta.minMinutes,
            maxMinutes: current.progress.eta.maxMinutes,
            confidence: current.progress.eta.confidence,
          },
        },
        {
          type: 'text',
          text: '访谈只围绕业务流程展开。请先选择资料中有证据的核心流程；触发、角色、步骤、异常、输出和交接完整时不会重复要求填写。',
        },
      ],
    });
    return { aiConstruction: current, message, ...(interaction ? { interaction } : {}) };
  }

  private knowledgeRunningBlocks(
    graph: AiConstructionTaskGraphSnapshot,
    taskId: string,
    title: string,
  ): AiConstructionMessage['blocks'] {
    return [
      {
        type: 'status', title: '正在构建知识结构', summary: `当前任务：${title}`,
        milestoneId: 'knowledge_confirmed', taskId, progress: graph.progress.percent,
        eta: {
          minMinutes: graph.progress.eta.minMinutes,
          maxMinutes: graph.progress.eta.maxMinutes,
          confidence: graph.progress.eta.confidence,
        },
      },
      {
        type: 'text',
        text: '我正在依据已确认流程、建模资料和访谈证据生成业务操作、对象关系、规则与指标，并会在提交前执行知识包校验。',
      },
    ];
  }

  private transaction<T>(work: () => T): T {
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = work();
      if (ownsTransaction) this.db.exec('COMMIT');
      return result;
    } catch (error) {
      if (ownsTransaction && this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }
}
