import { Type } from '@earendil-works/pi-ai';
import { createHash } from 'node:crypto';
import type { LlmDeps } from '../llm/provider.js';
import type {
  AnalysisResult,
  ArtifactCandidate,
  DataDictTable,
  MaterialAssessment,
  ModelingBrief,
} from '../types.js';
import { throwIfCancelled } from '../cancellation.js';
import { makeAgent, runAgentCaptured } from './agent.js';
import {
  assertCompleteDocumentPlan,
  planDocumentAnalysis,
  serializableDocumentAnalysisPlan,
  uniqueCoveredChars,
  type DocumentAnalysisChunk,
  type DocumentAnalysisPlan,
} from './document-analysis-plan.js';
import type { ReadableDocument } from './document-read-session.js';
import {
  MaterialAnalysisCheckpointStore,
  materialBriefFingerprint,
  materialSourceSetFingerprint,
  type ChunkAnalysisPayload,
  type ChunkEvidence,
  type ChunkCandidate,
  type DocumentAnalysisPayload,
  type MaterialAnalysisChunkRecord,
} from './material-analysis-checkpoints.js';
import { summarizeModelingBrief } from '../ai-construction/modeling-brief.js';

export interface MaterialMapReduceProgress {
  phase: string;
  title: string;
  summary?: string;
  percent: number;
}

export interface MaterialMapReduceResult {
  analysis: AnalysisResult;
  runId: string;
  reusedChunks: number;
  totalChunks: number;
}

export class MaterialAnalysisPausedError extends Error {
  readonly code = 'MODEL_QUOTA_EXHAUSTED';

  constructor(readonly runId: string, reason: string) {
    super(`模型额度不足，资料分析已暂停并保留断点：${reason}`);
    this.name = 'MaterialAnalysisPausedError';
  }
}

const artifactKind = Type.Union([
  Type.Literal('object'), Type.Literal('rule'), Type.Literal('operation'), Type.Literal('metric'),
]);
const chunkCandidateKind = Type.Union([artifactKind, Type.Literal('process')]);
const readiness = Type.Union([Type.Literal('extractable'), Type.Literal('needs_clarification')]);

const candidateSchema = Type.Object({
  kind: Type.Optional(chunkCandidateKind),
  name: Type.Optional(Type.String()),
  rationale: Type.Optional(Type.String()),
  evidenceOffsets: Type.Optional(Type.Array(Type.Number({ minimum: 0 }), { maxItems: 40 })),
  readiness: Type.Optional(readiness),
});

const submittedCandidateKind = Type.Union([chunkCandidateKind, Type.Literal('fact')]);
const submittedOffset = Type.Union([Type.Number({ minimum: 0 }), Type.String()]);

const normalizeNumber = (value: unknown): number | undefined => {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : undefined;
};

const normalizeCandidateKind = (value: unknown): ChunkCandidate['kind'] | 'fact' | undefined => {
  const kind = String(value ?? '').trim();
  return ['object', 'rule', 'operation', 'metric', 'process', 'fact'].includes(kind)
    ? kind as ChunkCandidate['kind'] | 'fact'
    : undefined;
};

function taskModel(llm: LlmDeps, hardOutputCap: number): LlmDeps {
  const contextWindow = Math.max(8_192, Number(llm.model?.contextWindow) || 128_000);
  const maxTokens = Math.max(1_024, Math.min(
    Number(llm.model?.maxTokens) || hardOutputCap,
    hardOutputCap,
    Math.floor(contextWindow * 0.20),
  ));
  return { ...llm, model: { ...llm.model, maxTokens } };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isQuotaExhausted(error: unknown): boolean {
  return /insufficient[_ -]?quota|quota.{0,40}exhaust|额度.{0,20}(耗尽|不足)/iu.test(errorText(error));
}

function isRateLimited(error: unknown): boolean {
  return /\b429\b|rate[_ -]?limit|too many requests|请求过于频繁|限流/iu.test(errorText(error));
}

function isTransientModelFailure(error: unknown): boolean {
  return /\b5\d\d\b|timeout|timed out|ECONN(?:RESET|REFUSED)|EAI_AGAIN|socket hang up|网络.{0,12}(异常|中断)/iu
    .test(errorText(error));
}

function normalizedChunkPayload(value: any, chunk: DocumentAnalysisChunk): ChunkAnalysisPayload {
  const inRange = (offset: unknown): offset is number => (
    Number.isFinite(offset) && Number(offset) >= chunk.startOffset && Number(offset) < chunk.endOffset
  );
  const evidence = (Array.isArray(value?.evidence) ? value.evidence : []).flatMap((item: any) => {
    const offset = normalizeNumber(item?.offset);
    return offset !== undefined && inRange(offset) && String(item?.note ?? '').trim()
      ? [{
          offset: Math.floor(offset),
          kind: ['object', 'rule', 'operation', 'metric', 'process', 'fact'].includes(item.kind)
            ? item.kind
            : 'fact',
          note: String(item.note).trim().slice(0, 500),
        }]
      : [];
  }).slice(0, 40);
  const candidates = (Array.isArray(value?.candidates) ? value.candidates : []).flatMap((item: any) => {
    const kind = normalizeCandidateKind(item?.kind);
    const name = String(item?.name ?? '').trim();
    const rationale = String(item?.rationale ?? '').trim();
    if (!name || !rationale || !kind || kind === 'fact') {
      // Some models classify a factual statement as a candidate. Preserve it
      // as evidence instead of forcing another full model call.
      if (kind === 'fact' && rationale) {
        for (const offsetValue of Array.isArray(item?.evidenceOffsets) ? item.evidenceOffsets : []) {
          const offset = normalizeNumber(offsetValue);
          if (offset !== undefined && inRange(offset)) {
            evidence.push({ offset: Math.floor(offset), kind: 'fact', note: rationale.slice(0, 500) });
          }
        }
      }
      return [];
    }
    return [{
      kind,
      name: name.slice(0, 200),
      rationale: rationale.slice(0, 1_000),
      evidenceOffsets: (Array.isArray(item.evidenceOffsets) ? item.evidenceOffsets : [])
        .map(normalizeNumber).filter((offset: number | undefined): offset is number => offset !== undefined)
        .filter(inRange).map((offset: number) => Math.floor(offset)).slice(0, 20),
      readiness: item.readiness === 'needs_clarification' ? 'needs_clarification' : 'extractable',
    } as ChunkCandidate];
  });
  const normalizedEvidence = evidence.slice(0, 40);
  const normalizedCandidates = candidates.map((candidate: ChunkCandidate) => ({
    ...candidate,
    evidenceOffsets: candidate.evidenceOffsets.filter((offset: number) => normalizedEvidence.some((item: ChunkEvidence) => item.offset === offset)),
  }));
  return {
    summary: String(value?.summary ?? '').trim().slice(0, 4_000) || '该分段未提取到可用摘要。',
    evidence: normalizedEvidence,
    candidates: normalizedCandidates,
    openQuestions: (Array.isArray(value?.openQuestions) ? value.openQuestions : []).flatMap((item: any) => {
      const topic = String(item?.topic ?? '').trim();
      const why = String(item?.why ?? '').trim();
      return topic && why ? [{ topic: topic.slice(0, 300), why: why.slice(0, 1_000) }] : [];
    }).slice(0, 20),
  };
}

function latestAssistantText(messages: unknown[]): string {
  for (const message of [...messages].reverse() as any[]) {
    if (message?.role !== 'assistant') continue;
    const content = message.content;
    const value = typeof content === 'string'
      ? content
      : Array.isArray(content)
        ? content.filter((part: any) => part?.type === 'text').map((part: any) => String(part.text ?? '')).join('\n')
        : '';
    if (value.trim()) return value.trim();
  }
  return '';
}

function balancedChunkReduceInput(chunks: MaterialAnalysisChunkRecord[], llm: LlmDeps) {
  const contextWindow = Math.max(8_192, Number(llm.model?.contextWindow) || 128_000);
  const outputReserve = Math.max(1_024, Math.min(
    Number(llm.model?.maxTokens) || 12_288,
    12_288,
    Math.floor(contextWindow * 0.20),
  ));
  const totalBudget = Math.max(2_000, Math.min(
    120_000,
    contextWindow - outputReserve - 4_096 - Math.floor(contextWindow * 0.10),
  ));
  const perChunk = Math.max(160, Math.floor(totalBudget / Math.max(1, chunks.length)));
  return chunks.map((chunk) => {
    const payload = chunk.payload;
    const candidates = (payload?.candidates ?? []).slice(0, Math.max(1, Math.floor(perChunk / 260))).map((candidate) => ({
      kind: candidate.kind,
      name: candidate.name,
      rationale: candidate.rationale.slice(0, Math.max(80, Math.floor(perChunk / 8))),
      evidenceOffsets: candidate.evidenceOffsets,
      readiness: candidate.readiness,
    }));
    const evidence = (payload?.evidence ?? []).slice(0, Math.max(1, Math.floor(perChunk / 220))).map((item) => ({
      ...item,
      note: item.note.slice(0, Math.max(80, Math.floor(perChunk / 12))),
    }));
    const fixed = JSON.stringify({
      range: [chunk.startOffset, chunk.endOffset],
      candidates,
      evidence,
      openQuestions: (payload?.openQuestions ?? []).slice(0, Math.max(1, Math.floor(perChunk / 260))),
    });
    return {
      range: [chunk.startOffset, chunk.endOffset],
      summary: payload?.summary.slice(0, Math.max(80, perChunk - fixed.length)),
      evidence,
      candidates,
      openQuestions: (payload?.openQuestions ?? []).slice(0, Math.max(1, Math.floor(perChunk / 260))),
    };
  });
}

function mergeChunkCandidates(
  submitted: ChunkCandidate[],
  extracted: ChunkCandidate[],
): ChunkCandidate[] {
  const merged = new Map<string, ChunkCandidate>();
  for (const candidate of [...extracted, ...submitted]) {
    const key = `${candidate.kind}\0${candidate.name.trim()}`;
    const previous = merged.get(key);
    merged.set(key, previous ? {
      ...previous,
      rationale: previous.rationale.length >= candidate.rationale.length
        ? previous.rationale
        : candidate.rationale,
      evidenceOffsets: [...new Set([...previous.evidenceOffsets, ...candidate.evidenceOffsets])].sort((a, b) => a - b),
      readiness: previous.readiness === 'needs_clarification' || candidate.readiness === 'needs_clarification'
        ? 'needs_clarification'
        : 'extractable',
    } : candidate);
  }
  return [...merged.values()];
}

function balancedDomainDocuments(
  documents: Array<{ filename: string; payload: DocumentAnalysisPayload }>,
  llm: LlmDeps,
) {
  const contextWindow = Math.max(8_192, Number(llm.model?.contextWindow) || 128_000);
  const totalBudget = Math.max(2_000, Math.min(100_000, Math.floor(contextWindow * 0.45)));
  const perDocument = Math.max(400, Math.floor(totalBudget / Math.max(1, documents.length)));
  return documents.map((document) => {
    const candidates = document.payload.candidates.map((candidate) => ({
      kind: candidate.kind,
      name: candidate.name,
      rationale: candidate.rationale.slice(0, Math.max(80, Math.floor(perDocument / 8))),
      evidenceOffsets: candidate.evidenceOffsets,
      readiness: candidate.readiness,
    }));
    const questions = document.payload.openQuestions.map((question) => ({
      topic: question.topic.slice(0, 160),
      why: question.why.slice(0, 400),
    }));
    const fixed = JSON.stringify({
      filename: document.filename,
      suitableFor: document.payload.suitableFor,
      rationale: document.payload.rationale,
      candidates,
      openQuestions: questions,
    });
    return {
      filename: document.filename,
      summary: document.payload.summary.slice(0, Math.max(160, perDocument - fixed.length)),
      suitableFor: document.payload.suitableFor,
      rationale: document.payload.rationale.slice(0, 500),
      candidates,
      openQuestions: questions,
    };
  });
}

function balancedTableSummaries(tables: DataDictTable[], llm: LlmDeps) {
  const contextWindow = Math.max(8_192, Number(llm.model?.contextWindow) || 128_000);
  const totalBudget = Math.max(2_000, Math.min(60_000, Math.floor(contextWindow * 0.25)));
  const fieldsPerTable = Math.max(4, Math.min(80, Math.floor(totalBudget / Math.max(1, tables.length) / 80)));
  return tables.map((table) => ({
    name: table.name,
    rowCount: table.rowCount,
    fields: table.fields.slice(0, fieldsPerTable).map((field) => ({
      name: field.name,
      type: field.inferredType,
      note: field.note?.slice(0, 120),
      samples: field.sampleValues.slice(0, 3).map((value) => String(value).slice(0, 80)),
    })),
    totalFields: table.fields.length,
  }));
}

async function analyzeChunk(
  llm: LlmDeps,
  chunk: DocumentAnalysisChunk,
  observe?: (event: unknown) => void,
): Promise<ChunkAnalysisPayload> {
  let fallbackSummary = '';
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const result = await analyzeChunkAttempt(llm, chunk, observe);
    if (result.payload) return result.payload;
    fallbackSummary = result.assistantText || fallbackSummary;
  }
  if (fallbackSummary) {
    return {
      summary: fallbackSummary.slice(0, 4_000),
      evidence: [],
      candidates: [],
      openQuestions: [{
        topic: `${chunk.filename} 第 ${chunk.chunkIndex + 1} 段需要在文档汇总阶段补充结构化提炼`,
        why: '模型已阅读并输出分段正文摘要，但连续多次未调用结构化提交工具。',
      }],
    };
  }
  throw new Error('分段分析未提交结构化结果');
}

async function analyzeChunkAttempt(
  llm: LlmDeps,
  chunk: DocumentAnalysisChunk,
  observe?: (event: unknown) => void,
): Promise<{ payload?: ChunkAnalysisPayload; assistantText: string }> {
  const capture: { value?: unknown } = {};
  const submit = {
    name: 'submit_document_chunk_analysis',
    label: '提交文档分段分析',
    description: '提交当前分段的摘要、带原文 offset 的证据、知识候选和待确认问题。',
    parameters: Type.Object({
      summary: Type.Optional(Type.String({ maxLength: 4_000 })),
      evidence: Type.Optional(Type.Array(Type.Object({
        offset: Type.Optional(submittedOffset),
        kind: Type.Optional(Type.Union([artifactKind, Type.Literal('process'), Type.Literal('fact')])),
        note: Type.Optional(Type.String({ maxLength: 500 })),
      }), { maxItems: 80 })),
      candidates: Type.Optional(Type.Array(Type.Object({
        kind: Type.Optional(submittedCandidateKind),
        name: Type.Optional(Type.String()),
        rationale: Type.Optional(Type.String()),
        evidenceOffsets: Type.Optional(Type.Array(submittedOffset, { maxItems: 40 })),
        readiness: Type.Optional(readiness),
      }), { maxItems: 60 })),
      openQuestions: Type.Optional(Type.Array(Type.Object({ topic: Type.Optional(Type.String()), why: Type.Optional(Type.String()) }), { maxItems: 40 })),
    }),
    async execute(_id: string, params: unknown) {
      capture.value = params;
      return { content: [{ type: 'text' as const, text: '当前分段分析已提交。' }], details: {}, terminate: true };
    },
  };
  const systemPrompt = [
    '你是资料分段知识提取器。只分析当前原文分段，不推测分段外内容。',
    '提取可复用的业务事实、对象、规则、操作、指标和流程线索；证据 offset 必须位于给定区间。',
    '缺少阈值、权威口径或映射时标记 needs_clarification，禁止补造业务事实。',
    '最后必须调用 submit_document_chunk_analysis，不要只输出正文。',
    `文档：${chunk.filename}`,
    `原文区间：[${chunk.startOffset}, ${chunk.endOffset})`,
  ].join('\n');
  const agent = makeAgent(
    taskModel(llm, 8_192),
    systemPrompt,
    [submit] as any,
  );
  const messages = await runAgentCaptured(
    agent,
    `请分析以下原文分段并提交结构化结果：\n\n${chunk.content}`,
    () => Boolean(capture.value && typeof (capture.value as any).summary === 'string' && String((capture.value as any).summary).trim()),
    '请立即调用 submit_document_chunk_analysis 提交当前分段结果。',
    observe,
    { timeoutMs: 600_000, maxCaptureAttempts: 3 },
  );
  return {
    ...(capture.value ? { payload: normalizedChunkPayload(capture.value, chunk) } : {}),
    assistantText: latestAssistantText(messages),
  };
}

async function reduceDocument(
  llm: LlmDeps,
  document: MaterialAnalysisChunkRecord,
  chunks: MaterialAnalysisChunkRecord[],
  observe?: (event: unknown) => void,
): Promise<DocumentAnalysisPayload> {
  const capture: { value?: any } = {};
  const submit = {
    name: 'submit_document_analysis',
    label: '提交文档汇总分析',
    description: '合并当前文档的分段结果，给出文档级摘要、知识类型、候选和待确认问题。',
    parameters: Type.Object({
      summary: Type.String({ maxLength: 6_000 }),
      suitableFor: Type.Array(artifactKind),
      rationale: Type.String({ maxLength: 2_000 }),
      candidates: Type.Array(candidateSchema, { maxItems: 100 }),
      openQuestions: Type.Array(Type.Object({ topic: Type.String(), why: Type.String() }), { maxItems: 40 }),
    }),
    async execute(_id: string, params: unknown) {
      capture.value = params;
      return { content: [{ type: 'text' as const, text: '文档汇总已提交。' }], details: {}, terminate: true };
    },
  };
  const inputs = balancedChunkReduceInput(chunks, llm);
  const agent = makeAgent(
    taskModel(llm, 12_288),
    [
      '你是文档级知识汇总器。输入来自同一文档的全部分段，已按 offset 排序并完成全文覆盖。',
      '合并同义候选，保留原始证据 offset；不得新增分段结果中不存在的业务阈值和事实。',
      '判断文档适合沉淀为 object/rule/operation/metric 中的哪些类型。',
      '最后调用 submit_document_analysis。',
      `文档：${document.filename}`,
    ].join('\n'),
    [submit] as any,
  );
  await runAgentCaptured(
    agent,
    `请汇总该文档的全部分段结果：\n${JSON.stringify(inputs)}`,
    () => Boolean(capture.value),
    '请立即调用 submit_document_analysis 提交文档汇总。',
    observe,
    { timeoutMs: 600_000 },
  );
  if (!capture.value) throw new Error(`文档汇总未提交：${document.filename}`);
  const allCandidates = chunks.flatMap((chunk) => chunk.payload?.candidates ?? []);
  const validOffsets = new Set(chunks.flatMap((chunk) => chunk.payload?.evidence.map((item) => item.offset) ?? []));
  const candidates: ChunkCandidate[] = (Array.isArray(capture.value.candidates) ? capture.value.candidates : []).flatMap((item: any) => {
    const fallback = allCandidates.find((candidate) => candidate.kind === item?.kind && candidate.name === item?.name);
    const name = String(item?.name ?? fallback?.name ?? '').trim();
    const rationale = String(item?.rationale ?? fallback?.rationale ?? '').trim();
    if (!name || !rationale || !['object', 'rule', 'operation', 'metric', 'process'].includes(item?.kind)) return [];
    return [{
      kind: item.kind,
      name: name.slice(0, 200),
      rationale: rationale.slice(0, 1_000),
      evidenceOffsets: (Array.isArray(item.evidenceOffsets) ? item.evidenceOffsets : [])
        .filter((offset: unknown) => Number.isFinite(offset) && validOffsets.has(Number(offset)))
        .map(Number).slice(0, 20),
      readiness: item.readiness === 'needs_clarification' ? 'needs_clarification' : 'extractable',
    } as ChunkCandidate];
  });
  const suitableFor = [...new Set([
    ...(Array.isArray(capture.value.suitableFor) ? capture.value.suitableFor : []),
    ...chunks.flatMap((chunk) => chunk.payload?.candidates ?? []).map((candidate) => candidate.kind),
  ]
    .filter((kind: unknown) => ['object', 'rule', 'operation', 'metric'].includes(String(kind))))] as DocumentAnalysisPayload['suitableFor'];
  return {
    summary: String(capture.value.summary ?? '').trim().slice(0, 6_000) || document.filename,
    suitableFor,
    rationale: String(capture.value.rationale ?? '').trim().slice(0, 2_000) || '基于全文分段汇总',
    candidates: mergeChunkCandidates(candidates, allCandidates),
    openQuestions: (Array.isArray(capture.value.openQuestions) ? capture.value.openQuestions : [])
      .flatMap((item: any) => String(item?.topic ?? '').trim() && String(item?.why ?? '').trim()
        ? [{ topic: String(item.topic).slice(0, 300), why: String(item.why).slice(0, 1_000) }]
        : []).slice(0, 40),
  };
}

function documentReduceFanout(llm: LlmDeps): number {
  const contextWindow = Math.max(8_192, Number(llm.model?.contextWindow) || 128_000);
  const outputReserve = Math.max(1_024, Math.min(
    Number(llm.model?.maxTokens) || 12_288,
    12_288,
    Math.floor(contextWindow * 0.20),
  ));
  const budget = Math.max(2_000, Math.min(
    120_000,
    contextWindow - outputReserve - 4_096 - Math.floor(contextWindow * 0.10),
  ));
  return Math.max(2, Math.min(48, Math.floor(budget / 600)));
}

function summaryChunk(
  document: MaterialAnalysisChunkRecord,
  group: MaterialAnalysisChunkRecord[],
  payload: DocumentAnalysisPayload,
  index: number,
): MaterialAnalysisChunkRecord {
  const evidenceByOffset = new Map(group.flatMap((chunk) => chunk.payload?.evidence ?? [])
    .map((item) => [item.offset, item]));
  const referencedOffsets = [...new Set(payload.candidates.flatMap((candidate) => candidate.evidenceOffsets))];
  return {
    runId: document.runId,
    documentKey: document.documentKey,
    documentId: document.documentId,
    filename: document.filename,
    revisionFingerprint: document.revisionFingerprint,
    chunkIndex: index,
    startOffset: Math.min(...group.map((chunk) => chunk.startOffset)),
    endOffset: Math.max(...group.map((chunk) => chunk.endOffset)),
    contentHash: `reduce-${index}`,
    status: 'completed',
    attemptCount: 1,
    payload: {
      summary: payload.summary,
      candidates: payload.candidates,
      evidence: referencedOffsets.map((offset) => evidenceByOffset.get(offset) ?? ({
        offset, kind: 'fact' as const, note: '分层汇总保留的原文证据位置',
      })),
      openQuestions: payload.openQuestions,
    },
  };
}

async function reduceDocumentHierarchically(
  llm: LlmDeps,
  document: MaterialAnalysisChunkRecord,
  chunks: MaterialAnalysisChunkRecord[],
  concurrency: number,
  observe?: (event: unknown) => void,
): Promise<DocumentAnalysisPayload> {
  const fanout = documentReduceFanout(llm);
  let current = chunks;
  let level = 0;
  while (current.length > fanout) {
    const groups = Array.from({ length: Math.ceil(current.length / fanout) }, (_, index) => (
      current.slice(index * fanout, (index + 1) * fanout)
    ));
    const summaries = (await adaptiveMap(groups, concurrency, (group) => (
      reduceDocument(llm, document, group, observe)
    ))).values;
    current = summaries.map((payload, index) => summaryChunk(document, groups[index], payload, index));
    level += 1;
    if (level > 20) throw new Error(`文档分层汇总没有收敛：${document.filename}`);
  }
  return reduceDocument(llm, document, current, observe);
}

interface DomainReducePayload extends Pick<AnalysisResult,
  'domainOverview' | 'candidateObjects' | 'candidateGoals' | 'candidateProcesses' | 'openQuestions'> {
  tableMaterialAssessments?: MaterialAssessment[];
  tableCandidateArtifacts?: ArtifactCandidate[];
}

function normalizeDomainTableArtifacts(
  tables: DataDictTable[],
  domain: DomainReducePayload,
): Pick<DomainReducePayload, 'tableMaterialAssessments' | 'tableCandidateArtifacts'> {
  const tableNames = new Set(tables.map((table) => table.name));
  const candidates = (Array.isArray(domain.tableCandidateArtifacts) ? domain.tableCandidateArtifacts : [])
    .filter((candidate) => ['object', 'rule', 'operation', 'metric'].includes(candidate.kind))
    .map((candidate) => ({
      ...candidate,
      sources: (Array.isArray(candidate.sources) ? candidate.sources : []).filter((source) => tableNames.has(source)),
    }))
    .filter((candidate) => candidate.sources.length > 0);
  const submitted = new Map(
    (Array.isArray(domain.tableMaterialAssessments) ? domain.tableMaterialAssessments : [])
      .filter((assessment) => tableNames.has(assessment.source))
      .map((assessment) => [assessment.source, assessment] as const),
  );
  return {
    tableCandidateArtifacts: candidates,
    tableMaterialAssessments: tables.map((table) => {
      const assessment = submitted.get(table.name);
      const coveredKinds = [...new Set(candidates
        .filter((candidate) => candidate.sources.includes(table.name))
        .map((candidate) => candidate.kind))];
      return {
        source: table.name,
        sourceType: 'table' as const,
        suitableFor: coveredKinds,
        rationale: String(assessment?.rationale ?? '').trim().slice(0, 2_000)
          || (coveredKinds.length
            ? '已从该表提取与当前建模目标相关的知识候选。'
            : '已分析该表，当前未形成有充分证据的知识候选。'),
      };
    }),
  };
}

async function reduceDomain(input: {
  llm: LlmDeps;
  projectContext: string;
  tables: DataDictTable[];
  documents: Array<{ filename: string; payload: DocumentAnalysisPayload }>;
  confirmedBrief?: ModelingBrief;
  observe?: (event: unknown) => void;
}): Promise<DomainReducePayload> {
  const capture: { value?: any } = {};
  const submit = {
    name: 'submit_material_analysis_result',
    label: '提交领域与表格资料分析',
    description: '提交领域概览、目标、流程、对象候选，以及表格资料的知识分类和候选。',
    parameters: Type.Object({
      domainOverview: Type.String(),
      candidateObjects: Type.Array(Type.Object({
        name: Type.String(), rationale: Type.String(), sourceTables: Type.Array(Type.String()),
      })),
      candidateGoals: Type.Array(Type.Object({
        title: Type.String(), problem: Type.String(), targetUsers: Type.Array(Type.String()),
        scenarios: Type.Array(Type.String()), successCriteria: Type.Array(Type.String()), rationale: Type.String(),
      }), { maxItems: 3 }),
      candidateProcesses: Type.Array(Type.Object({
        title: Type.String(), trigger: Type.String(), actors: Type.Array(Type.String()),
        inputs: Type.Array(Type.String()), steps: Type.Array(Type.String()), exceptions: Type.Array(Type.String()),
        outputs: Type.Array(Type.String()), handoffs: Type.Array(Type.String()), rationale: Type.String(),
      }), { maxItems: 3 }),
      openQuestions: Type.Array(Type.Object({ topic: Type.String(), why: Type.String() })),
      tableMaterialAssessments: Type.Array(Type.Object({
        source: Type.String(), sourceType: Type.Literal('table'), suitableFor: Type.Array(artifactKind), rationale: Type.String(),
      })),
      tableCandidateArtifacts: Type.Array(Type.Object({
        kind: artifactKind, name: Type.String(), rationale: Type.String(), sources: Type.Array(Type.String()),
        evidence: Type.Optional(Type.String()), readiness,
      })),
    }),
    async execute(_id: string, params: unknown) {
      capture.value = params;
      return { content: [{ type: 'text' as const, text: '领域分析已提交。' }], details: {}, terminate: true };
    },
  };
  const tableSummary = balancedTableSummaries(input.tables, input.llm);
  const documentSummary = balancedDomainDocuments(input.documents, input.llm);
  const goal = input.confirmedBrief ? summarizeModelingBrief(input.confirmedBrief) : '未提供额外建模目标，以材料事实为边界。';
  const agent = makeAgent(
    taskModel(input.llm, 16_384),
    [
      '你是领域资料 Reduce 汇总器。文档已经完成全文分段 Map 和文档级 Reduce，不需要也不得重新读取原文。',
      '基于文档汇总与数据表结构形成领域概览、最多 3 个目标和流程、对象候选及开放问题。',
      'candidateObjects.sourceTables 和表格材料 source 必须精确使用输入中的真实表名。',
      '表格缺少业务口径时标记 needs_clarification，不得根据样例值编造规则阈值。',
      '最后调用 submit_material_analysis_result。',
      `已确认建模目标：${goal}`,
      `项目上下文：${input.projectContext}`,
    ].join('\n'),
    [submit] as any,
  );
  await runAgentCaptured(
    agent,
    `【数据表结构】\n${JSON.stringify(tableSummary)}\n\n【全文文档汇总】\n${JSON.stringify(documentSummary)}`,
    () => Boolean(capture.value),
    '请立即调用 submit_material_analysis_result 提交领域分析。',
    input.observe,
    { timeoutMs: 900_000 },
  );
  if (!capture.value) throw new Error('领域汇总未提交结构化结果');
  const domain = capture.value as DomainReducePayload;
  return { ...domain, ...normalizeDomainTableArtifacts(input.tables, domain) };
}

interface AdaptiveQueueResult<T> {
  values: T[];
  rateLimitCount: number;
}

async function adaptiveMap<T, R>(
  items: T[],
  initialConcurrency: number,
  work: (item: T) => Promise<R>,
): Promise<AdaptiveQueueResult<R>> {
  const values = new Array<R>(items.length);
  const pending = items.map((item, index) => ({ item, index, attempt: 0, readyAt: 0 }));
  let active = 0;
  let limit = Math.max(1, Math.min(8, initialConcurrency));
  let rateLimitCount = 0;
  let fatalError: unknown;
  return new Promise((resolve, reject) => {
    const schedule = () => {
      if (fatalError) {
        if (active === 0) reject(fatalError);
        return;
      }
      if (!pending.length && active === 0) {
        resolve({ values, rateLimitCount });
        return;
      }
      const now = Date.now();
      let launched = false;
      while (active < limit) {
        const position = pending.findIndex((entry) => entry.readyAt <= now);
        if (position < 0) break;
        const entry = pending.splice(position, 1)[0];
        active += 1;
        launched = true;
        void work(entry.item).then((value) => {
          values[entry.index] = value;
        }).catch((error) => {
          if (isQuotaExhausted(error)) {
            fatalError ??= error;
            return;
          }
          if ((isRateLimited(error) || isTransientModelFailure(error)) && entry.attempt < 3) {
            if (isRateLimited(error)) {
              rateLimitCount += 1;
              limit = Math.max(1, Math.floor(limit / 2));
            }
            pending.push({ ...entry, attempt: entry.attempt + 1, readyAt: Date.now() + 750 * 2 ** entry.attempt });
            return;
          }
          fatalError ??= error;
        }).finally(() => {
          active -= 1;
          schedule();
        });
      }
      if (!launched && active === 0 && pending.length) {
        const wait = Math.max(1, Math.min(...pending.map((entry) => entry.readyAt - now).filter((value) => value > 0)));
        setTimeout(schedule, wait);
      }
    };
    schedule();
  });
}

function documentAnalysisToArtifacts(
  filename: string,
  payload: DocumentAnalysisPayload,
): { assessment: MaterialAssessment; candidates: ArtifactCandidate[]; processes: ChunkCandidate[] } {
  const coveredKinds = [...new Set(payload.candidates
    .map((candidate) => candidate.kind)
    .filter((kind): kind is MaterialAssessment['suitableFor'][number] =>
      ['object', 'rule', 'operation', 'metric'].includes(kind)))];
  return {
    assessment: {
      source: filename,
      sourceType: 'document',
      suitableFor: coveredKinds,
      rationale: payload.rationale,
    },
    candidates: payload.candidates.flatMap((candidate) => (
      candidate.kind === 'process' ? [] : [{
        kind: candidate.kind,
        name: candidate.name,
        rationale: candidate.rationale,
        sources: [filename],
        evidence: candidate.evidenceOffsets.length
          ? `原文 offset: ${candidate.evidenceOffsets.join('、')}`
          : undefined,
        readiness: candidate.readiness,
      } as ArtifactCandidate]
    )),
    processes: payload.candidates.filter((candidate) => candidate.kind === 'process'),
  };
}

function coverageFromPlan(plan: DocumentAnalysisPlan): AnalysisResult['documentReadCoverage'] {
  const keys = [...new Set(plan.chunks.map((chunk) => chunk.documentKey))];
  return keys.map((key) => {
    const chunks = plan.chunks.filter((chunk) => chunk.documentKey === key)
      .sort((left, right) => left.startOffset - right.startOffset);
    const first = chunks[0];
    const totalChars = Math.max(...chunks.map((chunk) => chunk.endOffset));
    const ranges = chunks.map((chunk) => [chunk.startOffset, chunk.endOffset] as [number, number]);
    return {
      ...(first.documentId ? { documentId: first.documentId } : {}),
      filename: first.filename,
      revisionFingerprint: first.revisionFingerprint,
      totalChars,
      ranges,
      contiguousEnd: totalChars,
      eofSeen: true,
      complete: true,
    };
  });
}

interface MaterialAnalysisMapReduceInput {
  projectId: string;
  projectContext: string;
  tables: DataDictTable[];
  documents: ReadableDocument[];
  llm: LlmDeps;
  confirmedBrief?: ModelingBrief;
  onProgress?: (progress: MaterialMapReduceProgress) => void;
  observe?: (phase: string) => ((event: unknown) => void);
  concurrency?: number;
  documentOnly?: boolean;
  finalizeAnalysis?: (analysis: AnalysisResult) => AnalysisResult;
}

const inFlightMaterialAnalyses = new Map<string, Promise<MaterialMapReduceResult>>();
const inFlightMaterialChunks = new Map<string, Promise<ChunkAnalysisPayload>>();
const inFlightDocumentReductions = new Map<string, Promise<DocumentAnalysisPayload>>();

function sharedExecution<T>(
  executions: Map<string, Promise<T>>,
  key: string,
  execute: () => Promise<T>,
): Promise<{ value: T; joined: boolean }> {
  const existing = executions.get(key);
  if (existing) return existing.then((value) => ({ value, joined: true }));
  const running = Promise.resolve().then(execute);
  executions.set(key, running);
  void running.finally(() => {
    if (executions.get(key) === running) executions.delete(key);
  }).catch(() => undefined);
  return running.then((value) => ({ value, joined: false }));
}

function chunkExecutionKey(
  projectId: string,
  plan: DocumentAnalysisPlan,
  chunk: DocumentAnalysisChunk,
): string {
  return JSON.stringify([
    projectId, plan.modelId, plan.protocolVersion,
    chunk.documentKey, chunk.revisionFingerprint,
    chunk.startOffset, chunk.endOffset, chunk.contentHash,
  ]);
}

function documentReductionKey(
  projectId: string,
  plan: DocumentAnalysisPlan,
  documentKey: string,
  chunks: MaterialAnalysisChunkRecord[],
): string {
  const payloadHash = createHash('sha256').update(JSON.stringify(chunks.map((chunk) => ({
    index: chunk.chunkIndex,
    start: chunk.startOffset,
    end: chunk.endOffset,
    contentHash: chunk.contentHash,
    payload: chunk.payload,
  })))).digest('hex');
  return JSON.stringify([
    projectId, plan.modelId, plan.protocolVersion,
    documentKey, chunks[0]?.revisionFingerprint ?? '', payloadHash,
  ]);
}

function materialAnalysisInFlightKey(input: MaterialAnalysisMapReduceInput): string {
  const plan = planDocumentAnalysis(input.documents, input.llm);
  return JSON.stringify([
    input.projectId,
    materialSourceSetFingerprint({
      purpose: input.documentOnly ? 'document_facts' : 'analysis',
      tables: input.tables,
      documents: input.documents,
    }),
    materialBriefFingerprint(input.confirmedBrief),
    plan.modelId,
    plan.protocolVersion,
  ]);
}

export function runMaterialAnalysisMapReduce(
  input: MaterialAnalysisMapReduceInput,
): Promise<MaterialMapReduceResult> {
  const key = materialAnalysisInFlightKey(input);
  const existing = inFlightMaterialAnalyses.get(key);
  if (existing) return existing;
  const running = runMaterialAnalysisMapReduceOnce(input);
  inFlightMaterialAnalyses.set(key, running);
  void running.finally(() => {
    if (inFlightMaterialAnalyses.get(key) === running) inFlightMaterialAnalyses.delete(key);
  }).catch(() => undefined);
  return running;
}

async function runMaterialAnalysisMapReduceOnce(
  input: MaterialAnalysisMapReduceInput,
): Promise<MaterialMapReduceResult> {
  const report = (progress: MaterialMapReduceProgress) => {
    try { input.onProgress?.(progress); } catch { /* 进度展示失败不能阻断分析 */ }
  };
  const plan = planDocumentAnalysis(input.documents, input.llm);
  assertCompleteDocumentPlan(plan, input.documents);
  const store = new MaterialAnalysisCheckpointStore();
  const run = store.startOrResume({
    projectId: input.projectId,
    sourceSetFingerprint: materialSourceSetFingerprint({
      purpose: input.documentOnly ? 'document_facts' : 'analysis',
      tables: input.tables,
      documents: input.documents,
    }),
    briefFingerprint: materialBriefFingerprint(input.confirmedBrief),
    plan,
  });
  if (run.status === 'completed' && run.result) {
    return { analysis: run.result as AnalysisResult, runId: run.id, reusedChunks: plan.chunks.length, totalChunks: plan.chunks.length };
  }
  const contentByChunk = new Map(plan.chunks.map((chunk) => [`${chunk.documentKey}\0${chunk.chunkIndex}`, chunk]));
  const before = store.listChunks(run.id);
  let reusedChunks = before.filter((chunk) => chunk.status === 'completed').length;
  store.updateStage(run.id, 'map');

  const reportMapProgress = (title: string, summary?: string) => {
    const completed = store.listChunks(run.id).filter((chunk) => chunk.status === 'completed');
    const coveredChars = uniqueCoveredChars(completed);
    const fraction = plan.totalChars ? coveredChars / plan.totalChars : 1;
    report({ phase: 'document_map', title, summary, percent: 5 + Math.round(fraction * 60) });
  };
  reportMapProgress(reusedChunks ? `复用 ${reusedChunks}/${plan.chunks.length} 个已完成分段` : '正在分析全文分段');

  const pending = before.filter((chunk) => chunk.status !== 'completed');
  try {
    await adaptiveMap(pending, input.concurrency ?? Number(process.env.MODELING_MATERIAL_ANALYSIS_CONCURRENCY ?? 5), async (record) => {
      throwIfCancelled(input.projectId, 'analyze');
      const chunk = contentByChunk.get(`${record.documentKey}\0${record.chunkIndex}`);
      if (!chunk) throw new Error(`分段计划已失效：${record.documentKey}#${record.chunkIndex}`);
      store.beginChunk(run.id, record);
      try {
        const shared = await sharedExecution(
          inFlightMaterialChunks,
          chunkExecutionKey(input.projectId, plan, chunk),
          () => analyzeChunk(
            input.llm,
            chunk,
            input.observe?.(`document_map_${record.documentKey}_${record.chunkIndex}`),
          ),
        );
        const payload = shared.value;
        if (shared.joined) reusedChunks += 1;
        store.completeChunk(run.id, record, payload);
        reportMapProgress(
          `已分析 ${record.filename} 第 ${record.chunkIndex + 1} 段`,
          `${store.listChunks(run.id).filter((item) => item.status === 'completed').length}/${plan.chunks.length} 个分段完成`,
        );
        return payload;
      } catch (error) {
        store.failChunk(run.id, record, error);
        throw error;
      }
    });
  } catch (error) {
    if (isQuotaExhausted(error)) {
      store.pause(run.id, error);
      throw new MaterialAnalysisPausedError(run.id, errorText(error));
    }
    store.fail(run.id, error);
    throw error;
  }

  const chunks = store.listChunks(run.id);
  if (chunks.some((chunk) => chunk.status !== 'completed')) {
    const error = new Error('全文分段分析未全部完成，已保留检查点，请重试');
    store.fail(run.id, error);
    throw error;
  }
  try {
    store.updateStage(run.id, 'document_reduce');
    const documentKeys = [...new Set(chunks.map((chunk) => chunk.documentKey))];
    report({ phase: 'document_reduce', title: '正在汇总各文档的全文分析', percent: 66 });
    await adaptiveMap(documentKeys, input.concurrency ?? Number(process.env.MODELING_MATERIAL_ANALYSIS_CONCURRENCY ?? 5), async (documentKey) => {
      const existing = store.getDocumentSummary(run.id, documentKey);
      if (existing) return existing;
      const items = chunks.filter((chunk) => chunk.documentKey === documentKey)
        .sort((left, right) => left.startOffset - right.startOffset);
      const first = items[0];
      store.beginDocumentSummary(run.id, first);
      try {
        const shared = await sharedExecution(
          inFlightDocumentReductions,
          documentReductionKey(input.projectId, plan, documentKey, items),
          () => reduceDocumentHierarchically(
            input.llm,
            first,
            items,
            input.concurrency ?? Number(process.env.MODELING_MATERIAL_ANALYSIS_CONCURRENCY ?? 5),
            input.observe?.(`document_reduce_${documentKey}`),
          ),
        );
        const payload = shared.value;
        store.completeDocumentSummary(run.id, documentKey, payload);
        return payload;
      } catch (error) {
        store.failDocumentSummary(run.id, documentKey, error);
        throw error;
      }
    });

    const documentSummaries = documentKeys.map((documentKey) => {
      const first = chunks.find((chunk) => chunk.documentKey === documentKey)!;
      const payload = store.getDocumentSummary(run.id, documentKey);
      if (!payload) throw new Error(`文档汇总缺失：${first.filename}`);
      return { filename: first.filename, payload };
    });
    const documentArtifacts = documentSummaries.map((item) => documentAnalysisToArtifacts(item.filename, item.payload));
    if (input.documentOnly) {
      const analysis: AnalysisResult = {
        domainOverview: documentSummaries.map((item) => `${item.filename}：${item.payload.summary}`).join('\n'),
        candidateObjects: [],
        openQuestions: documentSummaries.flatMap((item) => item.payload.openQuestions),
        materialAssessments: documentArtifacts.map((item) => item.assessment),
        candidateArtifacts: documentArtifacts.flatMap((item) => item.candidates),
        documentReadCoverage: coverageFromPlan(plan),
      };
      store.complete(run.id, analysis);
      report({
        phase: 'complete', title: '原始资料全文事实提取完成', percent: 100,
        summary: `文档 ${documentSummaries.length} 份 · 分段 ${plan.chunks.length} 个`,
      });
      return { analysis, runId: run.id, reusedChunks, totalChunks: plan.chunks.length };
    }
    store.updateStage(run.id, 'domain_reduce');
    report({ phase: 'domain_reduce', title: '正在生成领域概览和知识候选', percent: 82 });
    const domain = await reduceDomain({
      llm: input.llm,
      projectContext: input.projectContext,
      tables: input.tables,
      documents: documentSummaries,
      confirmedBrief: input.confirmedBrief,
      observe: input.observe?.('domain_reduce'),
    });
    const analysis: AnalysisResult = {
    domainOverview: String(domain.domainOverview ?? '').trim() || '已完成资料全文分析。',
    ...(input.confirmedBrief ? { modelingBrief: input.confirmedBrief } : {}),
    candidateObjects: Array.isArray(domain.candidateObjects) ? domain.candidateObjects : [],
    openQuestions: [
      ...(Array.isArray(domain.openQuestions) ? domain.openQuestions : []),
      ...documentSummaries.flatMap((item) => item.payload.openQuestions),
    ],
    candidateGoals: Array.isArray(domain.candidateGoals) ? domain.candidateGoals.slice(0, 3) : [],
    candidateProcesses: Array.isArray(domain.candidateProcesses) ? domain.candidateProcesses.slice(0, 3) : [],
    materialAssessments: [
      ...(Array.isArray(domain.tableMaterialAssessments) ? domain.tableMaterialAssessments : []),
      ...documentArtifacts.map((item) => item.assessment),
    ],
    candidateArtifacts: [
      ...(Array.isArray(domain.tableCandidateArtifacts) ? domain.tableCandidateArtifacts : []),
      ...documentArtifacts.flatMap((item) => item.candidates),
    ],
    documentReadCoverage: coverageFromPlan(plan),
    };
    const finalizedAnalysis = input.finalizeAnalysis?.(analysis) ?? analysis;
    store.complete(run.id, finalizedAnalysis);
    report({
      phase: 'material_reduce_complete', title: '建模资料候选汇总完成', percent: 84,
      summary: `文档 ${documentSummaries.length} 份 · 分段 ${plan.chunks.length} 个`,
    });
    return { analysis: finalizedAnalysis, runId: run.id, reusedChunks, totalChunks: plan.chunks.length };
  } catch (error) {
    if (isQuotaExhausted(error)) {
      store.pause(run.id, error);
      throw new MaterialAnalysisPausedError(run.id, errorText(error));
    }
    store.fail(run.id, error);
    throw error;
  }
}

export async function ensureFullDocumentSummaries(input: {
  projectId: string;
  documents: ReadableDocument[];
  llm: LlmDeps;
  onProgress?: (progress: MaterialMapReduceProgress) => void;
  observe?: (phase: string) => ((event: unknown) => void);
}): Promise<{ summaries: Array<{ filename: string; payload: DocumentAnalysisPayload }>; coverage: NonNullable<AnalysisResult['documentReadCoverage']> }> {
  const result = await runMaterialAnalysisMapReduce({
    ...input,
    projectContext: '仅建立原始资料的可复用全文摘要与证据索引。',
    tables: [],
    documentOnly: true,
    concurrency: Number(process.env.MODELING_MATERIAL_ANALYSIS_CONCURRENCY ?? 5),
  });
  const store = new MaterialAnalysisCheckpointStore();
  const chunks = store.listChunks(result.runId);
  const summaries = [...new Set(chunks.map((chunk) => chunk.documentKey))].map((documentKey) => ({
    filename: chunks.find((chunk) => chunk.documentKey === documentKey)!.filename,
    payload: store.getDocumentSummary(result.runId, documentKey)!,
  }));
  return { summaries, coverage: result.analysis.documentReadCoverage ?? [] };
}
