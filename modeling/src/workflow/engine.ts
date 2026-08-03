import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import { throwIfCancelled } from '../cancellation.js';
import {
  batchCacheKey, briefFingerprint, readCachedBatchAnalysis, writeCachedBatchAnalysis,
} from './material-analysis-cache.js';
import type { NodeStatus, NodeType, WorkflowNode, DataDictTable, AnalysisResult } from '../types.js';
import { buildTaskLlm, type LlmDeps } from '../llm/provider.js';
import { buildArtifactAnalysisTools, buildTools, type ToolContext } from './tools.js';
import { makeAgent, runAgent, runAgentCaptured } from './agent.js';
import { appendTraceEvent, traceEventFromAgentEvent } from './trace.js';
import { createSourceCatalog } from './source-catalog.js';
import { ensureMaterialHypotheses } from '../ai-construction/material-hypotheses.js';
import { summarizeModelingBrief, type ModelingBrief } from '../ai-construction/modeling-brief.js';

const TITLES: Record<NodeType, string> = {
  project_setup: '项目创建', ingest: '资料上传与解析', analyze: '初步分析',
  interview: '建模访谈', generate: '知识包生成',
  review: '知识包工作区', data_source: '数据源配置', testcases: '评测用例录入', evaluate: '评测运行', optimize: '优化建议与应用',
  dyn_followup: '追问访谈', dyn_deepread: '深读文档', dyn_probe: '数据探查', dyn_conflict: '冲突消解',
};

function rowToNode(r: any): WorkflowNode {
  return {
    id: r.id, projectId: r.project_id, type: r.type, status: r.status,
    ordinal: r.ordinal, title: r.title, reason: r.reason ?? undefined,
    agentMessages: r.agent_messages ? JSON.parse(r.agent_messages) : undefined,
    output: r.output_json ? JSON.parse(r.output_json) : undefined,
    createdAt: r.created_at, updatedAt: r.updated_at,
  };
}

export function listNodes(projectId: string): WorkflowNode[] {
  const db = getDb();
  const rows = db.prepare('SELECT * FROM wf_nodes WHERE project_id = ? ORDER BY ordinal').all(projectId);
  return rows.map(rowToNode);
}

export function latestNodeOf(
  nodes: WorkflowNode[],
  type: NodeType,
  statuses?: NodeStatus[],
): WorkflowNode | undefined {
  return nodes.reduce<WorkflowNode | undefined>((latest, node) => {
    if (node.type !== type || (statuses && !statuses.includes(node.status))) return latest;
    return !latest || node.ordinal > latest.ordinal ? node : latest;
  }, undefined);
}

export function getNode(id: string): WorkflowNode | null {
  const db = getDb();
  const r = db.prepare('SELECT * FROM wf_nodes WHERE id = ?').get(id);
  return r ? rowToNode(r as any) : null;
}

export function upsertNode(node: {
  projectId: string; type: NodeType; status?: NodeStatus; ordinal: number;
  reason?: string; agentMessages?: unknown[]; output?: unknown; id?: string;
}): WorkflowNode {
  const db = getDb();
  const now = nowIso();
  const id = node.id ?? `nd_${randomUUID().slice(0, 12)}`;
  const existing = node.id ? getNode(node.id) : null;
  if (existing) {
    db.prepare(`UPDATE wf_nodes SET status=?, reason=?, agent_messages=?, output_json=?, updated_at=? WHERE id=?`)
      .run(node.status ?? existing.status, node.reason ?? existing.reason ?? null,
        node.agentMessages ? JSON.stringify(node.agentMessages) : (existing.agentMessages ? JSON.stringify(existing.agentMessages) : null),
        node.output ? JSON.stringify(node.output) : (existing.output ? JSON.stringify(existing.output) : null),
        now, id);
  } else {
    db.prepare(`INSERT INTO wf_nodes (id, project_id, type, status, ordinal, title, reason, agent_messages, output_json, created_at, updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`)
      .run(id, node.projectId, node.type, node.status ?? 'pending', node.ordinal, TITLES[node.type],
        node.reason ?? null, node.agentMessages ? JSON.stringify(node.agentMessages) : null,
        node.output ? JSON.stringify(node.output) : null, now, now);
  }
  return getNode(id)!;
}

export function nextOrdinal(projectId: string): number {
  const db = getDb();
  const r = db.prepare('SELECT MAX(ordinal) AS m FROM wf_nodes WHERE project_id = ?').get(projectId) as { m: number | null };
  return (r.m ?? -1) + 1;
}

type ArtifactKind = 'object' | 'rule' | 'operation' | 'metric';

export interface ArtifactValidationIssue {
  source: string;
  sourceType: 'table' | 'document';
  kind?: ArtifactKind;
  message: string;
}

type ArtifactAnalysis = Pick<AnalysisResult, 'materialAssessments' | 'candidateArtifacts'>;

export interface MaterialAnalysisProgressUpdate {
  phase: string;
  title: string;
  summary?: string;
  percent: number;
}

export function canonicalizeArtifactAnalysisSources(
  tables: DataDictTable[],
  documents: { filename: string; textContent?: string }[],
  analysis: ArtifactAnalysis,
): ArtifactAnalysis {
  const tableCatalog = createSourceCatalog(tables);
  const documentNames = documents.map((document) => document.filename);
  const resolveDocument = (reference: string): string | undefined => {
    const exact = documentNames.filter((name) => name === reference);
    if (exact.length === 1) return exact[0];
    if (exact.length > 1) return undefined;
    const normalized = documentNames.filter((name) => name.trim() === reference.trim());
    return normalized.length === 1 ? normalized[0] : undefined;
  };
  const resolveTable = (reference: string): string | undefined => {
    const resolution = tableCatalog.resolveTable(reference);
    return resolution.status === 'resolved' ? resolution.tableName : undefined;
  };
  const resolveCandidateSource = (reference: string): string => {
    const exactDocument = documentNames.find((name) => name === reference);
    if (exactDocument) return exactDocument;
    return resolveTable(reference) ?? resolveDocument(reference) ?? reference;
  };

  return {
    materialAssessments: analysis.materialAssessments?.map((assessment) => ({
      ...assessment,
      source: assessment.sourceType === 'table'
        ? resolveTable(assessment.source) ?? assessment.source
        : resolveDocument(assessment.source) ?? assessment.source,
    })),
    candidateArtifacts: analysis.candidateArtifacts?.map((candidate) => ({
      ...candidate,
      sources: candidate.sources.map(resolveCandidateSource),
    })),
  };
}

export function validateArtifactAnalysis(
  tables: DataDictTable[],
  documents: { filename: string; textContent?: string }[],
  analysis: ArtifactAnalysis,
): ArtifactValidationIssue[] {
  const canonical = canonicalizeArtifactAnalysisSources(tables, documents, analysis);
  const assessments = canonical.materialAssessments ?? [];
  const candidates = canonical.candidateArtifacts ?? [];
  const expected = [
    ...tables.map((table) => ({ source: table.name, sourceType: 'table' as const })),
    ...documents.map((document) => ({ source: document.filename, sourceType: 'document' as const })),
  ];
  const issues: ArtifactValidationIssue[] = [];

  for (const input of expected) {
    const assessment = assessments.find((item) => item.source === input.source && item.sourceType === input.sourceType);
    if (!assessment) {
      issues.push({ ...input, message: `未覆盖输入材料 ${input.sourceType}:${input.source}` });
      continue;
    }
    if (!assessment.suitableFor?.length) {
      issues.push({ ...input, message: `材料 ${input.source} 未标注适合生成的知识类型` });
      continue;
    }
    for (const kind of assessment.suitableFor) {
      const covered = candidates.some((candidate) => candidate.kind === kind && candidate.sources?.includes(input.source));
      if (!covered) {
        issues.push({
          ...input,
          kind,
          message: `材料 ${input.source} 标注可生成 ${kind}，但没有对应候选`,
        });
      }
    }
  }
  return issues;
}

export function formatArtifactIssues(issues: ArtifactValidationIssue[]): string {
  return issues.map((issue) => `- ${issue.message}`).join('\n');
}

interface ArtifactBatch {
  tables: DataDictTable[];
  documents: { filename: string; textContent?: string }[];
  refs: { source: string; sourceType: 'table' | 'document' }[];
}

function buildArtifactBatches(
  tables: DataDictTable[],
  documents: { filename: string; textContent?: string }[],
  size = 4,
): ArtifactBatch[] {
  const sources = [
    ...tables.map((table) => ({ source: table.name, sourceType: 'table' as const, table })),
    ...documents.map((document) => ({ source: document.filename, sourceType: 'document' as const, document })),
  ];
  const batches: ArtifactBatch[] = [];
  for (let start = 0; start < sources.length; start += size) {
    const chunk = sources.slice(start, start + size);
    batches.push({
      tables: chunk.filter((item) => item.sourceType === 'table').map((item) => item.table),
      documents: chunk.filter((item) => item.sourceType === 'document').map((item) => item.document),
      refs: chunk.map(({ source, sourceType }) => ({ source, sourceType })),
    });
  }
  return batches;
}

function mergeArtifactAnalyses(parts: ArtifactAnalysis[]): ArtifactAnalysis {
  const assessments = new Map<string, NonNullable<AnalysisResult['materialAssessments']>[number]>();
  const candidates = new Map<string, NonNullable<AnalysisResult['candidateArtifacts']>[number]>();
  for (const part of parts) {
    for (const assessment of part.materialAssessments ?? []) {
      assessments.set(`${assessment.sourceType}:${assessment.source}`, assessment);
    }
    for (const candidate of part.candidateArtifacts ?? []) {
      const sources = [...(candidate.sources ?? [])].sort().join('|');
      candidates.set(`${candidate.kind}:${candidate.name.trim()}:${sources}`, candidate);
    }
  }
  return {
    materialAssessments: [...assessments.values()],
    candidateArtifacts: [...candidates.values()],
  };
}

// 节点 3：初步分析。跑一次 pi agent run，让其读数据字典/文档，产出结构化分析。
// llmOverride 供冒烟注入 faux。
export async function runAnalyze(
  projectId: string,
  tables: DataDictTable[],
  documents: { filename: string; textContent?: string }[],
  llmOverride?: LlmDeps,
  onProgress?: (progress: MaterialAnalysisProgressUpdate) => void,
  confirmedBrief?: ModelingBrief,
): Promise<{ node: WorkflowNode; analysis: AnalysisResult }> {
  const llm = llmOverride ?? buildTaskLlm();
  const captured: Record<string, unknown> = {};
  const ctx: ToolContext = { tables, documents, captured };
  const tools = buildTools(ctx);
  const goalContext = confirmedBrief
    ? `已确认建模目标（不得擅自扩大或改写）：\n${summarizeModelingBrief(confirmedBrief)}`
    : '';

  const systemPrompt = [
    '你是企业知识工程建模分析助手。基于已解析的数据表与文档，完成初步分析：',
    ...(goalContext ? [goalContext] : []),
    '1) 用 list_tables / describe_table / list_documents / read_document 探查资料；',
    '2) 围绕已确认建模目标归纳领域概览，识别与目标直接相关的候选业务对象（对应哪些表）；',
    '3) 已确认建模目标是分析范围，不得另行提出或改写业务目标；',
    '4) 仅提出最多 3 个能支撑已确认目标的核心业务流程。每个候选需包含触发、角色、输入、步骤、异常、输出、交接和依据；材料缺失的部分可留空，不得编造；',
    '5) 列出仍需向领域专家澄清的少量开放问题，优先记录无法通过候选选择确认的关键口径；',
    '6) candidateObjects.sourceTables 只能引用 list_tables 返回的已有表名，不得把文档概念或推测名称当成表名；',
    '7) 最后调用 submit_analysis 提交结构化结果；若工具返回来源错误，修正后重新提交。',
  ].join('\n');

  const node = upsertNode({ projectId, type: 'analyze', status: 'running', ordinal: nextOrdinal(projectId) });
  const reportProgress = (progress: MaterialAnalysisProgressUpdate) => {
    try { onProgress?.(progress); } catch { /* 进度展示失败不能中断资料分析 */ }
  };
  let messagesSoFar: unknown[] = [];
  const trace = (input: Parameters<typeof appendTraceEvent>[0]) => appendTraceEvent(input);
  const observe = (phase: string) => (event: unknown) => {
    const draft = traceEventFromAgentEvent(event);
    if (!draft) return;
    trace({ ...draft, phase, projectId, nodeId: node.id });
  };
  trace({
    projectId, nodeId: node.id, phase: 'prepare', kind: 'phase_started', status: 'running', title: '准备分析材料',
    summary: `数据表 ${tables.length} 张 · 文档 ${documents.length} 份`,
    details: { tableCount: tables.length, documentCount: documents.length },
  });
  reportProgress({
    phase: 'prepare', title: '正在准备分析建模资料', percent: 5,
    summary: `数据表 ${tables.length} 张 · 文档 ${documents.length} 份`,
  });
  try {
    trace({ projectId, nodeId: node.id, phase: 'domain_analysis', kind: 'phase_started', status: 'running', title: '生成领域概览' });
    reportProgress({ phase: 'domain_analysis', title: '正在理解资料并整理领域概览', percent: 12 });
    const agent = makeAgent(llm, systemPrompt, tools);
    const messages = await runAgent(agent, confirmedBrief
      ? `请围绕以下已确认建模目标分析已上传资料，完成后调用 submit_analysis：\n${summarizeModelingBrief(confirmedBrief)}`
      : '请对已上传的资料做初步分析，完成后调用 submit_analysis。', observe('domain_analysis'));
    messagesSoFar = messages;

    const submittedAnalysis = (captured.analysis as AnalysisResult) ?? {
      domainOverview: '(agent 未提交结构化分析)',
      candidateObjects: [], openQuestions: [],
    };
    const canonicalAnalysis = createSourceCatalog(tables).canonicalizeAnalysis(submittedAnalysis);
    if (canonicalAnalysis.problems.length) {
      throw new Error(`领域分析来源无效：${canonicalAnalysis.problems.map((problem) => problem.message).join('；')}`);
    }
    const baseAnalysis = ensureMaterialHypotheses(canonicalAnalysis.value, confirmedBrief);
    trace({ projectId, nodeId: node.id, phase: 'domain_analysis', kind: 'phase_completed', status: 'done', title: '领域概览已生成' });
    reportProgress({
      phase: 'domain_analysis', title: '已形成候选业务目标和流程', percent: 28,
      summary: `目标 ${baseAnalysis.candidateGoals?.length ?? 0} 个 · 流程 ${baseAnalysis.candidateProcesses?.length ?? 0} 个`,
    });

    const batches = buildArtifactBatches(tables, documents);
    const parts: ArtifactAnalysis[] = [];
    const artifactMessages: unknown[] = [];
    const cacheBrief = briefFingerprint(confirmedBrief);
    let reusedBatches = 0;
    for (const [index, batch] of batches.entries()) {
      // 检查点：上一批已经完整落在 parts 里，这里停下来不会留半条记录
      throwIfCancelled(projectId, 'analyze');
      const phase = `artifact_batch_${index + 1}`;
      const batchStartPercent = 28 + Math.round((index / Math.max(1, batches.length)) * 52);
      // 这批材料一个字节都没变就直接用上次的结果，不再花一次 agent run
      const cacheKey = batchCacheKey(batch, cacheBrief);
      const cached = readCachedBatchAnalysis<ArtifactAnalysis>(projectId, cacheKey);
      if (cached) {
        reusedBatches += 1;
        parts.push(cached);
        trace({
          projectId, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
          title: `第 ${index + 1}/${batches.length} 批材料复用已有分析`,
          summary: batch.refs.map((item) => item.source).join('、'),
          details: { reused: true, sources: batch.refs.map((item) => item.source) },
        });
        reportProgress({
          phase, title: `第 ${index + 1}/${batches.length} 批资料未变动，复用已有分析`,
          summary: batch.refs.map((item) => item.source).join('、').slice(0, 500),
          percent: 28 + Math.round(((index + 1) / Math.max(1, batches.length)) * 52),
        });
        continue;
      }
      trace({
        projectId, nodeId: node.id, phase, kind: 'phase_started', status: 'running',
        title: `分析第 ${index + 1}/${batches.length} 批材料`,
        summary: batch.refs.map((item) => item.source).join('、'),
        details: { batch: index + 1, totalBatches: batches.length, sources: batch.refs.map((item) => item.source) },
      });
      reportProgress({
        phase, title: `正在分析第 ${index + 1}/${batches.length} 批资料`,
        summary: batch.refs.map((item) => item.source).join('、').slice(0, 500),
        percent: batchStartPercent,
      });
      const artifactCaptured: Record<string, unknown> = {};
      const artifactTools = buildArtifactAnalysisTools({
        tables: batch.tables,
        documents: batch.documents,
        captured: artifactCaptured,
      });
      const artifactPrompt = [
        '你是知识材料分类助手。请在领域对象分析之后，逐份判断当前批次材料适合沉淀成哪类知识资源。',
        ...(goalContext ? [goalContext, '只分类和提取能支撑该目标的知识；其他内容可标记为与当前目标无关。'] : []),
        '分类单位是材料中的知识片段；同一文档可以同时产出 rule 和 operation。',
        '判定规则：数据实体/字段/映射 → object；适用条件/判断门槛/例外 → rule；查询/推荐/办理步骤 → operation；公式/统计口径 → metric。',
        '只有材料明确给出条件、流程或公式时标记 extractable；缺少阈值、权威口径或映射时标记 needs_clarification，禁止猜测。',
        '每份输入材料都必须有 materialAssessment；suitableFor 中每一种类型都必须至少对应一个 candidateArtifact。',
        'source 必须精确使用当前批次来源中的名称；candidateArtifact.sources 也使用同样的精确名称。',
        '每个候选给出材料来源和简短证据或章节定位；在覆盖当前批次所有材料与类型的前提下尽量合并重复候选。',
        '探查完成后必须调用 submit_artifact_analysis 提交当前批次完整结构化结果。',
        `当前批次来源：${JSON.stringify(batch.refs)}`,
        `已有领域分析：${JSON.stringify(baseAnalysis).slice(0, 2500)}`,
      ].join('\n');
      const artifactAgent = makeAgent(llm, artifactPrompt, artifactTools);
      let batchMessages = await runAgentCaptured(
        artifactAgent,
        '请探查当前批次材料并调用 submit_artifact_analysis 提交材料适用类型和知识候选。',
        () => !!artifactCaptured.artifactAnalysis,
        '请立即调用 submit_artifact_analysis 提交当前批次完整结构化结果，不要只写正文。',
        observe(phase),
      );
      messagesSoFar = [...messages, ...artifactMessages, ...batchMessages];
      if (!artifactCaptured.artifactAnalysis) throw new Error('材料知识类型分析未按协议提交，请重试');
      let current = canonicalizeArtifactAnalysisSources(
        batch.tables,
        batch.documents,
        artifactCaptured.artifactAnalysis as ArtifactAnalysis,
      );
      let issues = validateArtifactAnalysis(batch.tables, batch.documents, current);
      for (let repair = 1; issues.length && repair <= 2; repair += 1) {
        const issueText = formatArtifactIssues(issues);
        trace({
          projectId, nodeId: node.id, phase, kind: 'validation_warning', status: 'warning',
          title: `第 ${index + 1} 批发现 ${issues.length} 个遗漏`, summary: issueText,
          details: { issues: issues.map(({ source, sourceType, kind }) => ({ source, sourceType, kind })) },
        });
        trace({
          projectId, nodeId: node.id, phase, kind: 'repair_started', status: 'warning',
          title: `第 ${repair} 次自动补交`, summary: issueText,
          details: { attempt: repair, missingCount: issues.length },
        });
        reportProgress({
          phase, title: `正在补齐第 ${index + 1}/${batches.length} 批知识候选`,
          summary: `发现 ${issues.length} 个遗漏，正在进行第 ${repair} 次自动补交`,
          percent: Math.min(79, batchStartPercent + 3),
        });
        delete artifactCaptured.artifactAnalysis;
        batchMessages = await runAgent(
          artifactAgent,
          `上次提交不完整。请重新提交当前批次完整结果，不要只提交增量：\n${issueText}`,
          observe(phase),
        );
        messagesSoFar = [...messages, ...artifactMessages, ...batchMessages];
        if (!artifactCaptured.artifactAnalysis) {
          trace({
            projectId, nodeId: node.id, phase, kind: 'repair_completed', status: 'warning',
            title: `第 ${repair} 次自动补交未提交结构化结果`, summary: '模型没有调用材料知识类型提交工具',
            details: { attempt: repair, remainingCount: issues.length },
          });
          continue;
        }
        current = canonicalizeArtifactAnalysisSources(
          batch.tables,
          batch.documents,
          artifactCaptured.artifactAnalysis as ArtifactAnalysis,
        );
        issues = validateArtifactAnalysis(batch.tables, batch.documents, current);
        trace({
          projectId, nodeId: node.id, phase, kind: 'repair_completed',
          status: issues.length ? 'warning' : 'done', title: `第 ${repair} 次自动补交${issues.length ? '仍有遗漏' : '完成'}`,
          summary: issues.length ? formatArtifactIssues(issues) : '当前批次校验通过',
          details: { attempt: repair, remainingCount: issues.length },
        });
      }
      if (issues.length) {
        throw new Error(`第 ${index + 1} 批材料自动补交 2 次后仍不完整：\n${formatArtifactIssues(issues)}`);
      }
      parts.push(current);
      // 只缓存校验通过的结果：补交两次仍不完整的批次上面已经抛错，不会走到这里
      writeCachedBatchAnalysis(projectId, cacheKey, current);
      artifactMessages.push(...batchMessages);
      messagesSoFar = [...messages, ...artifactMessages];
      trace({
        projectId, nodeId: node.id, phase, kind: 'phase_completed', status: 'done',
        title: `第 ${index + 1}/${batches.length} 批材料校验通过`,
        summary: `材料 ${current.materialAssessments?.length ?? 0} · 候选 ${current.candidateArtifacts?.length ?? 0}`,
      });
      reportProgress({
        phase, title: `第 ${index + 1}/${batches.length} 批资料分析完成`,
        summary: `材料 ${current.materialAssessments?.length ?? 0} 份 · 知识候选 ${current.candidateArtifacts?.length ?? 0} 个`,
        percent: 28 + Math.round(((index + 1) / Math.max(1, batches.length)) * 52),
      });
    }
    if (reusedBatches) {
      trace({
        projectId, nodeId: node.id, phase: 'artifact_analysis', kind: 'phase_completed', status: 'done',
        title: `复用了 ${reusedBatches}/${batches.length} 批未变动资料的分析`,
        summary: '只有内容发生变化的资料重新分析，其余沿用上次结果',
        details: { reusedBatches, totalBatches: batches.length },
      });
    }
    const artifactAnalysis = mergeArtifactAnalyses(parts);
    const analysis: AnalysisResult = { ...baseAnalysis, ...artifactAnalysis };

    // 资料质量评估：确定性检查（同步、零成本）+ LLM 缺口分析（best-effort）。
    // 结论并入节点 output（quality 字段），供面板展示与访谈优先澄清。
    trace({ projectId, nodeId: node.id, phase: 'quality', kind: 'phase_started', status: 'running', title: '生成资料质量报告' });
    reportProgress({ phase: 'quality', title: '正在检查资料质量和关键缺口', percent: 86 });
    const { runDataQualityChecks, runGapAnalysis, buildQualityReport } = await import('./data-quality.js');
    const findings = runDataQualityChecks(tables, documents);
    let gaps: Awaited<ReturnType<typeof runGapAnalysis>> = [];
    try {
      const { getProject } = await import('../projects/repo.js');
      const project = getProject(projectId);
      if (project) gaps = await runGapAnalysis(project, tables, documents, analysis, llm);
    } catch { /* 缺口分析失败不阻断领域分析 */ }
    const quality = buildQualityReport(findings, gaps, tables.length, documents.filter((d) => d.textContent).length);
    trace({ projectId, nodeId: node.id, phase: 'quality', kind: 'phase_completed', status: 'done', title: '资料质量报告已生成' });
    reportProgress({ phase: 'quality', title: '资料质量检查已完成', percent: 96 });

    const done = upsertNode({
      id: node.id, projectId, type: 'analyze', status: 'done', ordinal: node.ordinal,
      agentMessages: [...messages, ...artifactMessages], output: { ...analysis, quality },
    });
    trace({ projectId, nodeId: node.id, phase: 'complete', kind: 'analysis_completed', status: 'done', title: '领域分析完成' });
    reportProgress({
      phase: 'complete', title: '建模资料分析完成', percent: 100,
      summary: `候选目标 ${analysis.candidateGoals?.length ?? 0} 个 · 候选流程 ${analysis.candidateProcesses?.length ?? 0} 个`,
    });
    return { node: done, analysis };
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    upsertNode({
      id: node.id, projectId, type: 'analyze', status: 'failed', ordinal: node.ordinal,
      reason, agentMessages: messagesSoFar,
    });
    trace({
      projectId, nodeId: node.id, phase: 'failed', kind: 'analysis_failed', status: 'error',
      title: '领域分析失败', summary: reason,
    });
    throw error;
  }
}
