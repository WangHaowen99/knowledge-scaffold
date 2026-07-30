import type {
  AnalysisResult, ArtifactCandidate, CoverageResult, DataDictTable, InterviewBatch,
  PackageDataSourceType, PackageSpec, Project, SpecMetric, SpecObject, SpecOperation, SpecRule,
} from '../types.js';
import { buildLlm, buildTaskLlm, type LlmDeps } from '../llm/provider.js';
import {
  buildCompetencyTools, buildInterviewTools, buildKnowledgeGenerateTools,
  buildObjectGenerateTools, buildOperationGenerateTools,
  type CompetencyCapture, type InterviewCapture, type KnowledgeSpecCapture,
  type ObjectSpecCapture, type OperationSpecCapture, type ToolContext,
} from './tools.js';
import { makeAgent, runAgent, runAgentCaptured } from './agent.js';
import { upsertNode, getNode, nextOrdinal, listNodes } from './engine.js';
import { normalizeKey } from './keys.js';
import * as repo from './m2-repo.js';
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

type ConfirmedDiagram = FlowchartOutput['diagrams'][number];

export interface PackageGenerationOptions extends GenerationOptions {
  dataSourceType?: PackageDataSourceType;
}

type Ctx = {
  tables: DataDictTable[];
  documents: { id?: string; filename: string; textContent?: string }[];
};

function toolCtx(ctx: Ctx, captured: Record<string, unknown> = {}): ToolContext {
  return { tables: ctx.tables, documents: ctx.documents, captured };
}

function qid(batch: unknown[], i: number): unknown {
  const q = batch[i] as Record<string, unknown>;
  return { id: `q${i}`, ...q };
}

// ---- 节点 4 访谈：启动（产出第一批题，挂起） ----
export async function startInterview(project: Project, ctx: Ctx, analysis: unknown, llmOverride?: LlmDeps): Promise<{ nodeId: string; batch: InterviewBatch | null; finished: boolean }> {
  const llm = llmOverride ?? buildLlm();
  const cap: InterviewCapture = {};
  const tools = buildInterviewTools(toolCtx(ctx), cap);
  const sys = [
    '你是业务流程建模访谈助手。围绕业务流程提问，不要把访谈重心放在对象、字段或对象关系上。',
    '依次澄清：触发条件、参与角色、输入、操作步骤、判断分支、异常处理、输出结果、跨角色交接。',
    '每次用 ask_user 提交一批（1~5 道）选择题（single/multi/text，均可含"其他"由前端补充）。专家答复后你再决定继续追问或 finish_interview。',
    '控制专家负担：整场访谈一般 2~3 批内结束；只有仍存在影响建模的关键分歧时才追问，勿为完备性重复确认。',
    `项目背景：${project.background || project.domain}`,
    `初步分析：${JSON.stringify(stripQuality(analysis)).slice(0, 2000)}`,
    ...gapHints(analysis),
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'interview', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgentCaptured(agent, '请基于资料开始业务流程访谈，用 ask_user 提交第一批流程题。',
    () => !!(cap.finished || cap.pendingBatch),
    '你刚才把题目写成了正文。请立即调用 ask_user 工具提交这批题（结构化），不要以正文形式输出；若确无需访谈请调用 finish_interview。');

  return finalizeInterviewStep(project.id, node.id, cap, messages);
}

// ---- 节点 4 访谈：专家应答 → 恢复继续 ----
export async function answerInterview(project: Project, ctx: Ctx, batchId: string, answers: any[], llmOverride?: LlmDeps): Promise<{ nodeId: string; batch: InterviewBatch | null; finished: boolean }> {
  repo.saveAnswers(batchId, answers);
  // 取最新访谈节点（访谈可能被重开，旧节点残留）
  const node = [...listNodes(project.id)].reverse().find((n) => n.type === 'interview');
  if (!node) throw new Error('访谈节点不存在');

  const llm = llmOverride ?? buildLlm();
  const cap: InterviewCapture = {};
  const tools = buildInterviewTools(toolCtx(ctx), cap);
  const sys = [
    '你是业务流程建模访谈助手。继续澄清触发、角色、输入、步骤、判断、异常、输出和交接。不要转向对象或关系盘点。',
    '控制专家负担：整场访谈一般 2~3 批内结束；流程已经能够完整绘制时就用 finish_interview 提交结构化流程。',
    `项目背景：${project.background || project.domain}`,
  ].join('\n');

  // 从快照恢复
  const agent = makeAgent(llm, sys, tools, (node.agentMessages as unknown[]) ?? []);
  const batch = repo.getBatch(batchId);
  const answerText = summarizeAnswers(batch, answers);
  const messages = await runAgentCaptured(agent, `专家对上一批题的答复：\n${answerText}`,
    () => !!(cap.finished || cap.pendingBatch),
    '请调用 ask_user 工具提交下一批题，或调用 finish_interview 结束访谈——不要以正文形式输出题目。');

  return finalizeInterviewStep(project.id, node.id, cap, messages);
}

function finalizeInterviewStep(projectId: string, nodeId: string, cap: InterviewCapture, messages: unknown[]) {
  if (cap.finished) {
    if (!cap.outcome?.processes.length) throw new Error('访谈缺少可绘制的业务流程，请继续澄清');
    upsertNode({
      id: nodeId, projectId, type: 'interview', status: 'done', ordinal: getNode(nodeId)!.ordinal,
      agentMessages: messages, output: cap.outcome,
    });
    return { nodeId, batch: null, finished: true };
  }
  if (cap.pendingBatch) {
    const qs = cap.pendingBatch.questions.map((_, i) => qid(cap.pendingBatch!.questions, i));
    const batch = repo.insertBatch(projectId, nodeId, qs);
    upsertNode({ id: nodeId, projectId, type: 'interview', status: 'waiting_user', ordinal: getNode(nodeId)!.ordinal, agentMessages: messages });
    return { nodeId, batch, finished: false };
  }
  // 没有 finish_interview 的结构化流程就不能完成访谈。否则时间线看似完成，
  // 下一步却没有可绘制的流程图，错误会被拖到更晚才暴露。
  const hadInterview = repo.listBatches(projectId, nodeId).length > 0;
  const reason = hadInterview
    ? '访谈 agent 未提交可绘制的业务流程，请重试本轮访谈'
    : '访谈 agent 未按协议出题（已轻推仍失败），请重试';
  upsertNode({
    id: nodeId,
    projectId,
    type: 'interview',
    status: hadInterview ? 'failed' : 'pending',
    ordinal: getNode(nodeId)!.ordinal,
    agentMessages: messages,
    reason,
  });
  throw new Error(reason);
}

// 资料质量评估的缺口 → 访谈优先澄清点（分析摘要剔除 quality 防挤占篇幅）
function stripQuality(analysis: unknown): unknown {
  if (!analysis || typeof analysis !== 'object') return analysis;
  const { quality, ...rest } = analysis as Record<string, unknown>;
  return rest;
}
function gapHints(analysis: unknown): string[] {
  const gaps = (analysis as any)?.quality?.gaps;
  if (!Array.isArray(gaps) || !gaps.length) return [];
  const lines = gaps.slice(0, 6).map((g: any) => `${g.kind}·${g.target}：${g.message}`).join('；');
  return [`资料缺口（访谈应优先澄清）：${lines}`.slice(0, 900)];
}

function summarizeAnswers(batch: InterviewBatch | null, answers: any[]): string {
  if (!batch) return JSON.stringify(answers);
  return batch.questions.map((q) => {
    const a = answers.find((x) => x.questionId === q.id);
    if (!a || a.skipped) return `- ${q.q}：（跳过）`;
    const parts = [...(a.selected ?? []), a.other].filter(Boolean);
    return `- ${q.q}：${parts.join('；')}`;
  }).join('\n');
}

// ---- 节点 5 胜任力：生成候选 ----
export async function proposeCompetency(project: Project, ctx: Ctx, analysis: unknown, llmOverride?: LlmDeps) {
  const llm = llmOverride ?? buildLlm();
  const cap: CompetencyCapture = {};
  const tools = buildCompetencyTools(toolCtx(ctx), cap);
  const sys = [
    '你是知识建模助手。基于数据字典、领域分析与已有访谈结论，提出 10~20 条"该知识包建成后必须能回答的业务问题"（领域问题）。',
    '问题应具体、可被数据回答、覆盖核心业务场景。用 propose_competency_questions 提交。',
    `初步分析：${JSON.stringify(analysis).slice(0, 1500)}`,
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'competency', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgentCaptured(agent, '请提出候选胜任力问题并调用 propose_competency_questions。',
    () => !!cap.questions, '请调用 propose_competency_questions 工具提交候选问题（不要以正文形式输出）。');

  const questions = cap.questions ?? [];
  repo.replaceCompetency(project.id, questions);
  upsertNode({ id: node.id, projectId: project.id, type: 'competency', status: questions.length ? 'done' : 'running', ordinal: node.ordinal, agentMessages: messages });
  return repo.listCompetency(project.id);
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

// ---- 节点 6 包生成：对象 → 规则/指标 → operation 分阶段生成，最后统一校验与渲染 ----
export async function generatePackage(
  project: Project,
  ctx: Ctx,
  llmOverride?: LlmDeps,
  confirmedFlowchartOrDataSourceType?: FlowchartOutput | PackageDataSourceType,
  options: PackageGenerationOptions = {},
): Promise<{
  spec: PackageSpec;
  coverage: CoverageResult;
  files: Record<string, string>;
  version: string;
  revisionId: string;
  generationNotices: GenerationNotice[];
}> {
  const confirmedFlowchart = typeof confirmedFlowchartOrDataSourceType === 'string'
    ? undefined
    : confirmedFlowchartOrDataSourceType;
  const dataSourceType = typeof confirmedFlowchartOrDataSourceType === 'string'
    ? confirmedFlowchartOrDataSourceType
    : options.dataSourceType ?? 'neo4j';
  const artifactStore = getPackageArtifactStore();
  const baseRevision = await artifactStore.ensureProject(project.id);
  const selectedCompetency = repo.listCompetency(project.id).filter((question) => question.selected);
  type OperationTarget = {
    id: string;
    question: string;
    diagram?: ConfirmedDiagram;
  };
  const flowchartTargets: OperationTarget[] = (confirmedFlowchart?.diagrams ?? []).map((diagram, index) => ({
    id: `process_${diagram.id || index + 1}`,
    question: `按照已确认的「${diagram.title}」流程完成业务处理并返回明确结果`,
    diagram,
  }));
  const operationTargets: OperationTarget[] = flowchartTargets.length
    ? flowchartTargets
    : selectedCompetency.map(({ id, question }) => ({ id, question, diagram: undefined }));
  const selectedQuestions = operationTargets.map((target) => target.question);
  if (!operationTargets.length) throw new Error('缺少人工确认的业务流程，无法生成业务操作');
  const interviewNode = [...listNodes(project.id)].reverse().find((node) => node.type === 'interview');
  const batches = interviewNode?.status === 'done' ? repo.listBatches(project.id, interviewNode.id) : [];
  const batchInterviewSummary = batches.map((b) => summarizeAnswers(b, repo.getAnswers(b.id))).join('\n');
  const interviewSummary = batchInterviewSummary || (interviewNode?.output
    ? JSON.stringify(interviewNode.output)
    : '');
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
      }).run
    : authoring.start(project.id, '生成知识包', baseRevision.oid, { nodeId: node.id }).run;
  const rawObjectHints = analysis?.candidateObjects?.length
    ? analysis.candidateObjects
    : ctx.tables.map((table) => ({ name: table.name, rationale: `来自数据表 ${table.name}`, sourceTables: [table.name] }));
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
  trace({
    projectId: project.id, nodeId: node.id, phase: 'prepare', kind: 'phase_started', status: 'running',
    title: '准备知识包生成', summary: `业务流程 ${selectedQuestions.length} 个 · 访谈 ${batches.length} 批`,
    details: {
      questionCount: selectedQuestions.length, batch: batches.length,
      objectBatchCount: objectBatches.length,
      knowledgeBatchCount: knowledgeBatches.length,
      operationBatchCount: questionBatches.length,
      totalStepCount,
    },
  });
  try {
    const objects: SpecObject[] = [];
    for (const [index, hints] of objectBatches.entries()) {
      const phase = `object_batch_${index + 1}`;
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成业务对象 ${index + 1}/${objectBatches.length}`,
        summary: `当前批次候选 ${hints.length} 个`,
        details: { batch: index + 1, totalBatches: objectBatches.length, candidateCount: hints.length },
      });
      let acceptedObjects: SpecObject[] | undefined;
      let lastIssues: string[] = [];
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const cap: ObjectSpecCapture = {};
        const sys = [
          '你是知识包对象建模助手。本阶段只生成业务对象，不生成规则、指标或操作。',
          '有资料来源时应给出 sourceRefs：数据对象引用真实 Sheet 和字段；产品、场景、需求等语义对象引用原始文档及章节或偏移。外部输入可以暂不绑定来源。',
          '数据对象还要给出字段中文名、类型、真实 sourceColumn 和主键；不得虚构数据字典中不存在的字段。',
          '补充查询边界、别名和有材料依据的对象关系。',
          '每个对象必须用 candidateId 认领当前批次中的一个候选，候选名称只用于理解，不用于绑定。',
          `当前批次有 ${hints.length} 个分析候选。候选是建模建议；没有充分证据、不适合当前版本或应由专家补充的候选可以省略。`,
          '用 submit_objects 一次提交当前批次中有依据的对象，最多 4 个。',
          `已生成对象 key：${objects.map((item) => item.key).join(', ') || '（无）'}；当前批次不得重复。`,
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
        const normalizedObjects = normalizeObjectDataMappingsForObjects(submitted.value, ctx.tables);
        lastIssues = [
          ...submitted.problems.map((problem) => problem.message),
          ...findObjectDataMappingProblemsForObjects(normalizedObjects, ctx.tables)
            .map((problem) => problem.message),
          ...validateObjectBatch(hints, normalizedObjects),
        ];
        if (lastIssues.length) {
          trace({
            projectId: project.id, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
            title: `第 ${index + 1} 批业务对象提交不完整`,
            summary: `缺少 ${lastIssues.length} 个候选对象或存在结构映射问题`,
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
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
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
      const phase = `knowledge_batch_${index + 1}`;
      const suggestedRules = candidates.filter((item) => item.kind === 'rule' && item.readiness === 'extractable').length;
      const suggestedMetrics = candidates.filter((item) => item.kind === 'metric' && item.readiness === 'extractable').length;
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成规则和指标 ${index + 1}/${knowledgeBatches.length}`,
        summary: `分析建议规则 ${suggestedRules} 条 · 指标 ${suggestedMetrics} 个`,
        details: { batch: index + 1, totalBatches: knowledgeBatches.length, ruleCount: suggestedRules, metricCount: suggestedMetrics },
      });
      let accepted = false;
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
          `访谈结论：${interviewSummary.slice(0, 2400) || '（无）'}`,
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
        appendUnique(rules, cap.rules as SpecRule[], '规则');
        appendUnique(metrics, cap.metrics as SpecMetric[], '指标');
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
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
        generationNotices.push({ ...failure, decision: 'expert_skipped' });
        trace({
          projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'warning',
          title: `专家确认跳过第 ${index + 1} 批规则/指标`,
          summary: '继续生成其余知识', details: { batch: index + 1, issues: failure.issues },
        });
        continue;
      }
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
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `生成业务操作 ${index + 1}/${questionBatches.length}`,
        summary: `覆盖本批 ${questions.length} 个业务流程`,
        details: { batch: index + 1, totalBatches: questionBatches.length, questionCount: questions.length },
      });
      let acceptedOperations: SpecOperation[] | undefined;
      let lastValidation = validateOperationBatch(questions, [], { objects, rules, metrics });
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        const cap: OperationSpecCapture = {};
        const sys = [
          '你是知识包操作建模助手。本阶段只生成 operation。',
          '当前批次的每个已确认流程都必须生成一个 operation，只填写给定的 competencyQuestionId；目标描述由服务端回填。',
          'operation 给出输入、输出、逐步流程和关联对象；流程引用对象写 object:<key>。',
          '需要判断条件或口径时优先引用已有 rule/metric，所有 relatedObjects、usesRules 和 usesMetrics 只能使用下方明确列出的 key。',
          '不得引用未声明的 key，也不得把缺少材料依据的阈值写进流程。',
          '用 submit_operations 一次提交当前批次全部结果，最多 4 个 operation。',
          `已生成 operation key：${operations.map((item) => item.key).join(', ') || '（无）'}；当前批次不得重复。`,
          `对象摘要：${JSON.stringify(objects.map((item) => ({ key: item.key, title: item.title, sourceRefs: item.sourceRefs, sourceTable: item.sourceTable, fields: item.fields.map((field) => field.key) })))}`,
          `可用规则：${JSON.stringify(rules.map((item) => ({ key: item.key, title: item.title })))}`,
          `可用指标：${JSON.stringify(metrics.map((item) => ({ key: item.key, title: item.title })))}`,
          `操作候选参考：${artifactSummary(operationCandidates)}`,
          `访谈结论：${interviewSummary.slice(0, 2400)}`,
          `本批已确认流程：\n${questions.map((target, i) => [
            `${i + 1}. [${target.id}] ${target.question}`,
            target.diagram ? JSON.stringify({
              title: target.diagram.title,
              ...confirmedDiagramMeta(target.diagram),
              nodes: target.diagram.nodes.map((item) => ({
                id: item.id, label: item.label, kind: item.kind, detail: item.detail,
              })),
              edges: target.diagram.edges,
            }) : '',
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
        if (!skipFailedPhases.has(phase)) throw new GenerationGateDecisionRequired(failure);
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
      trace({
        projectId: project.id, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${questionBatches.length} 批业务操作已生成`,
        summary: `本批 ${acceptedOperations.length} 个 · 累计 ${operations.length} 个`,
        details: { batch: index + 1, totalBatches: questionBatches.length, operationCount: acceptedOperations.length },
      });
    }

    const versions = repo.listPackageVersions(project.id);
    const version = `1.${versions.length}.0`;
    const rawSpec = attachGeneratedKnowledgeEvidence({
      id: `${slug(project.name)}-agent`,
      name: project.name,
      description: project.background || project.domain || project.name,
      version,
      language: 'zh-CN',
      defaultProviderAlias: slug(project.domain || 'data'),
      objects, operations, rules, metrics,
      dataSourceType,
    }, analysis, confirmedFlowchart?.diagrams ?? [], {
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

function confirmedDiagramMeta(diagram: ConfirmedDiagram): {
  trigger?: string;
  actors?: string[];
  inputs?: string[];
  exceptions?: string[];
  outputs?: string[];
  handoffs?: string[];
} {
  return {
    trigger: diagram.trigger,
    actors: diagram.actors,
    inputs: diagram.inputs,
    exceptions: diagram.exceptions,
    outputs: diagram.outputs,
    handoffs: diagram.handoffs,
  };
}

export function checkCoverage(spec: PackageSpec, selected: string[]): CoverageResult {
  return coverageFor(spec, selected);
}

function slug(s: string): string {
  // consumer 的 ontology id 仅允许字母数字下划线短横线且字母/数字开头——中文必须剔除
  return (s || 'pkg').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'pkg';
}

export { repo as m2repo };
