import type { DocumentSourceRef, SourceRef, SpecObject, TableSourceRef } from '../types.js';

export function sourceRefsFor(object: SpecObject): SourceRef[] {
  if (object.sourceRefs?.length) return object.sourceRefs;
  return object.sourceTable ? [{ kind: 'table', table: object.sourceTable }] : [];
}

export function tableSourceFor(object: SpecObject): TableSourceRef | undefined {
  return sourceRefsFor(object).find((source): source is TableSourceRef => source.kind === 'table');
}

export function tableSourcesFor(object: SpecObject): TableSourceRef[] {
  return sourceRefsFor(object).filter((source): source is TableSourceRef => source.kind === 'table');
}

export function documentSourcesFor(object: SpecObject): DocumentSourceRef[] {
  return sourceRefsFor(object).filter((source): source is DocumentSourceRef => source.kind === 'document');
}

export function sourceLabelsFor(object: SpecObject): string[] {
  return sourceRefsFor(object).map((source) => {
    if (source.kind === 'table') return `表格：${source.table}`;
    const anchor = source.anchors?.find((item) => item.heading)?.heading;
    return `文档：${source.filename}${anchor ? ` / ${anchor}` : ''}`;
  });
}
