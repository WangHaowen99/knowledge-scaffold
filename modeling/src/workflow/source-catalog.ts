import type {
  AnalysisResult,
  DataDictTable,
  DocumentSourceRef,
  SpecObject,
} from '../types.js';

export type TableReferenceResolution =
  | { status: 'resolved'; tableName: string; matchedBy: 'exact' | 'display' | 'normalized' }
  | { status: 'missing'; reference: string }
  | { status: 'ambiguous'; reference: string; candidates: string[] };

export interface SourceReferenceProblem {
  owner: string;
  reference: string;
  reason: 'missing' | 'ambiguous';
  candidates: string[];
  message: string;
}

export interface SourceCanonicalization<T> {
  value: T;
  problems: SourceReferenceProblem[];
}

export interface SourceCatalog {
  resolveTable(reference: string): TableReferenceResolution;
  canonicalizeAnalysis(analysis: AnalysisResult): SourceCanonicalization<AnalysisResult>;
  canonicalizeObjects(objects: SpecObject[]): SourceCanonicalization<SpecObject[]>;
}

export function createSourceCatalog(
  tables: DataDictTable[],
  documents: Array<{ id: string; filename: string }> = [],
): SourceCatalog {
  const exactNames = new Map<string, string[]>();
  const displayNames = new Map<string, string[]>();
  const normalizedNames = new Map<string, string[]>();
  const normalizedDisplayNames = new Map<string, string[]>();
  const documentsById = new Map(documents.map((document) => [document.id, document]));
  const documentIdsByFilename = new Map<string, string[]>();
  const add = (catalog: Map<string, string[]>, key: string, tableName: string) => {
    const names = catalog.get(key) ?? [];
    if (!names.includes(tableName)) catalog.set(key, [...names, tableName]);
  };
  const normalize = (value: string) => String(value ?? '').trim();
  for (const table of tables) {
    add(exactNames, table.name, table.name);
    const displayName = `${table.name}（${table.rowCount} 行）`;
    add(displayNames, displayName, table.name);
    add(normalizedNames, normalize(table.name), table.name);
    add(normalizedDisplayNames, `${normalize(table.name)}（${table.rowCount} 行）`, table.name);
  }
  for (const document of documents) {
    const ids = documentIdsByFilename.get(document.filename) ?? [];
    if (!ids.includes(document.id)) {
      documentIdsByFilename.set(document.filename, [...ids, document.id]);
    }
  }

  const resolveTable = (reference: string): TableReferenceResolution => {
    const exactCandidates = exactNames.get(reference) ?? [];
    if (exactCandidates.length === 1) {
      return { status: 'resolved', tableName: exactCandidates[0], matchedBy: 'exact' };
    }
    if (exactCandidates.length > 1) {
      return { status: 'ambiguous', reference, candidates: exactCandidates };
    }
    const candidates = displayNames.get(reference) ?? [];
    if (candidates.length === 1) {
      return { status: 'resolved', tableName: candidates[0], matchedBy: 'display' };
    }
    if (candidates.length > 1) {
      return { status: 'ambiguous', reference, candidates };
    }
    const normalizedReference = normalize(reference);
    const normalizedCandidates = normalizedNames.get(normalizedReference) ?? [];
    if (normalizedCandidates.length === 1) {
      return { status: 'resolved', tableName: normalizedCandidates[0], matchedBy: 'normalized' };
    }
    if (normalizedCandidates.length > 1) {
      return { status: 'ambiguous', reference, candidates: normalizedCandidates };
    }
    const normalizedDisplayCandidates = normalizedDisplayNames.get(normalizedReference) ?? [];
    if (normalizedDisplayCandidates.length === 1) {
      return { status: 'resolved', tableName: normalizedDisplayCandidates[0], matchedBy: 'normalized' };
    }
    if (normalizedDisplayCandidates.length > 1) {
      return { status: 'ambiguous', reference, candidates: normalizedDisplayCandidates };
    }
    return { status: 'missing', reference };
  };

  return {
    resolveTable,
    canonicalizeAnalysis(analysis: AnalysisResult): SourceCanonicalization<AnalysisResult> {
      const problems: SourceReferenceProblem[] = [];
      const candidateObjects = analysis.candidateObjects.map((candidate) => ({
        ...candidate,
        sourceTables: candidate.sourceTables.map((reference) => {
          const resolution = resolveTable(reference);
          if (resolution.status === 'resolved') return resolution.tableName;
          const candidates = resolution.status === 'ambiguous' ? resolution.candidates : [];
          problems.push({
            owner: candidate.name,
            reference,
            reason: resolution.status,
            candidates,
            message: resolution.status === 'ambiguous'
              ? `候选对象「${candidate.name}」的来源表「${reference}」存在歧义`
              : `候选对象「${candidate.name}」的来源表「${reference}」不存在`,
          });
          return reference;
        }),
      }));
      return { value: { ...analysis, candidateObjects }, problems };
    },
    canonicalizeObjects(objects: SpecObject[]): SourceCanonicalization<SpecObject[]> {
      const problems: SourceReferenceProblem[] = [];
      const resolveObjectReference = (object: SpecObject, reference: string): string => {
        const resolution = resolveTable(reference);
        if (resolution.status === 'resolved') return resolution.tableName;
        const candidates = resolution.status === 'ambiguous' ? resolution.candidates : [];
        problems.push({
          owner: object.title || object.key,
          reference,
          reason: resolution.status,
          candidates,
          message: resolution.status === 'ambiguous'
            ? `对象「${object.title || object.key}」的来源表「${reference}」存在歧义`
            : `对象「${object.title || object.key}」的来源表「${reference}」不存在`,
        });
        return reference;
      };
      const resolveDocumentReference = (
        object: SpecObject,
        source: DocumentSourceRef,
      ): DocumentSourceRef => {
        if (source.documentId) {
          const document = documentsById.get(source.documentId);
          if (document) return { ...source, filename: document.filename };
          problems.push({
            owner: object.title || object.key,
            reference: source.documentId,
            reason: 'missing',
            candidates: [],
            message: `对象「${object.title || object.key}」引用的资料修订「${source.documentId}」不存在`,
          });
          return source;
        }
        const candidates = documentIdsByFilename.get(source.filename) ?? [];
        if (candidates.length === 1) return { ...source, documentId: candidates[0] };
        problems.push({
          owner: object.title || object.key,
          reference: source.filename,
          reason: candidates.length ? 'ambiguous' : 'missing',
          candidates,
          message: candidates.length
            ? `对象「${object.title || object.key}」引用的文档「${source.filename}」存在多个修订，请明确 documentId`
            : `对象「${object.title || object.key}」引用的文档「${source.filename}」不存在`,
        });
        return source;
      };
      const value = objects.map((object) => ({
        ...object,
        ...(object.sourceTable
          ? { sourceTable: resolveObjectReference(object, object.sourceTable) }
          : {}),
        ...(object.sourceRefs
          ? {
            sourceRefs: object.sourceRefs.map((source) => source.kind === 'table'
              ? { ...source, table: resolveObjectReference(object, source.table) }
              : resolveDocumentReference(object, source)),
          }
          : {}),
      }));
      return { value, problems };
    },
  };
}
