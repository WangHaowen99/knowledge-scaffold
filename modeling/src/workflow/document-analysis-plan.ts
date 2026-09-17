import { createHash } from 'node:crypto';
import type { LlmDeps } from '../llm/provider.js';
import type { ReadableDocument } from './document-read-session.js';

export const MATERIAL_ANALYSIS_PROTOCOL_VERSION = 'map-reduce-v1';
export const MATERIAL_ANALYSIS_SPLITTER_VERSION = 'section-token-v1';
// 产品事实改为显式、可校验的捕获契约，仅含摘要的旧检查点不得复用。
const MATERIAL_ANALYSIS_PROMPT_VERSION = 'facts-inventory-v3';

export interface DocumentAnalysisChunk {
  documentKey: string;
  documentId?: string;
  filename: string;
  revisionFingerprint: string;
  chunkIndex: number;
  startOffset: number;
  endOffset: number;
  contentHash: string;
  content: string;
}

export interface DocumentAnalysisPlan {
  protocolVersion: string;
  splitterVersion: string;
  modelId: string;
  contextWindow: number;
  outputReserveTokens: number;
  targetChunkTokens: number;
  overlapTokens: number;
  totalChars: number;
  chunks: DocumentAnalysisChunk[];
}

export interface DocumentAnalysisPlanOptions {
  promptTokens?: number;
  outputReserveTokens?: number;
  overlapTokens?: number;
}

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

export function sha256(value: string): string {
  return createHash('sha256').update(value).digest('hex');
}

/**
 * 无模型 tokenizer 时使用保守估算：中日韩字符按一个 token，ASCII 单词和标点
 * 约四字符一个 token。规划结果还保留 15% 上下文余量，宁可多一个分段也不越窗。
 */
export function estimateTextTokens(value: string): number {
  let cjk = 0;
  let other = 0;
  for (const character of value) {
    if (/\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}/u.test(character)) cjk += 1;
    else other += 1;
  }
  return cjk + Math.ceil(other / 4);
}

export function dynamicChunkTokenBudget(
  llm: Pick<LlmDeps, 'model'>,
  options: DocumentAnalysisPlanOptions = {},
): { contextWindow: number; outputReserveTokens: number; targetChunkTokens: number; overlapTokens: number } {
  const contextWindow = Math.max(8_192, Math.floor(Number(llm.model?.contextWindow) || 128_000));
  const configuredOutput = Math.max(1_024, Math.floor(Number(llm.model?.maxTokens) || 8_192));
  const outputReserveTokens = Math.min(
    options.outputReserveTokens ?? configuredOutput,
    12_288,
    Math.max(1_024, Math.floor(contextWindow * 0.20)),
  );
  const promptTokens = Math.min(
    Math.max(1_024, Math.floor(options.promptTokens ?? 4_096)),
    Math.max(1_024, Math.floor(contextWindow * 0.25)),
  );
  const safetyMargin = Math.floor(contextWindow * 0.15);
  const available = Math.max(1_024, contextWindow - outputReserveTokens - promptTokens - safetyMargin);
  const targetChunkTokens = Math.min(available, clamp(Math.floor(available * 0.25), 2_000, 16_000));
  const overlapTokens = clamp(Math.floor(options.overlapTokens ?? 384), 128, 512);
  return { contextWindow, outputReserveTokens, targetChunkTokens, overlapTokens };
}

function revisionFingerprint(document: ReadableDocument): string {
  return document.revisionFingerprint
    ?? `sha256:${sha256(document.textContent ?? '')}`;
}

function documentKey(document: ReadableDocument): string {
  return document.id?.trim() || `filename:${document.filename}`;
}

function modelFingerprint(llm: Pick<LlmDeps, 'model'>): string {
  return `model:${sha256(JSON.stringify({
    id: llm.model?.id,
    name: llm.model?.name,
    provider: llm.model?.provider,
    api: llm.model?.api,
    baseUrl: llm.model?.baseUrl,
  })).slice(0, 24)}`;
}

function boundaryScore(text: string, index: number): number {
  const before = text.slice(Math.max(0, index - 3), index);
  const after = text.slice(index, Math.min(text.length, index + 3));
  if (/\n#{1,6}\s|\n第[^\n]{0,24}[章节篇]\s*/u.test(after)) return 100;
  if (/\n\s*\n/u.test(`${before}${after}`)) return 80;
  if (/\n/u.test(`${before}${after}`)) return 60;
  if (/[。！？；.!?;]\s*$/u.test(before)) return 40;
  return 0;
}

function offsetForTokens(text: string, start: number, targetTokens: number): number {
  let tokens = 0;
  let index = start;
  for (; index < text.length && tokens < targetTokens; index += 1) {
    const character = text[index];
    tokens += /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}/u.test(character)
      ? 1
      : 0.25;
  }
  return Math.max(start + 1, Math.min(text.length, index));
}

function naturalEnd(text: string, start: number, target: number): number {
  if (target >= text.length) return text.length;
  const radius = Math.max(256, Math.floor((target - start) * 0.20));
  const from = Math.max(start + 1, target - radius);
  const to = Math.min(text.length, target + radius);
  let best = target;
  let bestScore = -1;
  for (let index = from; index <= to; index += 1) {
    const score = boundaryScore(text, index) * 10 - Math.abs(index - target) / Math.max(1, radius);
    if (score > bestScore) {
      best = index;
      bestScore = score;
    }
  }
  return Math.max(start + 1, best);
}

export function planDocumentAnalysis(
  documents: ReadableDocument[],
  llm: Pick<LlmDeps, 'model'>,
  options: DocumentAnalysisPlanOptions = {},
): DocumentAnalysisPlan {
  const budget = dynamicChunkTokenBudget(llm, options);
  const chunks: DocumentAnalysisChunk[] = [];
  const seenDocumentKeys = new Set<string>();
  let totalChars = 0;
  for (const document of documents) {
    const key = documentKey(document);
    if (seenDocumentKeys.has(key)) {
      throw new Error(`文档标识重复，无法安全建立分析检查点：${document.filename}`);
    }
    seenDocumentKeys.add(key);
    const body = document.textContent ?? '';
    totalChars += body.length;
    if (!body.length) continue;
    let start = 0;
    let chunkIndex = 0;
    while (start < body.length) {
      const target = offsetForTokens(body, start, budget.targetChunkTokens);
      const end = naturalEnd(body, start, target);
      const content = body.slice(start, end);
      chunks.push({
        documentKey: key,
        ...(document.id ? { documentId: document.id } : {}),
        filename: document.filename,
        revisionFingerprint: revisionFingerprint(document),
        chunkIndex,
        startOffset: start,
        endOffset: end,
        contentHash: sha256(content),
        content,
      });
      if (end >= body.length) break;
      const overlapStart = offsetForTokens(body, start, Math.max(1, estimateTextTokens(content) - budget.overlapTokens));
      start = Math.min(end - 1, Math.max(start + 1, overlapStart));
      chunkIndex += 1;
    }
  }
  return {
    protocolVersion: [
      MATERIAL_ANALYSIS_PROTOCOL_VERSION,
      MATERIAL_ANALYSIS_PROMPT_VERSION,
      MATERIAL_ANALYSIS_SPLITTER_VERSION,
      budget.targetChunkTokens,
      budget.overlapTokens,
    ].join(':'),
    splitterVersion: MATERIAL_ANALYSIS_SPLITTER_VERSION,
    modelId: modelFingerprint(llm),
    ...budget,
    totalChars,
    chunks,
  };
}

export function uniqueCoveredChars(chunks: Array<Pick<DocumentAnalysisChunk, 'documentKey' | 'startOffset' | 'endOffset'>>): number {
  const byDocument = new Map<string, Array<[number, number]>>();
  for (const chunk of chunks) {
    const ranges = byDocument.get(chunk.documentKey) ?? [];
    ranges.push([chunk.startOffset, chunk.endOffset]);
    byDocument.set(chunk.documentKey, ranges);
  }
  let total = 0;
  for (const ranges of byDocument.values()) {
    ranges.sort((left, right) => left[0] - right[0]);
    let start = -1;
    let end = -1;
    for (const range of ranges) {
      if (start < 0) [start, end] = range;
      else if (range[0] <= end) end = Math.max(end, range[1]);
      else {
        total += end - start;
        [start, end] = range;
      }
    }
    if (start >= 0) total += end - start;
  }
  return total;
}

export function assertCompleteDocumentPlan(plan: DocumentAnalysisPlan, documents: ReadableDocument[]): void {
  for (const document of documents) {
    const length = document.textContent?.length ?? 0;
    if (!length) continue;
    const chunks = plan.chunks.filter((chunk) => chunk.documentKey === documentKey(document));
    if (!chunks.length || chunks[0].startOffset !== 0 || Math.max(...chunks.map((chunk) => chunk.endOffset)) !== length) {
      throw new Error(`文档分段未完整覆盖：${document.filename}`);
    }
    const covered = uniqueCoveredChars(chunks);
    if (covered !== length) throw new Error(`文档分段存在空洞：${document.filename} ${covered}/${length}`);
  }
}

export function serializableDocumentAnalysisPlan(plan: DocumentAnalysisPlan): Omit<DocumentAnalysisPlan, 'chunks'> & {
  chunks: Array<Omit<DocumentAnalysisChunk, 'content'>>;
} {
  return { ...plan, chunks: plan.chunks.map(({ content: _content, ...chunk }) => chunk) };
}
