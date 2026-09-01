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
  bindOperationCompetencyQuestions, coverageFor, validateKnowledgeBatch,
  validateKnowledgeOutputs, validateObjectBatch, validateOperationBatch,
  validateOperationOutputs, validateSpecIntegrity,
} from './spec-validation.js';
import { appendTraceEvent, traceEventFromAgentEvent } from './trace.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getPiAuthoringAdapter } from '../authoring/pi-adapter.js';
import { getAuthoringState } from '../authoring/state.js';
import { createSourceCatalog } from './source-catalog.js';
import type { FlowchartOutput } from '../authoring/workflow-extras.js';
import { attachGeneratedKnowledgeEvidence } from './knowledge-evidence.js';
import { RELATION_NAME_GENERATION_GUIDANCE } from './relation-name.js';
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
import { startJobHeartbeat } from '../authoring/job-observability.js';
import {
  classifyGenerationError,
  generationModelCallError,
  shouldRetryGenerationError,
} from './generation-error-policy.js';
import type { ReadableDocument } from './document-read-session.js';
import { attachVerifiedDocumentReadCoverage } from './document-read-coverage.js';
import {
  attachObjectMaterialSources,
  buildKnowledgeGenerationCandidates,
  buildObjectGenerationHints,
} from './object-generation-hints.js';
import { buildPhysicalSchemaObjects, detectSchemaManifests } from './schema-manifest.js';
import { enforceObjectSourcePolicy, schemaObjectPolicyIssues } from './schema-object-policy.js';
import {
  dispositionForObjectAndOperationCandidates,
  validateGeneratedKnowledgeResources,
  planArtifactCandidates,
  validateKnowledgeCandidateDispositions,
} from './candidate-disposition.js';
import {
  AdaptiveGenerationGate,
  configuredGenerationConcurrency,
  dependencyHash as generationDependencyHash,
  mapWithConcurrency,
  planGenerationUnits,
  waitBeforeGenerationRetry,
} from './generation-planner.js';

type ConfirmedDiagram = FlowchartOutput['diagrams'][number];

export interface OperationTarget {
  id: string;
  question: string;
  diagram: ConfirmedDiagram;
  focusNodeId?: string;
  focusNodeLabel?: string;
  requiresRule?: boolean;
  requiresMetric?: boolean;
}

const RULE_STEP = /(?:判断|校验|准入|匹配|分类|识别|审核|审批|筛选|例外|异常)/u;
const METRIC_STEP = /(?:计算|测算|评估|评分|排序|聚合|汇总|占比|比例|金额|强度|指标)/u;

/**
 * 一张确认流程图同时产生一个总编排 operation 和可独立测试的步骤 operation。
 * 单步骤流程保持原来的一图一操作，避免制造没有复用价值的重复资源。
 */
export function buildOperationTargets(diagrams: ConfirmedDiagram[]): OperationTarget[] {
  return diagrams.flatMap((diagram, index) => {
    const processId = `process_${diagram.id || index + 1}`;
    const orchestrator: OperationTarget = {
      id: processId,
      question: `按照已确认的「${diagram.title}」流程完成业务处理并返回明确结果`,
      diagram,
    };
    const executable = diagram.nodes.filter((node) => node.kind === 'action' || node.kind === 'decision');
    if (executable.length <= 1) return [orchestrator];
    return [orchestrator, ...executable.map((node): OperationTarget => {
      const semantic = `${node.label}\n${node.detail ?? ''}`;
      return {
        id: `${processId}:node_${node.id}`,
        question: `按照已确认的「${diagram.title}」流程，独立完成步骤「${node.label}」并返回明确结果`,
        diagram,
        focusNodeId: node.id,
        focusNodeLabel: node.label,
        requiresRule: node.kind === 'decision' || RULE_STEP.test(semantic),
        requiresMetric: METRIC_STEP.test(semantic),
      };
    })];
  });
}

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
    // 计划指纹只用于判断输入是否变化；键排序必须不改变数组语义，
    // 由调用方在构建计划时显式传入稳定顺序。
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
  if (batchKind === 'knowledge') return `第 ${batchIndex} 批规则/指标连续两次自动补交后仍未通过`;
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
  const operationTargets = buildOperationTargets(confirmedFlowchart.diagrams);
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
        nodeId: node.id,
      step: 'resume_after_generation_gate',
      purpose: 'knowledge_generation',
      phase: '知识结构生成',
      currentStep: '恢复知识包生成',
      progress: 0,
      retryCount: 0,
    }).run
    : authoring.start(project.id, '生成知识包', baseRevision.oid, {
      nodeId: node.id,
      purpose: 'knowledge_generation',
      phase: '知识结构生成',
      currentStep: '准备知识包生成',
      progress: 0,
      retryCount: 0,
    }).run;
  // 单个模型批次可能超过 90 秒；批次进度只在边界上报，统一 Job 仍需持续心跳。
  // 进入 waiting/completed/failed 时由下方 finally 停止，且 adapter 会保护 waiting 不被改回 running。
  const stopJobHeartbeat = startJobHeartbeat(
    authoring,
    project.id,
    authoringRun.id,
    { phase: '知识结构生成', currentStep: '准备知识包生成', progress: 0, retryCount: 0 },
  );
  const schemaOnlyTables = ctx.tables.filter((table) => table.materialPurpose === 'schema');
  const physicalSchemaObjects = buildPhysicalSchemaObjects(schemaOnlyTables);
  const schemaManifests = detectSchemaManifests(schemaOnlyTables);
  const rawObjectHints = buildObjectGenerationHints(analysis, ctx.tables);
  const objectHints = rawObjectHints.map((candidate, index) => ({
    ...candidate,
    id: `candidate_${index + 1}`,
  }));
  const contextWindow = Number(llm.model?.contextWindow ?? 1_000_000);
  const documentDependencies = ctx.documents.map((document) => ({
    id: document.id, filename: document.filename, fingerprint: documentFingerprint(document),
  }));
  const physicalObjectDependencies = physicalSchemaObjects.map(({ table, object }) => ({
    table: table.name, documentId: table.documentId, objectKey: object.key,
    fields: object.fields.map((field) => ({ key: field.key, type: field.type, sourceColumn: field.sourceColumn })),
  }));
  const objectUnits = planGenerationUnits('object', objectHints, contextWindow).map((unit) => ({
    ...unit,
    dependencyHash: generationDependencyHash({
      unit: unit.dependencyHash,
      project: { domain: project.domain, background: project.background },
      physicalObjects: physicalObjectDependencies,
      documents: documentDependencies,
    }),
  }));
  const rawKnowledgeCandidates = buildKnowledgeGenerationCandidates(analysis, ctx.tables);
  const allKnowledgeCandidates = (analysis?.candidateArtifacts ?? [])
    .filter((item) => item.kind === 'rule' || item.kind === 'metric');
  const knowledgePlan = planArtifactCandidates(allKnowledgeCandidates, rawKnowledgeCandidates);
  const knowledgeCandidates = knowledgePlan.selected;
  const knowledgeUnits = planGenerationUnits('knowledge', knowledgeCandidates, contextWindow).map((unit) => ({
    ...unit,
    dependencyHash: generationDependencyHash({
      unit: unit.dependencyHash,
      objectPlan: objectUnits.map((item) => item.dependencyHash),
      physicalObjects: physicalObjectDependencies,
      confirmedProcessSummary,
      documents: documentDependencies,
    }),
  }));
  const operationCandidates = (analysis?.candidateArtifacts ?? []).filter((item) => item.kind === 'operation');
  const operationUnits = planGenerationUnits('operation', operationTargets, contextWindow).map((unit) => ({
    ...unit,
    dependencyHash: generationDependencyHash({
      unit: unit.dependencyHash,
      objectPlan: objectUnits.map((item) => item.dependencyHash),
      knowledgePlan: knowledgeUnits.map((item) => item.dependencyHash),
      physicalObjects: physicalObjectDependencies,
      confirmedProcessSummary,
    }),
  }));
  const allUnits = [...objectUnits, ...knowledgeUnits, ...operationUnits];
  const dependencyHashes = new Map(allUnits.map((unit) => [unit.phase, unit.dependencyHash]));
  const totalStepCount = allUnits.length + 1;
  const messages: unknown[] = [];
  const generationGate = new AdaptiveGenerationGate(Math.max(
    configuredGenerationConcurrency('object'),
    configuredGenerationConcurrency('knowledge'),
    configuredGenerationConcurrency('operation'),
  ));
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
      protocolVersion: 'knowledge-generation-v3-parallel-units',
      baseRevision: baseRevision.oid,
      dataSourceType,
      project: { name: project.name, domain: project.domain, background: project.background },
      confirmedFlowchart,
      analysis,
      physicalSchemaObjects: physicalSchemaObjects.map(({ table, object }) => ({
        table: table.name, documentId: table.documentId, objectKey: object.key,
      })),
      objectHints,
      knowledgeCandidates,
      knowledgeCandidateDispositions: knowledgePlan.dispositions,
      generationUnits: allUnits.map((unit) => ({
        phase: unit.phase, dependencyHash: unit.dependencyHash, itemCount: unit.items.length,
      })),
      operationTargets,
      tables: ctx.tables,
      documents: ctx.documents.map((document) => ({
        id: document.id,
        filename: document.filename,
        fingerprint: documentFingerprint(document),
      })),
    });
    const checkpointStore = new GenerationCheckpointStore();
    const explicitPriorRunId = resumableRun?.state && typeof resumableRun.state.priorRunId === 'string'
      ? resumableRun.state.priorRunId
      : undefined;
    const priorRunId = explicitPriorRunId
      ?? checkpointStore.latestInterruptedRun(project.id, baseRevision.oid);
    if (priorRunId && priorRunId !== authoringRun.id) {
      checkpointStore.copyToRun({
        projectId: project.id,
        fromRunId: priorRunId,
        toRunId: authoringRun.id,
        planHash,
        dependencyHashes,
      });
    }
    const checkpoints = new Map(
      checkpointStore.list(project.id, authoringRun.id, planHash, dependencyHashes)
        .map((checkpoint) => [checkpoint.phase, checkpoint]),
    );
    for (const checkpoint of checkpoints.values()) {
      if (checkpoint.status === 'skipped') skipFailedPhases.add(checkpoint.phase);
    }
    trace({
      projectId: project.id, nodeId: node.id, phase: 'prepare', kind: 'phase_started', status: 'running',
      title: '准备知识包生成', summary: `业务流程 ${selectedQuestions.length} 个`,
      details: {
        questionCount: selectedQuestions.length,
        schemaManifestCount: schemaManifests.length,
        physicalSchemaObjectCount: physicalSchemaObjects.length,
        objectBatchCount: objectUnits.length,
        knowledgeBatchCount: knowledgeUnits.length,
        operationBatchCount: operationUnits.length,
        objectConcurrency: configuredGenerationConcurrency('object'),
        knowledgeConcurrency: configuredGenerationConcurrency('knowledge'),
        operationConcurrency: configuredGenerationConcurrency('operation'),
        totalStepCount,
        restoredCheckpointCount: checkpoints.size,
      },
    });
    // 生成要跑十几到几十分钟，按全部阶段与批次实时上报。
    const generationStartedAt = Date.now();
    const objects: SpecObject[] = physicalSchemaObjects.map((item) => item.object);
    trace({
      projectId: project.id, nodeId: node.id, phase: 'schema_objects', kind: 'phase_completed', status: 'done',
      title: '物理 Schema 对象已生成',
      summary: `识别目录 ${schemaManifests.length} 张 · 覆盖物理 Sheet ${physicalSchemaObjects.length} 张`,
      details: {
        manifestTables: schemaManifests.map((manifest) => manifest.table.name),
        objectCount: physicalSchemaObjects.length,
      },
    });
    const objectUnitOutputs = await mapWithConcurrency(
      objectUnits,
      configuredGenerationConcurrency('object'),
      async (unit) => {
      const index = unit.index - 1;
      const hints = unit.items;
      throwIfCancelled(project.id, 'knowledgeGenerate');
      reportLongTaskProgress(project.id, {
        taskKinds: ['knowledge_extraction', 'operation_extraction'],
        title: `正在并发生成第 ${index + 1}/${objectUnits.length} 批业务对象`,
        produced: `已确定生成 ${objects.length} 个 Sheet 对象`,
        done: index,
        total: totalStepCount,
        elapsedMs: Date.now() - generationStartedAt,
        authoringRunId: authoringRun.id,
        phase: '知识结构生成',
      });
      const phase = unit.phase;
      const checkpoint = checkpoints.get(phase);
      if (checkpoint?.status === 'completed') {
        const output = checkpoint.output as Extract<GenerationBatchOutput, { objects: SpecObject[] }>;
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${objectUnits.length} 批业务对象已从检查点恢复`,
          summary: `恢复 ${output.objects?.length ?? 0} 个`,
          details: { batch: index + 1, totalBatches: objectUnits.length, resumed: true },
        });
        return output.objects ?? [];
      }
      if (checkpoint?.status === 'skipped' && skipFailedPhases.has(phase)) {
        generationNotices.push(checkpointNotice(checkpoint));
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批对象`,
          summary: `恢复已记录的 ${checkpoint.issues.length} 个未解决问题`,
          details: { batch: index + 1, issues: checkpoint.issues, resumed: true },
        });
        return [];
      }
      if (checkpoint?.status === 'failed' && skipFailedPhases.has(phase)) {
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'object', batchIndex: index + 1, dependencyHash: unit.dependencyHash, issues: checkpoint.issues,
          attemptCount: checkpoint.attemptCount,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push(checkpointNotice(skipped));
        return [];
      }
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成业务对象 ${index + 1}/${objectUnits.length}`,
        summary: `当前批次候选 ${hints.length} 个`,
        details: { batch: index + 1, totalBatches: objectUnits.length, candidateCount: hints.length },
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
          `补充查询边界、别名和有材料依据的对象关系。${RELATION_NAME_GENERATION_GUIDANCE}对象别名在整个知识包内必须唯一：忽略 ASCII 大小写和首尾空格后，不得与其他对象的别名、key 或 title 重复。`,
          '每个对象必须用 candidateId 认领当前批次中的一个候选，候选名称只用于理解，不用于绑定。',
          `当前批次有 ${hints.length} 个逻辑对象候选。所有候选均已通过服务端筛选，必须逐项生成，不得省略。`,
          '用 submit_objects 一次提交当前批次全部对象，最多 12 个。',
          `已确定对象 key：${objects.map((item) => item.key).join(', ') || '（无）'}；当前批次不得重复。`,
          `已确定对象及别名：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title, aliases: item.aliases ?? [] })))}`,
          `当前批次候选：${JSON.stringify(hints)}`,
          `项目背景：${project.background || project.domain || project.name}`,
          ...(attempt > 1 ? [`上次提交不完整：${lastIssues.join('；')}。这次请重新提交整批完整结果，不是只提交缺失项。`] : []),
        ].join('\n');
        const agent = makeAgent(llm, sys, buildObjectGenerateTools(toolCtx(ctx), cap, hints));
        let stageMessages: unknown[];
        try {
          stageMessages = await generationGate.run(() => runAgentCaptured(agent,
            `请完成第 ${index + 1}/${objectUnits.length} 批全部逻辑对象并调用 submit_objects。`,
            () => !!cap.objects, '请立即调用 submit_objects 提交当前批次全部逻辑对象。', observe(phase)));
        } catch (error) {
          const policy = classifyGenerationError(error);
          if (policy.kind === 'rate_limited') generationGate.rateLimited(policy.retryAfterMs);
          lastIssues = [policy.message];
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务对象未完成结构化提交`,
            summary: policy.kind === 'quota_exhausted' ? '模型额度耗尽，暂停重试当前批次' : lastIssues[0],
            details: { attempt, issues: lastIssues, errorClass: policy.kind, retryAfterMs: policy.retryAfterMs },
          });
          if (attempt < 3 && shouldRetryGenerationError(error)) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动补交当前对象批次', details: { attempt: attempt + 1, remainingCount: 1 },
          });
          if (!shouldRetryGenerationError(error) || attempt >= 3) {
            throw generationModelCallError(error);
          }
          await waitBeforeGenerationRetry(error, attempt);
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
          projectId: project.id, runId: authoringRun.id, planHash, failure, dependencyHash: unit.dependencyHash,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, failedCheckpoint);
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'object', batchIndex: index + 1, dependencyHash: unit.dependencyHash, issues: failure.issues,
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
        return [];
      }
      const completedCheckpoint = checkpointStore.saveCompleted({
        projectId: project.id, runId: authoringRun.id, planHash, phase,
        batchKind: 'object', batchIndex: index + 1, dependencyHash: unit.dependencyHash,
        output: { objects: acceptedObjects },
        attemptCount: priorAttemptCount + acceptedAttempt,
      });
      checkpoints.set(phase, completedCheckpoint);
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${objectUnits.length} 批业务对象已生成`,
        summary: `本批 ${acceptedObjects.length} 个`,
        details: { batch: index + 1, totalBatches: objectUnits.length, objectCount: acceptedObjects.length },
      });
      return acceptedObjects;
    });
    for (const output of objectUnitOutputs) appendUnique(objects, output, '对象');

    const rules: SpecRule[] = [];
    const metrics: SpecMetric[] = [];
    const candidateDispositions = [...knowledgePlan.dispositions];
    type KnowledgeUnitOutput = { rules: SpecRule[]; metrics: SpecMetric[]; dispositions: typeof candidateDispositions };
    const knowledgeUnitOutputs = await mapWithConcurrency(
      knowledgeUnits,
      configuredGenerationConcurrency('knowledge'),
      async (unit): Promise<KnowledgeUnitOutput> => {
      const index = unit.index - 1;
      const candidates = unit.items;
      throwIfCancelled(project.id, 'knowledgeGenerate');
      reportLongTaskProgress(project.id, {
        taskKinds: ['knowledge_extraction', 'operation_extraction'],
        title: `正在并发生成第 ${index + 1}/${knowledgeUnits.length} 批规则和指标`,
        produced: `已产出 ${objects.length} 个对象、${rules.length} 条规则、${metrics.length} 个指标`,
        done: objectUnits.length + index,
        total: totalStepCount,
        elapsedMs: Date.now() - generationStartedAt,
        authoringRunId: authoringRun.id,
        phase: '知识结构生成',
      });
      const phase = unit.phase;
      const checkpoint = checkpoints.get(phase);
      if (checkpoint?.status === 'completed') {
        const output = checkpoint.output as Extract<GenerationBatchOutput, { rules: SpecRule[]; metrics: SpecMetric[] }>;
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${knowledgeUnits.length} 批规则和指标已从检查点恢复`,
          summary: `恢复规则 ${output.rules?.length ?? 0} 条、指标 ${output.metrics?.length ?? 0} 个`,
          details: { batch: index + 1, totalBatches: knowledgeUnits.length, resumed: true },
        });
        return { rules: output.rules ?? [], metrics: output.metrics ?? [], dispositions: (output.dispositions ?? []) };
      }
      if (checkpoint?.status === 'skipped' && skipFailedPhases.has(phase)) {
        generationNotices.push(checkpointNotice(checkpoint));
        return { rules: [], metrics: [], dispositions: [] };
      }
      if (checkpoint?.status === 'failed' && skipFailedPhases.has(phase)) {
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'knowledge', batchIndex: index + 1, dependencyHash: unit.dependencyHash, issues: checkpoint.issues,
          attemptCount: checkpoint.attemptCount,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push(checkpointNotice(skipped));
        return { rules: [], metrics: [], dispositions: [] };
      }
      const suggestedRules = candidates.filter((item) => item.kind === 'rule' && item.readiness === 'extractable').length;
      const suggestedMetrics = candidates.filter((item) => item.kind === 'metric' && item.readiness === 'extractable').length;
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成规则和指标 ${index + 1}/${knowledgeUnits.length}`,
        summary: `分析建议规则 ${suggestedRules} 条 · 指标 ${suggestedMetrics} 个`,
        details: { batch: index + 1, totalBatches: knowledgeUnits.length, ruleCount: suggestedRules, metricCount: suggestedMetrics },
      });
      let acceptedRules: SpecRule[] | undefined;
      let acceptedMetrics: SpecMetric[] | undefined;
      let acceptedDispositions: typeof candidateDispositions | undefined;
      let acceptedAttempt = 0;
      const priorAttemptCount = checkpoint?.status === 'failed' ? checkpoint.attemptCount : 0;
      let lastCounts = { rules: 0, metrics: 0 };
      let lastIssues: string[] = [];
      const requiresDispositions = candidates.length > 0;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const cap: KnowledgeSpecCapture = {};
        const sys = [
          '你是知识包规则与指标提取助手。本阶段只生成 rule 和 metric，不生成 operation。',
          '适用条件、判断门槛、例外和产品匹配条件沉淀为 rule；明确公式、统计粒度和边界的口径沉淀为 metric。',
          '必须能在材料中找到依据；缺少阈值、公式或权威映射时不要猜测，返回空数组并留给访谈澄清。',
          'readiness=needs_clarification 的候选只有在访谈结论已明确补齐条件或口径时才能生成；否则不产出。',
          `本批有 ${suggestedRules} 条 rule 候选、${suggestedMetrics} 条 metric 候选。与当前确认流程及 Excel 对象直接相关的 extractable 候选必须生成或合并；确实不相关的候选才可 deferred/excluded，并要指出它与哪个流程能力或对象边界不匹配，不能使用“留待后续”等泛化理由。needs_clarification 候选不得创建空资源。每个候选必须提交处置记录，不能静默省略。`,
          '只能提交当前批次候选能够证明的资源；不得凭流程描述另造规则或指标。一个候选允许拆成多个互补指标，但每个资源都必须被 generated 处置记录认领。',
          '生成 rule 时必须填写 appliesToObjects、完整 content 和至少一条 conditions；生成 metric 时必须填写 usesObjects、unit、grain、含公式的 caliber、boundaries 和 nullPolicy。无法绑定当前对象或缺少口径时应具体说明并延期，不得生成空壳。',
          'rule/metric 引用的对象只能使用下列对象 key。用 submit_rules_metrics 提交资源和全部 dispositions，单类最多 12 条。',
          `对象：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title })))}`,
          '各批次会在完成后执行全局 key 冲突校验；当前批次内部 key 必须唯一。',
          `已确认流程摘要：${confirmedProcessSummary.slice(0, 2400)}`,
          `当前批次材料候选：${artifactSummary(candidates)}`,
        ].join('\n');
        const agent = makeAgent(llm, sys, buildKnowledgeGenerateTools(toolCtx(ctx), cap));
        const retryHint = attempt === 1
          ? ''
          : `上次提交存在以下问题：${lastIssues.join('；') || '未完成结构化提交'}。请重新提交当前批次的完整结果。`;
        let stageMessages: unknown[];
        try {
          stageMessages = await generationGate.run(() => runAgentCaptured(agent,
            `请完成第 ${index + 1}/${knowledgeUnits.length} 批规则和指标提取并调用 submit_rules_metrics。${retryHint}`,
            () => !!cap.rules && !!cap.metrics && (!requiresDispositions || !!cap.dispositions),
            '请立即调用 submit_rules_metrics，并为当前批次每个候选提交 disposition。', observe(phase)));
        } catch (error) {
          const policy = classifyGenerationError(error);
          if (policy.kind === 'rate_limited') generationGate.rateLimited(policy.retryAfterMs);
          const issue = policy.message;
          lastIssues = [issue];
          lastCounts = { rules: 0, metrics: 0 };
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批规则/指标未完成结构化提交`,
            summary: policy.kind === 'quota_exhausted' ? '模型额度耗尽，暂停重试当前批次' : issue,
            details: { attempt, issues: [issue], errorClass: policy.kind, retryAfterMs: policy.retryAfterMs },
          });
          if (attempt < 3 && shouldRetryGenerationError(error)) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动重试当前规则/指标批次', details: { attempt: attempt + 1 },
          });
          if (!shouldRetryGenerationError(error) || attempt >= 3) {
            throw generationModelCallError(error);
          }
          await waitBeforeGenerationRetry(error, attempt);
          continue;
        }
        messages.push(...stageMessages);
        lastCounts = { rules: cap.rules?.length ?? 0, metrics: cap.metrics?.length ?? 0 };
        if (!cap.rules || !cap.metrics || (requiresDispositions && !cap.dispositions)) {
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
        const submittedRules = cap.rules as SpecRule[];
        const submittedMetrics = cap.metrics as SpecMetric[];
        const submittedDispositions = (cap.dispositions ?? []) as typeof candidateDispositions;
        const validated = validateKnowledgeBatch(
          { rules: submittedRules, metrics: submittedMetrics },
          { objects, rules: [], metrics: [] },
        );
        lastIssues = [
          ...validated.issues,
          ...validateKnowledgeCandidateDispositions(candidates, submittedRules, submittedMetrics, submittedDispositions),
          ...validateGeneratedKnowledgeResources(submittedRules, submittedMetrics, submittedDispositions),
        ];
        if (lastIssues.length) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批规则/指标引用校验未通过`,
            summary: `发现 ${lastIssues.length} 个待修正问题`,
            details: { attempt, issues: lastIssues, invalidReferenceCount: validated.invalidReferenceCount },
          });
          if (attempt < 3) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动重试当前规则/指标批次', details: { attempt: attempt + 1, remainingCount: lastIssues.length },
          });
          continue;
        }
        acceptedRules = submittedRules;
        acceptedMetrics = submittedMetrics;
        acceptedDispositions = submittedDispositions;
        acceptedAttempt = attempt;
        if (attempt > 1) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_completed', status: 'done',
            title: '规则/指标自动重试完成', details: { attempt, remainingCount: 0 },
          });
        }
        break;
      }
      if (!acceptedRules || !acceptedMetrics || !acceptedDispositions) {
        const failure = {
          phase, batchKind: 'knowledge' as const, batchIndex: index + 1,
          title: `第 ${index + 1} 批规则/指标连续两次自动补交后仍未通过`,
          issues: lastIssues.length
            ? lastIssues
            : [`实际提交规则 ${lastCounts.rules} 条、指标 ${lastCounts.metrics} 条`],
        };
        const failedCheckpoint = checkpointStore.saveFailed({
          projectId: project.id, runId: authoringRun.id, planHash, failure, dependencyHash: unit.dependencyHash,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, failedCheckpoint);
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'knowledge', batchIndex: index + 1, dependencyHash: unit.dependencyHash, issues: failure.issues,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push({ ...failure, decision: 'expert_skipped' });
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批规则/指标`,
          summary: '继续生成其余知识', details: { batch: index + 1, issues: failure.issues },
        });
        return { rules: [], metrics: [], dispositions: [] };
      }
      const completedCheckpoint = checkpointStore.saveCompleted({
        projectId: project.id, runId: authoringRun.id, planHash, phase,
        batchKind: 'knowledge', batchIndex: index + 1, dependencyHash: unit.dependencyHash,
        output: { rules: acceptedRules, metrics: acceptedMetrics, dispositions: acceptedDispositions },
        attemptCount: priorAttemptCount + acceptedAttempt,
      });
      checkpoints.set(phase, completedCheckpoint);
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${knowledgeUnits.length} 批规则和指标已生成`,
        summary: `本批规则 ${acceptedRules.length} 条 · 指标 ${acceptedMetrics.length} 个`,
        details: { batch: index + 1, totalBatches: knowledgeUnits.length, ruleCount: acceptedRules.length, metricCount: acceptedMetrics.length },
      });
      return { rules: acceptedRules, metrics: acceptedMetrics, dispositions: acceptedDispositions };
    });
    const knowledgeOutputIssues = validateKnowledgeOutputs(knowledgeUnitOutputs, objects);
    if (knowledgeOutputIssues.length) throw new Error(`规则/指标并发归并冲突：${knowledgeOutputIssues.join('；')}`);
    for (const output of knowledgeUnitOutputs) {
      appendUnique(rules, output.rules, '规则');
      appendUnique(metrics, output.metrics, '指标');
      candidateDispositions.push(...output.dispositions);
    }

    const operations: SpecOperation[] = [];
    const operationUnitOutputs = await mapWithConcurrency(
      operationUnits,
      configuredGenerationConcurrency('operation'),
      async (unit): Promise<SpecOperation[]> => {
      const index = unit.index - 1;
      const questions = unit.items;
      throwIfCancelled(project.id, 'knowledgeGenerate');
      reportLongTaskProgress(project.id, {
        taskKinds: ['knowledge_extraction', 'operation_extraction'],
        title: `正在并发生成第 ${index + 1}/${operationUnits.length} 批业务操作`,
        produced: `已产出 ${objects.length} 个对象、${rules.length} 条规则、${metrics.length} 个指标、${operations.length} 个操作`,
        done: objectUnits.length + knowledgeUnits.length + index,
        total: totalStepCount,
        elapsedMs: Date.now() - generationStartedAt,
        authoringRunId: authoringRun.id,
        phase: '知识结构生成',
      });
      const phase = unit.phase;
      const checkpoint = checkpoints.get(phase);
      if (checkpoint?.status === 'completed') {
        const output = checkpoint.output as Extract<GenerationBatchOutput, { operations: SpecOperation[] }>;
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${operationUnits.length} 批业务操作已从检查点恢复`,
          summary: `恢复 ${output.operations?.length ?? 0} 个`,
          details: { batch: index + 1, totalBatches: operationUnits.length, resumed: true },
        });
        return output.operations ?? [];
      }
      if (checkpoint?.status === 'skipped' && skipFailedPhases.has(phase)) {
        generationNotices.push(checkpointNotice(checkpoint));
        return [];
      }
      if (checkpoint?.status === 'failed' && skipFailedPhases.has(phase)) {
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'operation', batchIndex: index + 1, dependencyHash: unit.dependencyHash, issues: checkpoint.issues,
          attemptCount: checkpoint.attemptCount,
        });
        checkpoints.set(phase, skipped);
        generationNotices.push(checkpointNotice(skipped));
        return [];
      }
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成业务操作 ${index + 1}/${operationUnits.length}`,
        summary: `覆盖本批 ${questions.length} 个业务流程`,
        details: { batch: index + 1, totalBatches: operationUnits.length, questionCount: questions.length },
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
          '本阶段不得声明 script，也不得在 flow 中引用 script:<key>；确定性脚本会在知识结构确认后单独识别、确认和生成。',
          '需要判断条件或口径时优先引用已有 rule/metric，所有 relatedObjects、usesRules 和 usesMetrics 只能使用下方明确列出的 key。',
          '不得引用未声明的 key，也不得把缺少材料依据的阈值写进流程。',
          '用 submit_operations 一次提交当前批次全部结果，最多 3 个 operation。',
          '各批次会在完成后执行全局 operation key 冲突校验；当前批次内部 key 必须唯一。',
          `对象摘要：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title, sourceRefs: item.sourceRefs, sourceTable: item.sourceTable, fields: item.fields.map((field) => field.key) })))}`,
          `可用规则：${JSON.stringify(rules.map((item) => ({ key: item.key, title: item.title })))}`,
          `可用指标：${JSON.stringify(metrics.map((item) => ({ key: item.key, title: item.title })))}`,
          `操作候选参考：${artifactSummary(operationCandidates)}`,
          ...(questions.length === 1 && questions[0].focusNodeLabel
            ? [`当前 operation 只负责流程步骤「${questions[0].focusNodeLabel}」，不要把整张流程图重复塞进一个 operation。`]
            : []),
          `已确认流程摘要：${confirmedProcessSummary.slice(0, 2400)}`,
          `本批已确认流程：\n${questions.map((target, i) => [
            `${i + 1}. [${target.id}] ${target.question}`,
            JSON.stringify({
              title: target.diagram.title,
              targetRole: target.focusNodeId ? 'reusable_step' : 'process_orchestrator',
              focusNode: target.focusNodeId
                ? { id: target.focusNodeId, label: target.focusNodeLabel }
                : undefined,
              requiresRule: target.requiresRule,
              requiresMetric: target.requiresMetric,
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
          stageMessages = await generationGate.run(() => runAgentCaptured(agent,
            `请完成第 ${index + 1}/${operationUnits.length} 批有依据的 operation 并调用 submit_operations。`,
            () => !!cap.operations, '请立即调用 submit_operations 提交当前有依据的操作；没有可靠结果时提交空数组。', observe(phase)));
        } catch (error) {
          const policy = classifyGenerationError(error);
          if (policy.kind === 'rate_limited') generationGate.rateLimited(policy.retryAfterMs);
          const issue = policy.message;
          lastValidation = { issues: [issue], uncoveredQuestions: [], invalidReferenceCount: 0 };
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务操作未完成结构化提交`,
            summary: policy.kind === 'quota_exhausted' ? '模型额度耗尽，暂停重试当前批次' : issue,
            details: { attempt, issues: [issue], errorClass: policy.kind, retryAfterMs: policy.retryAfterMs },
          });
          if (attempt < 3 && shouldRetryGenerationError(error)) trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
            title: '自动补交当前业务操作批次', details: { attempt: attempt + 1, remainingCount: 1 },
          });
          if (!shouldRetryGenerationError(error) || attempt >= 3) {
            throw generationModelCallError(error);
          }
          await waitBeforeGenerationRetry(error, attempt);
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
          projectId: project.id, runId: authoringRun.id, planHash, failure, dependencyHash: unit.dependencyHash,
          attemptCount: priorAttemptCount + 3,
        });
        checkpoints.set(phase, failedCheckpoint);
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        const skipped = checkpointStore.markSkipped({
          projectId: project.id, runId: authoringRun.id, planHash, phase,
          batchKind: 'operation', batchIndex: index + 1, dependencyHash: unit.dependencyHash, issues: failure.issues,
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
        return [];
      }
      const completedCheckpoint = checkpointStore.saveCompleted({
        projectId: project.id, runId: authoringRun.id, planHash, phase,
        batchKind: 'operation', batchIndex: index + 1, dependencyHash: unit.dependencyHash,
        output: { operations: acceptedOperations },
        attemptCount: priorAttemptCount + acceptedAttempt,
      });
      checkpoints.set(phase, completedCheckpoint);
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${operationUnits.length} 批业务操作已生成`,
        summary: `本批 ${acceptedOperations.length} 个`,
        details: { batch: index + 1, totalBatches: operationUnits.length, operationCount: acceptedOperations.length },
      });
      return acceptedOperations;
    });
    const operationOutputIssues = validateOperationOutputs(operationUnitOutputs.flat());
    if (operationOutputIssues.length) throw new Error(`业务操作并发归并冲突：${operationOutputIssues.join('；')}`);
    for (const output of operationUnitOutputs) appendUnique(operations, output, '操作');

    reportLongTaskProgress(project.id, {
      taskKinds: ['knowledge_extraction', 'operation_extraction'],
      title: '正在校验并保存知识结构',
      produced: `对象 ${objects.length} · 规则 ${rules.length} · 指标 ${metrics.length} · 操作 ${operations.length}`,
      done: totalStepCount - 1,
      total: totalStepCount,
      elapsedMs: Date.now() - generationStartedAt,
      authoringRunId: authoringRun.id,
      phase: '知识结构校验',
    });

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
      candidateDispositions: [
        ...candidateDispositions,
        ...dispositionForObjectAndOperationCandidates(
          analysis?.candidateArtifacts ?? [], objects, operations,
        ),
      ],
      dataSourceType,
    }, analysis, confirmedFlowchart.diagrams, {
      documents: ctx.documents
        .filter((document): document is typeof document & { id: string } => Boolean(document.id))
        .map((document) => ({ id: document.id, filename: document.filename })),
      tables: ctx.tables.map((table) => ({ name: table.name, documentId: table.documentId })),
    });
    const governedSpec = enforceObjectSourcePolicy(rawSpec, ctx.tables);
    const spec = normalizeObjectDataMappings(governedSpec, schemaOnlyTables);
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
        .flatMap((notice) => knowledgeUnits[notice.batchIndex - 1]?.items ?? []),
    );
    const skippedQuestionIds = new Set(
      generationNotices
        .filter((notice) => notice.batchKind === 'operation')
        .flatMap((notice) => operationUnits[notice.batchIndex - 1]?.items ?? [])
        .map((question) => question.id),
    );
    const validationAnalysis: AnalysisResult | undefined = analysis && skippedKnowledgeCandidates.size
      ? {
        ...analysis,
        candidateArtifacts: (analysis.candidateArtifacts ?? [])
          .filter((candidate) => !knowledgePlan.selected.some((planned) => (
            skippedKnowledgeCandidates.has(planned)
            && planned.kind === candidate.kind
            && planned.name === candidate.name
            && planned.sources.join('\u0000') === candidate.sources.join('\u0000')
          ))),
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
    const objectSourceIssues = schemaObjectPolicyIssues(spec, files, ctx.tables);
    if (objectSourceIssues.length) {
      throw new Error(`对象来源策略未通过：${objectSourceIssues.map((issue) => issue.message).join('；')}`);
    }
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
      phase: '结果写入',
      currentStep: '保存知识包版本',
      progress: 95,
      retryCount: 0,
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
  } finally {
    stopJobHeartbeat();
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
