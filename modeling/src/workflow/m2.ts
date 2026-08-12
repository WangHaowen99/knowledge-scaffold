import { createHash } from 'node:crypto';
import type {
  AnalysisResult, ArtifactCandidate, CoverageResult, DataDictTable,
  PackageDataSourceType, PackageSpec, Project, SpecMetric, SpecObject, SpecOperation, SpecRule,
} from '../types.js';
import { buildTaskLlm, type LlmDeps } from '../llm/provider.js';
import {
  buildKnowledgeGenerateTools, buildObjectGenerateTools, buildOperationGenerateTools,
  type KnowledgeSpecCapture,
  type ObjectSpecCapture, type OperationSpecCapture, type ToolContext,
} from './tools.js';
import { makeAgent, runAgentCaptured } from './agent.js';
import { upsertNode, nextOrdinal, listNodes } from './engine.js';
import { normalizeKey } from './keys.js';
import {
  assertObjectDataMappings,
  findObjectDataMappingProblemsForObjects,
  normalizeObjectDataMappings,
  normalizeObjectDataMappingsForObjects,
} from './data-mapping.js';
import { renderPackage } from './package-render.js';
import {
  bindOperationCompetencyQuestions, coverageFor, validateObjectBatch, validateOperationBatch, validateSpecIntegrity,
} from './spec-validation.js';
import { appendTraceEvent, traceEventFromAgentEvent } from './trace.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getPiAuthoringAdapter } from '../authoring/pi-adapter.js';
import { getAuthoringState } from '../authoring/state.js';
import { createSourceCatalog } from './source-catalog.js';
import type { FlowchartOutput } from '../authoring/workflow-extras.js';
import { attachGeneratedKnowledgeEvidence } from './knowledge-evidence.js';
import { tableSourcesFor } from './source-references.js';
import {
  GenerationGateDecisionRequired,
  GenerationWaitingForExpert,
  type GenerationNotice,
  type GenerationOptions,
} from './generation-gate.js';
import {
  GenerationCheckpointStore,
  type GenerationBatchCheckpoint,
  type GenerationBatchOutput,
} from './generation-checkpoint.js';
import { nextValidationCandidateVersion } from './package-version-lifecycle.js';
import { throwIfCancelled } from '../cancellation.js';
import { reportLongTaskProgress } from '../ai-construction/task-progress.js';
import type { ReadableDocument } from './document-read-session.js';
import { attachVerifiedDocumentReadCoverage } from './document-read-coverage.js';
import {
  attachObjectMaterialSources,
  buildObjectGenerationHints,
} from './object-generation-hints.js';

type ConfirmedDiagram = FlowchartOutput['diagrams'][number];

export interface PackageGenerationOptions extends GenerationOptions {
  dataSourceType?: PackageDataSourceType;
}

type Ctx = {
  tables: DataDictTable[];
  documents: Array<ReadableDocument & { id: string }>;
};

function toolCtx(ctx: Ctx, captured: Record<string, unknown> = {}): ToolContext {
  return { tables: ctx.tables, documents: ctx.documents, captured };
}

const chunk = <T>(items: T[], size: number): T[][] => {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
};

function latestAnalysis(projectId: string): AnalysisResult | undefined {
  return [...listNodes(projectId)].reverse()
    .find((node) => node.type === 'analyze' && ['done', 'stale'].includes(node.status))?.output as AnalysisResult | undefined;
}

function appendUnique<T extends { key: string }>(target: T[], incoming: T[], kind: string): void {
  const keys = new Set(target.map((item) => item.key));
  for (const item of incoming) {
    if (keys.has(item.key)) throw new Error(`${kind} key 重复：${item.key}`);
    keys.add(item.key);
    target.push(item);
  }
}

function artifactSummary(items: ArtifactCandidate[]): string {
  return items.length ? JSON.stringify(items) : '（分析阶段未列出候选，请直接检查材料，但不得编造）';
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]));
  }
  return value;
}

function generationPlanHash(input: unknown): string {
  return createHash('sha256')
    .update(JSON.stringify(stableValue(input)))
    .digest('hex');
}

function documentFingerprint(document: Ctx['documents'][number]): string {
  return document.revisionFingerprint
    ?? createHash('sha256').update(document.textContent ?? '').digest('hex');
}

function failureTitle(batchKind: GenerationBatchCheckpoint['batchKind'], batchIndex: number): string {
  if (batchKind === 'object') return `第 ${batchIndex} 批对象连续两次自动补交后仍未通过`;
  if (batchKind === 'knowledge') return `第 ${batchIndex} 批规则/指标连续两次自动补交后仍未结构化提交`;
  return `第 ${batchIndex} 批操作连续两次自动补交后仍未通过`;
}

function checkpointNotice(checkpoint: GenerationBatchCheckpoint): GenerationNotice {
  return {
    phase: checkpoint.phase,
    batchKind: checkpoint.batchKind,
    batchIndex: checkpoint.batchIndex,
    title: failureTitle(checkpoint.batchKind, checkpoint.batchIndex),
    issues: checkpoint.issues,
    decision: 'expert_skipped',
  };
}

function canonicalizeOperationObjectRefs(operation: SpecOperation, objects: SpecObject[]): SpecOperation {
  const keyByNormalized = new Map(objects.map((item) => [normalizeKey(item.key), item.key]));
  const sourceCandidates = new Map<string, SpecObject[]>();
  for (const object of objects) {
    for (const tableSource of tableSourcesFor(object)) {
      const source = normalizeKey(tableSource.table);
      sourceCandidates.set(source, [...(sourceCandidates.get(source) ?? []), object]);
    }
  }
  const relatedObjects = [...new Set((operation.relatedObjects ?? []).map((raw) => {
    const ref = normalizeKey(raw);
    const direct = keyByNormalized.get(ref);
    if (direct) return direct;
    const candidates = sourceCandidates.get(ref) ?? [];
    return candidates.length === 1 ? candidates[0].key : ref;
  }))];
  return { ...operation, relatedObjects };
}

// ---- 知识包生成：确认流程 → 对象 → 规则/指标 → operation → 统一校验与渲染 ----
export async function generatePackage(
  project: Project,
  ctx: Ctx,
  llmOverride: LlmDeps | undefined,
  confirmedFlowchart: FlowchartOutput,
  options: PackageGenerationOptions = {},
): Promise<{
  spec: PackageSpec;
  coverage: CoverageResult;
  files: Record<string, string>;
  version: string;
  revisionId: string;
  generationNotices: GenerationNotice[];
}> {
  const dataSourceType = options.dataSourceType ?? 'neo4j';
  const artifactStore = getPackageArtifactStore();
  const baseRevision = await artifactStore.ensureProject(project.id);
  const basePackage = await artifactStore.current(project.id);
  if (!basePackage) throw new Error('知识包草稿初始化失败');
  if (!confirmedFlowchart || !Array.isArray(confirmedFlowchart.diagrams)) {
    throw new Error('知识包生成必须提供已确认的业务流程');
  }
  type OperationTarget = {
    id: string;
    question: string;
    diagram: ConfirmedDiagram;
  };
  const operationTargets: OperationTarget[] = confirmedFlowchart.diagrams.map((diagram, index) => ({
    id: `process_${diagram.id || index + 1}`,
    question: `按照已确认的「${diagram.title}」流程完成业务处理并返回明确结果`,
    diagram,
  }));
  const selectedQuestions = operationTargets.map((target) => target.question);
  if (!operationTargets.length) throw new Error('缺少人工确认的业务流程，无法生成业务操作');
  const confirmedProcessSummary = JSON.stringify(confirmedFlowchart.diagrams.map((diagram) => ({
    title: diagram.title,
    ...diagramProcessMeta(diagram),
    nodes: diagram.nodes.map((node) => ({ label: node.label, kind: node.kind, detail: node.detail })),
    edges: diagram.edges,
  })));
  const sourceCatalog = createSourceCatalog(
    ctx.tables,
    ctx.documents.flatMap((document) => document.id
      ? [{ id: document.id, filename: document.filename }]
      : []),
  );
  const submittedAnalysis = latestAnalysis(project.id);
  const canonicalAnalysis = submittedAnalysis
    ? sourceCatalog.canonicalizeAnalysis(submittedAnalysis)
    : undefined;
  if (canonicalAnalysis?.problems.length) {
    throw new Error(`历史领域分析来源无效：${canonicalAnalysis.problems.map((problem) => problem.message).join('；')}`);
  }
  const analysis = canonicalAnalysis?.value;
  const resumableRun = options.resumeRunId
    ? getAuthoringState().snapshot(project.id).activeRuns
      .find((candidate) => candidate.id === options.resumeRunId)
    : undefined;
  if (options.resumeRunId && (!resumableRun
    || resumableRun.intent !== '生成知识包'
    || resumableRun.status !== 'running')) {
    throw new Error('原知识生成任务已结束或不可恢复，请重新发起生成');
  }
  if (resumableRun?.baseRevision && resumableRun.baseRevision !== baseRevision.oid) {
    throw new Error('知识包基线已变化，不能沿用此前的生成门禁决定');
  }
  const llm = llmOverride ?? buildTaskLlm();
  const node = upsertNode({ projectId: project.id, type: 'generate', status: 'running', ordinal: nextOrdinal(project.id) });
  const authoring = getPiAuthoringAdapter();
  const authoringRun = resumableRun
    ? authoring.progress(project.id, resumableRun.id, {
        ...resumableRun.state,
        nodeId: node.id,
        step: 'resume_after_generation_gate',
      }).run
    : authoring.start(project.id, '生成知识包', baseRevision.oid, { nodeId: node.id }).run;
  const rawObjectHints = buildObjectGenerationHints(analysis, ctx.tables);
  const objectHints = rawObjectHints.map((candidate, index) => ({
    ...candidate,
    id: `candidate_${index + 1}`,
  }));
  const objectBatches = chunk(objectHints, 4);
  if (!objectBatches.length) objectBatches.push([]);
  const knowledgeCandidates = (analysis?.candidateArtifacts ?? [])
    .filter((item) => item.kind === 'rule' || item.kind === 'metric');
  const knowledgeBatches = chunk(knowledgeCandidates, 5);
  if (!knowledgeBatches.length) knowledgeBatches.push([]);
  const operationCandidates = (analysis?.candidateArtifacts ?? []).filter((item) => item.kind === 'operation');
  const questionBatches = chunk(operationTargets, 3);
  const totalStepCount = objectBatches.length + knowledgeBatches.length + questionBatches.length + 1;
  const messages: unknown[] = [];
  const generationNotices: GenerationNotice[] = [];
  const skipFailedPhases = new Set(options.skipFailedPhases ?? []);
  const trace = (input: Parameters<typeof appendTraceEvent>[0]) => appendTraceEvent(input);
  const observe = (phase: string) => (event: unknown) => {
    const draft = traceEventFromAgentEvent(event);
    if (!draft) return;
    trace({ ...draft, projectId: project.id, nodeId: node.id, phase });
  };
  try {
    const planHash = generationPlanHash({
      baseRevision: baseRevision.oid,
      dataSourceType,
      project: {
        name: project.name,
        domain: project.domain,
        background: project.background,
      },
      confirmedFlowchart,
      analysis,
      objectHints,
      knowledgeCandidates,
      operationCandidates,
      operationTargets,
      tables: ctx.tables,
      documents: ctx.documents.map((document) => ({
        id: document.id,
        filename: document.filename,
        fingerprint: documentFingerprint(document),
      })),
    });
    const checkpointStore = new GenerationCheckpointStore();
    const priorRunId = resumableRun?.state && typeof resumableRun.state.priorRunId === 'string'
      ? resumableRun.state.priorRunId
      : undefined;
    if (priorRunId && priorRunId !== authoringRun.id) {
      checkpointStore.copyToRun({
        projectId: project.id,
        fromRunId: priorRunId,
        toRunId: authoringRun.id,
        planHash,
      });
    }
    const checkpoints = new Map(
      checkpointStore.list(project.id, authoringRun.id, planHash).map((checkpoint) => [checkpoint.phase, checkpoint]),
    );
    for (const checkpoint of checkpoints.values()) {
      if (checkpoint.status === 'skipped') skipFailedPhases.add(checkpoint.phase);
    }
    trace({
      projectId: project.id, nodeId: node.id, phase: 'prepare', kind: 'phase_started', status: 'running',
      title: '准备知识包生成', summary: `业务流程 ${selectedQuestions.length} 个`,
      details: {
        questionCount: selectedQuestions.length,
        objectBatchCount: objectBatches.length,
        knowledgeBatchCount: knowledgeBatches.length,
        operationBatchCount: questionBatches.length,
        totalStepCount,
      },
    });
    // 生成要跑十几到几十分钟，用户只看得到一个转圈——按批次实时上报在干什么、产出了多少
    const generationStartedAt = Date.now();
    const objects: SpecObject[] = [];
    for (const [index, hints] of objectBatches.entries()) {
      throwIfCancelled(project.id, 'knowledgeGenerate');
      reportLongTaskProgress(project.id, {
        taskKinds: ['knowledge_extraction', 'operation_extraction'],
        title: `正在生成第 ${index + 1}/${objectBatches.length} 批业务对象`,
        produced: `已产出 ${objects.length} 个对象`,
        done: index,
        total: objectBatches.length + knowledgeBatches.length,
        elapsedMs: Date.now() - generationStartedAt,
      });
      const phase = `object_batch_${index + 1}`;
      const checkpoint = checkpoints.get(phase);
      if (checkpoint?.status === 'completed') {
        const output = checkpoint.output as Extract<GenerationBatchOutput, { objects: SpecObject[] }>;
        appendUnique(objects, output.objects ?? [], '对象');
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${objectBatches.length} 批业务对象已从检查点恢复`,
          summary: `恢复 ${output.objects?.length ?? 0} 个 · 累计 ${objects.length} 个`,
          details: { batch: index + 1, totalBatches: objectBatches.length, objectCount: output.objects?.length ?? 0, resumed: true },
        });
        continue;
      }
      if (checkpoint?.status === 'skipped' && skipFailedPhases.has(phase)) {
        generationNotices.push(checkpointNotice(checkpoint));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批对象`,
          summary: `恢复已记录的 ${checkpoint.issues.length} 个未解决问题`,
          details: { batch: index + 1, issues: checkpoint.issues, resumed: true },
        });
        continue;
      }
      if (checkpoint?.status === 'failed' && skipFailedPhases.has(phase)) {
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'object', batchIndex: index + 1, issues: checkpoint.issues,
          attemptCount: checkpoint.attemptCount,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push(checkpointNotice(skipped));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批对象`,
          summary: `跳过 ${skipped.issues.length} 个未解决问题，继续生成其余知识`,
          details: { batch: index + 1, issues: skipped.issues, resumed: true },
        });
        continue;
      }
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成业务对象 ${index + 1}/${objectBatches.length}`,
        summary: `当前批次候选 ${hints.length} 个`,
        details: { batch: index + 1, totalBatches: objectBatches.length, candidateCount: hints.length },
      });
      let acceptedObjects: SpecObject[] | undefined;
      let acceptedAttempt = 0;
      const priorAttemptCount = checkpoint?.status === 'failed' ? checkpoint.attemptCount : 0;
      let lastIssues: string[] = [];
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const cap: ObjectSpecCapture = {};
        const sys = [
          '你是知识包对象建模助手。本阶段只生成业务对象，不生成规则、指标或操作。',
          '有资料来源时应给出 sourceRefs：数据对象引用真实 Sheet 和字段；产品、场景、需求等语义对象引用原始文档及章节或偏移。外部输入可以暂不绑定来源。',
          '候选中的 materialEvidence 来自已完成全文覆盖的材料分析，必须用于补充文档来源和正文证据；不得只重新读取文档首页后丢弃这些证据。',
          '数据对象还要给出字段中文名、类型、真实 sourceColumn 和主键；不得虚构数据字典中不存在的字段。',
          '补充查询边界、别名和有材料依据的对象关系。对象别名在整个知识包内必须唯一：忽略 ASCII 大小写和首尾空格后，不得与其他对象的别名、key 或 title 重复。',
          '每个对象必须用 candidateId 认领当前批次中的一个候选，候选名称只用于理解，不用于绑定。',
          `当前批次有 ${hints.length} 个分析候选。候选是建模建议；没有充分证据、不适合当前版本或应由专家补充的候选可以省略。`,
          '用 submit_objects 一次提交当前批次中有依据的对象，最多 4 个。',
          `已生成对象 key：${objects.map((item) => item.key).join(', ') || '（无）'}；当前批次不得重复。`,
          `已生成对象及别名：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title, aliases: item.aliases ?? [] })))}`,
          `当前批次候选：${JSON.stringify(hints)}`,
          `项目背景：${project.background || project.domain || project.name}`,
          ...(attempt > 1 ? [`上次提交不完整：${lastIssues.join('；')}。这次请重新提交整批完整结果，不是只提交缺失项。`] : []),
        ].join('\n');
        const agent = makeAgent(llm, sys, buildObjectGenerateTools(toolCtx(ctx), cap, hints));
        let stageMessages: unknown[];
        try {
          stageMessages = await runAgentCaptured(agent,
            `请完成第 ${index + 1}/${objectBatches.length} 批有依据的对象并调用 submit_objects。`,
            () => !!cap.objects, '请立即调用 submit_objects 提交当前有依据的对象；没有可靠结果时提交空数组。', observe(phase));
        } catch (error) {
          lastIssues = [error instanceof Error ? error.message : String(error)];
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务对象未完成结构化提交`,
            summary: lastIssues[0], details: { attempt, issues: lastIssues },
          });
          if (attempt < 3) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动补交当前对象批次', details: { attempt: attempt + 1, remainingCount: 1 },
          });
          continue;
        }
        messages.push(...stageMessages);
        const submitted = sourceCatalog.canonicalizeObjects((cap.objects ?? []) as SpecObject[]);
        const normalizedObjects = attachVerifiedDocumentReadCoverage(
          attachObjectMaterialSources(
            normalizeObjectDataMappingsForObjects(submitted.value, ctx.tables),
            hints,
            ctx.documents,
          ),
          analysis?.documentReadCoverage,
          ctx.documents,
        );
        lastIssues = [
          ...submitted.problems.map((problem) => problem.message),
          ...findObjectDataMappingProblemsForObjects(normalizedObjects, ctx.tables)
            .map((problem) => problem.message),
          ...validateObjectBatch(hints, normalizedObjects, objects),
        ];
        if (lastIssues.length) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务对象提交不完整`,
            summary: `当前对象批次存在 ${lastIssues.length} 个待修正问题`,
            details: { attempt, missingCount: lastIssues.length, issues: lastIssues },
          });
          if (attempt < 3) {
            trace({
              projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
              title: '自动补交当前对象批次', details: { attempt: attempt + 1, remainingCount: lastIssues.length },
            });
          }
          continue;
        }
        acceptedObjects = normalizedObjects;
        acceptedAttempt = attempt;
        if (attempt > 1) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_completed', status: 'done',
            title: '业务对象自动补交完成', details: { attempt, remainingCount: 0 },
          });
        }
        break;
      }
      if (!acceptedObjects) {
        const failure = {
          phase, batchKind: 'object' as const, batchIndex: index + 1,
          title: `第 ${index + 1} 批对象连续两次自动补交后仍未通过`,
          issues: lastIssues,
        };
        const failedCheckpoint = checkpointStore.saveFailed({
          projectId: project.id, runId: authoringRun.id, planHash, failure, attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, failedCheckpoint);
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: failure.batchKind, batchIndex: failure.batchIndex, issues: failure.issues,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push({ ...failure, decision: 'expert_skipped' });
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批对象`,
          summary: `跳过 ${lastIssues.length} 个未解决问题，继续生成其余知识`,
          details: { batch: index + 1, issues: lastIssues },
        });
        continue;
      }
      appendUnique(objects, acceptedObjects, '对象');
      const completedCheckpoint = checkpointStore.saveCompleted({
        projectId: project.id, runId: authoringRun.id, planHash, phase,
        batchKind: 'object', batchIndex: index + 1,
        output: { objects: acceptedObjects }, attemptCount: priorAttemptCount + acceptedAttempt,
      });
      checkpoints.set(phase, completedCheckpoint);
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${objectBatches.length} 批业务对象已生成`,
        summary: `本批 ${acceptedObjects.length} 个 · 累计 ${objects.length} 个`,
        details: { batch: index + 1, totalBatches: objectBatches.length, objectCount: acceptedObjects.length },
      });
    }

    const rules: SpecRule[] = [];
    const metrics: SpecMetric[] = [];
    for (const [index, candidates] of knowledgeBatches.entries()) {
      throwIfCancelled(project.id, 'knowledgeGenerate');
      reportLongTaskProgress(project.id, {
        taskKinds: ['knowledge_extraction', 'operation_extraction'],
        title: `正在生成第 ${index + 1}/${knowledgeBatches.length} 批规则和指标`,
        produced: `已产出 ${objects.length} 个对象、${rules.length} 条规则、${metrics.length} 个指标`,
        done: objectBatches.length + index,
        total: objectBatches.length + knowledgeBatches.length,
        elapsedMs: Date.now() - generationStartedAt,
      });
      const phase = `knowledge_batch_${index + 1}`;
      const checkpoint = checkpoints.get(phase);
      if (checkpoint?.status === 'completed') {
        const output = checkpoint.output as Extract<GenerationBatchOutput, { rules: SpecRule[]; metrics: SpecMetric[] }>;
        appendUnique(rules, output.rules ?? [], '规则');
        appendUnique(metrics, output.metrics ?? [], '指标');
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${knowledgeBatches.length} 批规则和指标已从检查点恢复`,
          summary: `恢复规则 ${output.rules?.length ?? 0} 条、指标 ${output.metrics?.length ?? 0} 个`,
          details: { batch: index + 1, totalBatches: knowledgeBatches.length, ruleCount: output.rules?.length ?? 0, metricCount: output.metrics?.length ?? 0, resumed: true },
        });
        continue;
      }
      if (checkpoint?.status === 'skipped' && skipFailedPhases.has(phase)) {
        generationNotices.push(checkpointNotice(checkpoint));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批规则/指标`,
          summary: `恢复已记录的 ${checkpoint.issues.length} 个未解决问题`,
          details: { batch: index + 1, issues: checkpoint.issues, resumed: true },
        });
        continue;
      }
      if (checkpoint?.status === 'failed' && skipFailedPhases.has(phase)) {
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'knowledge', batchIndex: index + 1, issues: checkpoint.issues,
          attemptCount: checkpoint.attemptCount,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push(checkpointNotice(skipped));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批规则/指标`,
          summary: `跳过 ${skipped.issues.length} 个未解决问题，继续生成其余知识`,
          details: { batch: index + 1, issues: skipped.issues, resumed: true },
        });
        continue;
      }
      const suggestedRules = candidates.filter((item) => item.kind === 'rule' && item.readiness === 'extractable').length;
      const suggestedMetrics = candidates.filter((item) => item.kind === 'metric' && item.readiness === 'extractable').length;
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成规则和指标 ${index + 1}/${knowledgeBatches.length}`,
        summary: `分析建议规则 ${suggestedRules} 条 · 指标 ${suggestedMetrics} 个`,
        details: { batch: index + 1, totalBatches: knowledgeBatches.length, ruleCount: suggestedRules, metricCount: suggestedMetrics },
      });
      let accepted = false;
      let acceptedRules: SpecRule[] = [];
      let acceptedMetrics: SpecMetric[] = [];
      let acceptedAttempt = 0;
      const priorAttemptCount = checkpoint?.status === 'failed' ? checkpoint.attemptCount : 0;
      let lastCounts = { rules: 0, metrics: 0 };
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const cap: KnowledgeSpecCapture = {};
        const sys = [
          '你是知识包规则与指标提取助手。本阶段只生成 rule 和 metric，不生成 operation。',
          '适用条件、判断门槛、例外和产品匹配条件沉淀为 rule；明确公式、统计粒度和边界的口径沉淀为 metric。',
          '必须能在材料中找到依据；缺少阈值、公式或权威映射时不要猜测，返回空数组并留给访谈澄清。',
          'readiness=needs_clarification 的候选只有在访谈结论已明确补齐条件或口径时才能生成；否则不产出。',
          `本批有 ${suggestedRules} 条 extractable rule 候选、${suggestedMetrics} 条 extractable metric 候选。它们是分析建议，可根据证据合并、省略或留给专家补充。`,
          'rule/metric 引用的对象只能使用下列对象 key。用 submit_rules_metrics 提交当前批次，单类最多 5 条。',
          `对象：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title })))}`,
          `已生成规则 key：${rules.map((item) => item.key).join(', ') || '（无）'}；已生成指标 key：${metrics.map((item) => item.key).join(', ') || '（无）'}。`,
          `已确认流程摘要：${confirmedProcessSummary.slice(0, 2400)}`,
          `当前批次材料候选：${artifactSummary(candidates)}`,
        ].join('\n');
        const agent = makeAgent(llm, sys, buildKnowledgeGenerateTools(toolCtx(ctx), cap));
        const retryHint = attempt === 1 ? '' : '上次没有完成结构化提交，请重新调用工具提交有依据的结果。';
        let stageMessages: unknown[];
        try {
          stageMessages = await runAgentCaptured(agent,
            `请完成第 ${index + 1}/${knowledgeBatches.length} 批规则和指标提取并调用 submit_rules_metrics。${retryHint}`,
            () => !!cap.rules && !!cap.metrics,
            '请立即调用 submit_rules_metrics；没有可靠内容时提交空数组。', observe(phase));
        } catch (error) {
          const issue = error instanceof Error ? error.message : String(error);
          lastCounts = { rules: 0, metrics: 0 };
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批规则/指标未完成结构化提交`,
            summary: issue, details: { attempt, issues: [issue] },
          });
          if (attempt < 3) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动重试当前规则/指标批次', details: { attempt: attempt + 1 },
          });
          continue;
        }
        messages.push(...stageMessages);
        lastCounts = { rules: cap.rules?.length ?? 0, metrics: cap.metrics?.length ?? 0 };
        if (!cap.rules || !cap.metrics) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批规则/指标未完成结构化提交`,
            summary: '模型没有同时提交 rules 和 metrics 数组',
            details: { attempt, missingCount: 1, ruleCount: lastCounts.rules, metricCount: lastCounts.metrics },
          });
          if (attempt < 3) {
            trace({
              projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
              title: '自动重试当前规则/指标批次', details: { attempt: attempt + 1 },
            });
          }
          continue;
        }
        acceptedRules = cap.rules as SpecRule[];
        acceptedMetrics = cap.metrics as SpecMetric[];
        appendUnique(rules, acceptedRules, '规则');
        appendUnique(metrics, acceptedMetrics, '指标');
        acceptedAttempt = attempt;
        if (attempt > 1) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_completed', status: 'done',
            title: '规则/指标自动重试完成', details: { attempt, remainingCount: 0 },
          });
        }
        accepted = true;
        break;
      }
      if (!accepted) {
        const failure = {
          phase, batchKind: 'knowledge' as const, batchIndex: index + 1,
          title: `第 ${index + 1} 批规则/指标连续两次自动补交后仍未结构化提交`,
          issues: [`实际提交规则 ${lastCounts.rules} 条、指标 ${lastCounts.metrics} 条`],
        };
        const failedCheckpoint = checkpointStore.saveFailed({
          projectId: project.id, runId: authoringRun.id, planHash, failure, attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, failedCheckpoint);
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: failure.batchKind, batchIndex: failure.batchIndex, issues: failure.issues,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push({ ...failure, decision: 'expert_skipped' });
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批规则/指标`,
          summary: '继续生成其余知识', details: { batch: index + 1, issues: failure.issues },
        });
        continue;
      }
      const completedCheckpoint = checkpointStore.saveCompleted({
        projectId: project.id, runId: authoringRun.id, planHash, phase,
        batchKind: 'knowledge', batchIndex: index + 1,
        output: { rules: acceptedRules, metrics: acceptedMetrics },
        attemptCount: priorAttemptCount + acceptedAttempt,
      });
      checkpoints.set(phase, completedCheckpoint);
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${knowledgeBatches.length} 批规则和指标已生成`,
        summary: `累计规则 ${rules.length} 条 · 指标 ${metrics.length} 个`,
        details: { batch: index + 1, totalBatches: knowledgeBatches.length, ruleCount: rules.length, metricCount: metrics.length },
      });
    }

    const operations: SpecOperation[] = [];
    for (const [index, questions] of questionBatches.entries()) {
      const phase = `operation_batch_${index + 1}`;
      const checkpoint = checkpoints.get(phase);
      if (checkpoint?.status === 'completed') {
        const output = checkpoint.output as Extract<GenerationBatchOutput, { operations: SpecOperation[] }>;
        appendUnique(operations, output.operations ?? [], '操作');
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${questionBatches.length} 批业务操作已从检查点恢复`,
          summary: `恢复 ${output.operations?.length ?? 0} 个 · 累计 ${operations.length} 个`,
          details: { batch: index + 1, totalBatches: questionBatches.length, operationCount: output.operations?.length ?? 0, resumed: true },
        });
        continue;
      }
      if (checkpoint?.status === 'skipped' && skipFailedPhases.has(phase)) {
        generationNotices.push(checkpointNotice(checkpoint));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批操作`,
          summary: `恢复已记录的 ${checkpoint.issues.length} 个未解决问题`,
          details: { batch: index + 1, issues: checkpoint.issues, resumed: true },
        });
        continue;
      }
      if (checkpoint?.status === 'failed' && skipFailedPhases.has(phase)) {
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'operation', batchIndex: index + 1, issues: checkpoint.issues,
          attemptCount: checkpoint.attemptCount,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push(checkpointNotice(skipped));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批操作`,
          summary: `跳过 ${skipped.issues.length} 个未解决问题，继续生成其余知识`,
          details: { batch: index + 1, issues: skipped.issues, resumed: true },
        });
        continue;
      }
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成业务操作 ${index + 1}/${questionBatches.length}`,
        summary: `覆盖本批 ${questions.length} 个业务流程`,
        details: { batch: index + 1, totalBatches: questionBatches.length, questionCount: questions.length },
      });
      let acceptedOperations: SpecOperation[] | undefined;
      let acceptedAttempt = 0;
      const priorAttemptCount = checkpoint?.status === 'failed' ? checkpoint.attemptCount : 0;
      let lastValidation = validateOperationBatch(questions, [], { objects, rules, metrics });
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const cap: OperationSpecCapture = {};
        const sys = [
          '你是知识包操作建模助手。本阶段只生成 operation。',
          '当前批次的每个已确认流程都必须生成一个 operation，只填写给定的 competencyQuestionId；目标描述由服务端回填。',
          'operation 给出输入、输出、逐步流程和关联对象；flow 数组每项只写步骤正文，不要添加 1.、2. 等序号；流程引用对象写 object:<key>。',
          '需要判断条件或口径时优先引用已有 rule/metric，所有 relatedObjects、usesRules 和 usesMetrics 只能使用下方明确列出的 key。',
          '不得引用未声明的 key，也不得把缺少材料依据的阈值写进流程。',
          '用 submit_operations 一次提交当前批次全部结果，最多 4 个 operation。',
          `已生成 operation key：${operations.map((item) => item.key).join(', ') || '（无）'}；当前批次不得重复。`,
          `对象摘要：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title, sourceRefs: item.sourceRefs, sourceTable: item.sourceTable, fields: item.fields.map((field) => field.key) })))}`,
          `可用规则：${JSON.stringify(rules.map((item) => ({ key: item.key, title: item.title })))}`,
          `可用指标：${JSON.stringify(metrics.map((item) => ({ key: item.key, title: item.title })))}`,
          `操作候选参考：${artifactSummary(operationCandidates)}`,
          `已确认流程摘要：${confirmedProcessSummary.slice(0, 2400)}`,
          `本批已确认流程：\n${questions.map((target, i) => [
            `${i + 1}. [${target.id}] ${target.question}`,
            JSON.stringify({
              title: target.diagram.title,
              ...diagramProcessMeta(target.diagram),
              nodes: target.diagram.nodes.map((item) => ({
                id: item.id, label: item.label, kind: item.kind, detail: item.detail,
              })),
              edges: target.diagram.edges,
            }),
          ].filter(Boolean).join('\n')).join('\n')}`,
          ...(attempt > 1 ? [`上次提交未通过校验：${lastValidation.issues.join('；')}。请重新提交本批全部 operation。`] : []),
        ].join('\n');
        const agent = makeAgent(llm, sys, buildOperationGenerateTools(toolCtx(ctx), cap, questions));
        let stageMessages: unknown[];
        try {
          stageMessages = await runAgentCaptured(agent,
            `请完成第 ${index + 1}/${questionBatches.length} 批有依据的 operation 并调用 submit_operations。`,
            () => !!cap.operations, '请立即调用 submit_operations 提交当前有依据的操作；没有可靠结果时提交空数组。', observe(phase));
        } catch (error) {
          const issue = error instanceof Error ? error.message : String(error);
          lastValidation = { issues: [issue], uncoveredQuestions: [], invalidReferenceCount: 0 };
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务操作未完成结构化提交`,
            summary: issue, details: { attempt, issues: [issue] },
          });
          if (attempt < 3) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动补交当前业务操作批次', details: { attempt: attempt + 1, remainingCount: 1 },
          });
          continue;
        }
        messages.push(...stageMessages);
        const bound = bindOperationCompetencyQuestions(questions, (cap.operations ?? []) as SpecOperation[]);
        const submitted = bound.operations
          .map((operation) => canonicalizeOperationObjectRefs(operation, objects));
        const validated = validateOperationBatch(questions, submitted, { objects, rules, metrics });
        lastValidation = { ...validated, issues: [...bound.issues, ...validated.issues] };
        if (lastValidation.issues.length) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务操作提交不完整`,
            summary: `未覆盖 ${lastValidation.uncoveredQuestions.length} 个问题 · 无效引用 ${lastValidation.invalidReferenceCount} 处`,
            details: { attempt, missingCount: lastValidation.issues.length, issues: lastValidation.issues },
          });
          if (attempt < 3) {
            trace({
              projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
              title: '自动补交当前业务操作批次', details: { attempt: attempt + 1, remainingCount: lastValidation.issues.length },
            });
          }
          continue;
        }
        acceptedOperations = submitted;
        acceptedAttempt = attempt;
        if (attempt > 1) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_completed', status: 'done',
            title: '业务操作自动补交完成', details: { attempt, remainingCount: 0 },
          });
        }
        break;
      }
      if (!acceptedOperations) {
        const failure = {
          phase, batchKind: 'operation' as const, batchIndex: index + 1,
          title: `第 ${index + 1} 批操作连续两次自动补交后仍未通过`,
          issues: lastValidation.issues,
        };
        const failedCheckpoint = checkpointStore.saveFailed({
          projectId: project.id, runId: authoringRun.id, planHash, failure, attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, failedCheckpoint);
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: failure.batchKind, batchIndex: failure.batchIndex, issues: failure.issues,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push({ ...failure, decision: 'expert_skipped' });
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批操作`,
          summary: `跳过 ${lastValidation.issues.length} 个未解决问题，继续生成其余知识`,
          details: { batch: index + 1, issues: lastValidation.issues },
        });
        continue;
      }
      appendUnique(operations, acceptedOperations, '操作');
      const completedCheckpoint = checkpointStore.saveCompleted({
        projectId: project.id, runId: authoringRun.id, planHash, phase,
        batchKind: 'operation', batchIndex: index + 1,
        output: { operations: acceptedOperations }, attemptCount: priorAttemptCount + acceptedAttempt,
      });
      checkpoints.set(phase, completedCheckpoint);
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${questionBatches.length} 批业务操作已生成`,
        summary: `本批 ${acceptedOperations.length} 个 · 累计 ${operations.length} 个`,
        details: { batch: index + 1, totalBatches: questionBatches.length, operationCount: acceptedOperations.length },
      });
    }

    const version = nextValidationCandidateVersion({
      currentVersion: basePackage.packageVersion,
      currentStatus: basePackage.spec.status ?? 'draft',
    });
    const rawSpec = attachGeneratedKnowledgeEvidence({
      id: `${slug(project.name)}-agent`,
      name: project.name,
      description: project.background || project.domain || project.name,
      version,
      status: 'review',
      language: 'zh-CN',
      defaultProviderAlias: slug(project.domain || 'data'),
      objects, operations, rules, metrics,
      dataSourceType,
    }, analysis, confirmedFlowchart.diagrams, {
      documents: ctx.documents
        .filter((document): document is typeof document & { id: string } => Boolean(document.id))
        .map((document) => ({ id: document.id, filename: document.filename })),
      tables: ctx.tables.map((table) => ({ name: table.name, documentId: table.documentId })),
    });
    const spec = normalizeObjectDataMappings(rawSpec, ctx.tables);
    trace({
      projectId: project.id, nodeId: node.id, phase: 'validation', kind: 'phase_started', status: 'running',
      title: '校验知识包完整性',
      summary: `对象 ${objects.length} · 规则 ${rules.length} · 指标 ${metrics.length} · 操作 ${operations.length}`,
      details: { objectCount: objects.length, ruleCount: rules.length, metricCount: metrics.length, operationCount: operations.length },
    });
    assertObjectDataMappings(spec, ctx.tables);
    const skippedKnowledgeCandidates = new Set(
      generationNotices
        .filter((notice) => notice.batchKind === 'knowledge')
        .flatMap((notice) => knowledgeBatches[notice.batchIndex - 1] ?? []),
    );
    const skippedQuestionIds = new Set(
      generationNotices
        .filter((notice) => notice.batchKind === 'operation')
        .flatMap((notice) => questionBatches[notice.batchIndex - 1] ?? [])
        .map((question) => question.id),
    );
    const validationAnalysis: AnalysisResult | undefined = analysis && skippedKnowledgeCandidates.size
      ? {
        ...analysis,
        candidateArtifacts: (analysis.candidateArtifacts ?? [])
          .filter((candidate) => !skippedKnowledgeCandidates.has(candidate)),
      }
      : analysis;
    const validationQuestions = operationTargets
      .filter((question) => !skippedQuestionIds.has(question.id))
      .map((question) => question.question);
    validateSpecIntegrity(spec, validationQuestions, validationAnalysis);
    const coverage = coverageFor(spec, selectedQuestions);
    trace({
      projectId: project.id, nodeId: node.id, phase: 'validation', kind: 'phase_completed', status: 'done',
      title: '知识包完整性校验通过',
      summary: `已覆盖 ${coverage.covered.length} 条 · 未覆盖 ${coverage.uncovered.length} 条`,
      details: { coveredCount: coverage.covered.length, uncoveredCount: coverage.uncovered.length },
    });
    const files = renderPackage(spec);
    const saved = await artifactStore.savePackageVersion(project.id, {
      expectedRevision: baseRevision.oid,
      spec,
      coverage,
      note: node.id,
      action: {
        cause: 'generate', summary: `生成版本 ${version}`, actorKind: 'agent', actorName: '智能助手',
      },
    });
    upsertNode({
      id: node.id, projectId: project.id, type: 'generate', status: 'done', ordinal: node.ordinal,
      agentMessages: messages,
      output: {
        version, coverage, objectCount: spec.objects.length, operationCount: spec.operations.length,
        ruleCount: spec.rules?.length ?? 0, metricCount: spec.metrics?.length ?? 0,
        dataSourceType,
      },
    });
    trace({
      projectId: project.id, nodeId: node.id, phase: 'complete', kind: 'generation_completed', status: 'done',
      title: '知识包生成完成', summary: `版本 ${version}`,
    });
    authoring.progress(project.id, authoringRun.id, {
      nodeId: node.id,
      step: 'complete',
    }, {
      type: 'package.revision.created',
      payload: { revisionId: saved.revision.oid, cause: 'generate', packageVersion: version },
    });
    authoring.complete(project.id, authoringRun.id, {
      version,
      revisionId: saved.revision.oid,
      objectCount: spec.objects.length,
      operationCount: spec.operations.length,
      skippedBatchCount: generationNotices.length,
    });
    return { spec, coverage, files, version, revisionId: saved.revision.oid, generationNotices };
  } catch (error) {
    if (error instanceof GenerationGateDecisionRequired) {
      const reason = error.message;
      upsertNode({
        id: node.id, projectId: project.id, type: 'generate', status: 'waiting_user', ordinal: node.ordinal,
        reason, agentMessages: messages,
      });
      trace({
        projectId: project.id, nodeId: node.id, phase: error.failure.phase,
        kind: 'validation_warning', status: 'warning',
        title: '模型自动补交已耗尽，等待专家决定', summary: error.failure.title,
        details: { issues: error.failure.issues, batchKind: error.failure.batchKind, batchIndex: error.failure.batchIndex },
      });
      const requested = authoring.request(project.id, authoringRun.id, 'decision', {
        purpose: 'generation_gate_skip',
        phase: error.failure.phase,
        batchKind: error.failure.batchKind,
        batchIndex: error.failure.batchIndex,
        title: '确认未通过批次的处理方式',
        prompt: `${error.failure.title}。请选择重新生成，或跳过该批次继续生成其余知识。`,
        issues: error.failure.issues,
        defaultValue: 'retry',
        allowCustom: false,
        options: [
          { value: 'retry', label: '重新生成', note: '重新运行知识生成，模型会再次读取资料并提交。' },
          { value: 'skip', label: '跳过该批继续', note: '不写入本批未通过内容，其余内容继续生成并记录专家决策。' },
        ],
      }, baseRevision.oid);
      throw new GenerationWaitingForExpert(error.failure, requested.interaction);
    }
    const reason = error instanceof Error ? error.message : String(error);
    upsertNode({
      id: node.id, projectId: project.id, type: 'generate', status: 'failed', ordinal: node.ordinal,
      reason, agentMessages: messages,
    });
    trace({
      projectId: project.id, nodeId: node.id, phase: 'failed', kind: 'generation_failed', status: 'error',
      title: '知识包生成失败', summary: reason,
    });
    try { authoring.fail(project.id, authoringRun.id, reason); } catch { /* 保留原始失败原因 */ }
    throw error;
  }
}

function diagramProcessMeta(diagram: ConfirmedDiagram): {
  trigger?: string;
  actors?: string[];
  inputs?: string[];
  exceptions?: string[];
  outputs?: string[];
  handoffs?: string[];
} {
  const meta = diagram.processMeta ?? {};
  return {
    trigger: meta.trigger,
    actors: meta.actors,
    inputs: meta.inputs,
    exceptions: meta.exceptions,
    outputs: meta.outputs,
    handoffs: meta.handoffs,
  };
}

export function checkCoverage(spec: PackageSpec, selected: string[]): CoverageResult {
  return coverageFor(spec, selected);
}

function slug(s: string): string {
  // consumer 的 ontology id 仅允许字母数字下划线短横线且字母/数字开头——中文必须剔除
  return (s || 'pkg').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'pkg';
}
