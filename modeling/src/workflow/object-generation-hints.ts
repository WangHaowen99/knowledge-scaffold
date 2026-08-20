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
  const hints: ObjectGenerationHint[] = (analysis?.candidateObjects?.length
    ? analysis.candidateObjects
    : tables.map((table) => ({
      name: table.name,
      rationale: `来自数据表 ${table.name}`,
      sourceTables: [table.name],
    }))).map((candidate) => ({ ...candidate, sourceTables: [...candidate.sourceTables] }));
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
    // When domain Reduce already selected the modeling scope, chunk/document
    // candidates are evidence only. Promoting every unmatched mention to a new
    // object explodes long-document projects into hundreds of duplicate hints.
    if (analysis?.candidateObjects?.length) continue;
    hints.push({
      name: candidate.name,
      rationale: candidate.rationale,
      sourceTables: candidate.sources.filter((source) => tableNames.has(source)),
      materialEvidence: [materialEvidence(candidate)],
    });
  }
  return hints;
}

/**
 * Domain Reduce candidates define the first-version knowledge scope. Document
 * candidates remain the evidence pool and are used as a fallback for projects
 * without any structured-table domain candidates.
 */
export function buildKnowledgeGenerationCandidates(
  analysis: AnalysisResult | undefined,
  tables: DataDictTable[],
): ArtifactCandidate[] {
  const candidates = (analysis?.candidateArtifacts ?? [])
    .filter((item) => item.kind === 'rule' || item.kind === 'metric');
  const tableNames = new Set(tables.map((table) => table.name));
  const domainCandidates = candidates.filter((candidate) =>
    candidate.sources.some((source) => tableNames.has(source)));
  return domainCandidates.length ? domainCandidates : candidates;
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
