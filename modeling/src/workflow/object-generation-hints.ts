import type { AnalysisResult, ArtifactCandidate, DataDictTable, SpecObject } from '../types.js';
import type { ReadableDocument } from './document-read-session.js';

export interface ObjectMaterialEvidence {
  rationale: string;
  sources: string[];
  evidence?: string;
  readiness: ArtifactCandidate['readiness'];
}

export interface ObjectGenerationHint {
  name: string;
  rationale: string;
  sourceTables: string[];
  materialEvidence?: ObjectMaterialEvidence[];
}

function normalizedName(value: string): string {
  return value.normalize('NFKC').toLocaleLowerCase('zh-CN').replace(/[^\p{L}\p{N}]/gu, '');
}

function matchScore(left: string, right: string): number {
  const normalizedLeft = normalizedName(left);
  const normalizedRight = normalizedName(right);
  if (!normalizedLeft || !normalizedRight) return -1;
  if (normalizedLeft === normalizedRight) return 10_000;
  const shorter = Math.min(normalizedLeft.length, normalizedRight.length);
  if (shorter < 4) return -1;
  if (!normalizedLeft.includes(normalizedRight) && !normalizedRight.includes(normalizedLeft)) return -1;
  return 1_000 - Math.abs(normalizedLeft.length - normalizedRight.length);
}

function materialEvidence(candidate: ArtifactCandidate): ObjectMaterialEvidence {
  return {
    rationale: candidate.rationale,
    sources: [...candidate.sources],
    ...(candidate.evidence ? { evidence: candidate.evidence } : {}),
    readiness: candidate.readiness,
  };
}

/**
 * 初步对象候选负责建模范围，材料候选负责正文证据。两者在分析阶段分开提交，
 * 生成对象前必须重新合并，否则文档对象只会拿到名称和一段浅层 rationale。
 */
export function buildObjectGenerationHints(
  analysis: AnalysisResult | undefined,
  tables: DataDictTable[],
): ObjectGenerationHint[] {
  // Excel/CSV 已经给出可执行的物理 Schema 时，对象集合以 Sheet 为准。
  // 文档候选只能为这些对象补证据，不能再扩张对象目录；否则几十张 Sheet
  // 会叠加出几百个语义对象，既拖慢生成，也使后续数据绑定不可执行。
  const hasUploadedStructuredSheet = tables.some((table) => Boolean(table.documentId));
  if (hasUploadedStructuredSheet) return [];
  // 物理 Sheet 由服务端确定性生成。Agent 只负责没有物理表绑定的逻辑对象，
  // 避免领域 Reduce 把几十张 Sheet 聚合成一个无法执行的数据对象。
  const hints: ObjectGenerationHint[] = (analysis?.candidateObjects ?? [])
    .filter((candidate) => candidate.sourceTables.length === 0)
    .map((candidate) => ({ ...candidate, sourceTables: [] }));
  const tableNames = new Set(tables.map((table) => table.name));

  for (const candidate of analysis?.candidateArtifacts?.filter((item) => item.kind === 'object') ?? []) {
    const matched = hints
      .map((hint, index) => ({ index, score: matchScore(hint.name, candidate.name) }))
      .filter((item) => item.score >= 0)
      .sort((left, right) => right.score - left.score)[0];
    if (matched) {
      const hint = hints[matched.index];
      hint.materialEvidence = [...(hint.materialEvidence ?? []), materialEvidence(candidate)];
      continue;
    }
    // 表格对象已经确定性生成；绑定真实 Sheet 的分段候选只用于提供证据。
    if (candidate.sources.some((source) => tableNames.has(source))) continue;
    if (candidate.readiness !== 'extractable') continue;
    hints.push({
      name: candidate.name,
      rationale: candidate.rationale,
      sourceTables: [],
      materialEvidence: [materialEvidence(candidate)],
    });
  }
  return hints;
}

/**
 * Excel/CSV 只决定物理对象边界，不应把文档规则和指标排除在知识生成之外。
 * 规则与指标可以引用 Sheet 字段，也可以引用产品手册中的业务口径；两者
 * 在生成前由候选处置计划和批次质量门禁分别约束。只有需要补充业务口径的
 * 候选留给专家，不把可提取候选静默延期到“增量建模”。
 */
export function buildKnowledgeGenerationCandidates(
  analysis: AnalysisResult | undefined,
  tables: DataDictTable[],
): ArtifactCandidate[] {
  void tables;
  return (analysis?.candidateArtifacts ?? [])
    .filter((item) => item.kind === 'rule' || item.kind === 'metric');
}

/** 文档来源由分析证据确定，不能因为生成模型漏填 sourceRefs 就丢失。 */
export function attachObjectMaterialSources(
  objects: SpecObject[],
  hints: Array<ObjectGenerationHint & { id: string }>,
  documents: ReadableDocument[],
): SpecObject[] {
  const documentsByFilename = new Map(documents.map((document) => [document.filename, document]));
  const hintsById = new Map(hints.map((hint) => [hint.id, hint]));
  return objects.map((object) => {
    const hint = object.candidateId ? hintsById.get(object.candidateId) : undefined;
    const materialDocuments = [...new Set((hint?.materialEvidence ?? [])
      .flatMap((evidence) => evidence.sources)
      .map((source) => documentsByFilename.get(source))
      .filter((document): document is ReadableDocument => Boolean(document)))];
    if (!materialDocuments.length) return object;
    const sourceRefs = [...(object.sourceRefs ?? [])];
    for (const document of materialDocuments) {
      const exists = sourceRefs.some((source) => source.kind === 'document' && (
        document.id && source.documentId
          ? document.id === source.documentId
          : document.filename === source.filename
      ));
      if (!exists) {
        sourceRefs.push({
          kind: 'document',
          ...(document.id ? { documentId: document.id } : {}),
          filename: document.filename,
        });
      }
    }
    return { ...object, sourceRefs };
  });
}
