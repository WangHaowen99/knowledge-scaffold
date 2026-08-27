import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { NodeStatus, NodeType, WorkflowNode, DataDictTable, AnalysisResult } from '../types.js';
import { buildTaskLlm, type LlmDeps } from '../llm/provider.js';
import { appendTraceEvent, traceEventFromAgentEvent } from './trace.js';
import { createSourceCatalog } from './source-catalog.js';
import { ensureMaterialHypotheses } from '../ai-construction/material-hypotheses.js';
import type { ModelingBrief } from '../ai-construction/modeling-brief.js';
import type { ReadableDocument } from './document-read-session.js';
import { runMaterialAnalysisMapReduce } from './material-analysis-map-reduce.js';

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
  documents: ReadableDocument[],
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
  documents: ReadableDocument[],
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

// 节点 3：动态分段并发分析，文档级 Reduce 后再做一次领域 Reduce。
// 分段和汇总均持久化，失败或服务重启后从未完成分段继续。
export async function runAnalyze(
  projectId: string,
  tables: DataDictTable[],
  documents: ReadableDocument[],
  llmOverride?: LlmDeps,
  onProgress?: (progress: MaterialAnalysisProgressUpdate) => void,
  confirmedBrief?: ModelingBrief,
): Promise<{ node: WorkflowNode; analysis: AnalysisResult }> {
  const llm = llmOverride ?? buildTaskLlm();
  const node = upsertNode({ projectId, type: 'analyze', status: 'running', ordinal: nextOrdinal(projectId) });
  const reportProgress = (progress: MaterialAnalysisProgressUpdate) => {
    try { onProgress?.(progress); } catch { /* 进度展示失败不能中断资料分析 */ }
  };
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
    const { getProject } = await import('../projects/repo.js');
    const project = getProject(projectId);
    const mapReduce = await runMaterialAnalysisMapReduce({
      projectId,
      projectContext: [project?.domain, project?.background, project?.targetUsers]
        .filter(Boolean).join('\n').slice(0, 8_000),
      tables,
      documents,
      llm,
      confirmedBrief,
      onProgress: reportProgress,
      observe,
      finalizeAnalysis: (submittedAnalysis) => {
        const canonicalAnalysis = createSourceCatalog(tables).canonicalizeAnalysis(submittedAnalysis);
        if (canonicalAnalysis.problems.length) {
          throw new Error(`领域分析来源无效：${canonicalAnalysis.problems.map((problem) => problem.message).join('；')}`);
        }
        const canonicalArtifacts = canonicalizeArtifactAnalysisSources(tables, documents, canonicalAnalysis.value);
        const issues = validateArtifactAnalysis(tables, documents, canonicalArtifacts);
        if (issues.length) throw new Error(`资料分析结果不完整：\n${formatArtifactIssues(issues)}`);
        return ensureMaterialHypotheses({ ...canonicalAnalysis.value, ...canonicalArtifacts }, confirmedBrief);
      },
    });
    const analysis = mapReduce.analysis;

    trace({ projectId, nodeId: node.id, phase: 'quality', kind: 'phase_started', status: 'running', title: '生成资料质量报告' });
    reportProgress({ phase: 'quality', title: '正在检查资料质量和关键缺口', percent: 86 });
    const { runDataQualityChecks, runGapAnalysis, buildQualityReport } = await import('./data-quality.js');
    const findings = runDataQualityChecks(tables, documents);
    let gaps: Awaited<ReturnType<typeof runGapAnalysis>> = [];
    try {
      if (project) gaps = await runGapAnalysis(project, tables, documents, analysis, llm);
    } catch { /* 缺口分析失败不阻断领域分析 */ }
    const quality = buildQualityReport(findings, gaps, tables.length, documents.filter((d) => d.textContent).length);
    trace({ projectId, nodeId: node.id, phase: 'quality', kind: 'phase_completed', status: 'done', title: '资料质量报告已生成' });
    reportProgress({ phase: 'quality', title: '资料质量检查已完成', percent: 96 });

    const done = upsertNode({
      id: node.id, projectId, type: 'analyze', status: 'done', ordinal: node.ordinal,
      agentMessages: [], output: { ...analysis, quality },
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
      reason, agentMessages: [],
    });
    trace({
      projectId, nodeId: node.id, phase: 'failed', kind: 'analysis_failed', status: 'error',
      title: '领域分析失败', summary: reason,
    });
    throw error;
  }
}
