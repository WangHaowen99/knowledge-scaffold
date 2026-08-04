import type {
  DocumentReadCoverage,
  DocumentSourceReadCoverage,
  SpecObject,
} from '../types.js';
import type { DocumentReadSession, ReadableDocument } from './document-read-session.js';

function continuousFromZero(coverage: DocumentReadCoverage): boolean {
  if (!coverage.complete || !coverage.eofSeen) return false;
  if (coverage.totalChars === 0) return coverage.contiguousEnd === 0;
  const ranges = [...coverage.ranges].sort((left, right) => left[0] - right[0]);
  let end = 0;
  for (const [start, rangeEnd] of ranges) {
    if (start < 0 || rangeEnd < start || start > end) return false;
    end = Math.max(end, rangeEnd);
  }
  return end >= coverage.totalChars && coverage.contiguousEnd >= coverage.totalChars;
}

function matchingDocument(
  coverage: DocumentReadCoverage,
  documents: ReadableDocument[],
): ReadableDocument | undefined {
  if (coverage.documentId) {
    return documents.find((document) => document.id === coverage.documentId);
  }
  const matches = documents.filter((document) => document.filename === coverage.filename);
  return matches.length === 1 ? matches[0] : undefined;
}

function matchesCurrentRevision(
  coverage: DocumentReadCoverage,
  document: ReadableDocument,
): boolean {
  if (coverage.filename !== document.filename) return false;
  if (coverage.documentId && document.id && coverage.documentId !== document.id) return false;
  if (coverage.revisionFingerprint && document.revisionFingerprint
    && coverage.revisionFingerprint !== document.revisionFingerprint) return false;
  return coverage.totalChars === (document.textContent?.length ?? 0);
}

export function captureDocumentReadCoverage(
  session: DocumentReadSession,
  documents: ReadableDocument[],
): DocumentReadCoverage[] {
  const byFilename = new Map(documents.map((document) => [document.filename, document]));
  return session.allLedgers().map((ledger) => {
    const document = byFilename.get(ledger.filename);
    return {
      ...(document?.id ? { documentId: document.id } : {}),
      filename: ledger.filename,
      ...(document?.revisionFingerprint ? { revisionFingerprint: document.revisionFingerprint } : {}),
      totalChars: ledger.totalChars,
      ranges: ledger.ranges.map((range) => [...range] as [number, number]),
      contiguousEnd: ledger.contiguousEnd,
      eofSeen: ledger.eofSeen,
      complete: ledger.complete,
    };
  });
}

export function verifiedDocumentReadCoverage(
  coverage: DocumentReadCoverage[] | undefined,
  documents: ReadableDocument[],
): DocumentReadCoverage[] {
  return (coverage ?? []).filter((item) => {
    const document = matchingDocument(item, documents);
    return Boolean(document && matchesCurrentRevision(item, document) && continuousFromZero(item));
  });
}

function sourceReadCoverage(coverage: DocumentReadCoverage): DocumentSourceReadCoverage {
  return {
    ...(coverage.revisionFingerprint
      ? { revisionFingerprint: coverage.revisionFingerprint }
      : {}),
    totalChars: coverage.totalChars,
    ranges: coverage.ranges.map((range) => [...range] as [number, number]),
    contiguousEnd: coverage.contiguousEnd,
    eofSeen: coverage.eofSeen,
    complete: coverage.complete,
  };
}

export function attachVerifiedDocumentReadCoverage(
  objects: SpecObject[],
  coverage: DocumentReadCoverage[] | undefined,
  documents: ReadableDocument[],
): SpecObject[] {
  const verified = verifiedDocumentReadCoverage(coverage, documents);
  return objects.map((object) => ({
    ...object,
    ...(object.sourceRefs ? {
      sourceRefs: object.sourceRefs.map((source) => {
        if (source.kind !== 'document') return source;
        const { readCoverage: _untrustedCoverage, ...sourceWithoutCoverage } = source;
        const match = verified.find((item) => (
          source.documentId && item.documentId
            ? source.documentId === item.documentId
            : source.filename === item.filename
        ));
        return match
          ? { ...sourceWithoutCoverage, readCoverage: sourceReadCoverage(match) }
          : sourceWithoutCoverage;
      }),
    } : {}),
  }));
}

export function stripUnverifiedDocumentReadCoverage(object: SpecObject): SpecObject {
  if (!object.sourceRefs) return object;
  return {
    ...object,
    sourceRefs: object.sourceRefs.map((source) => {
      if (source.kind !== 'document') return source;
      const { readCoverage: _untrustedCoverage, ...sourceWithoutCoverage } = source;
      return sourceWithoutCoverage;
    }),
  };
}

/** 修改对象时保留服务端已经写入的覆盖事实，同时丢弃模型自行提交的覆盖声明。 */
export function preserveDocumentReadCoverage(
  previous: SpecObject,
  submitted: SpecObject,
): SpecObject {
  if (!submitted.sourceRefs?.length) {
    return previous.sourceRefs?.length
      ? { ...submitted, sourceRefs: previous.sourceRefs }
      : submitted;
  }
  const sanitized = stripUnverifiedDocumentReadCoverage(submitted);
  return {
    ...sanitized,
    sourceRefs: sanitized.sourceRefs!.map((source) => {
      if (source.kind !== 'document') return source;
      const previousSource = previous.sourceRefs?.find((candidate) => (
        candidate.kind === 'document'
        && (source.documentId && candidate.documentId
          ? source.documentId === candidate.documentId
          : source.filename === candidate.filename)
      ));
      return previousSource?.kind === 'document' && previousSource.readCoverage
        ? {
          ...source,
          ...(previousSource.documentId && !source.documentId
            ? { documentId: previousSource.documentId }
            : {}),
          readCoverage: previousSource.readCoverage,
        }
        : source;
    }),
  };
}

export function documentReadCoveragePrompt(coverage: DocumentReadCoverage[]): string {
  if (!coverage.length) return '无可验证的历史全文覆盖记录';
  return coverage.map((item) => (
    `${item.filename}：${item.contiguousEnd}/${item.totalChars} 字符，EOF 已确认，完整覆盖`
  )).join('；');
}
