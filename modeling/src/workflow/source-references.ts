import type {
  DocumentSourceRef,
  SourceRef,
  SpecObject,
  TableSourceFieldBinding,
  TableSourceRef,
  TableSourceRole,
} from '../types.js';

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

export function tableSourceRoleFor(object: SpecObject, source: TableSourceRef): TableSourceRole {
  if (source.role) return source.role;
  const index = tableSourcesFor(object).findIndex((candidate) => (
    candidate === source
    || (candidate.documentId === source.documentId && candidate.table === source.table)
  ));
  return index <= 0 ? 'primary' : 'extension';
}

export function primaryTableSourceFor(object: SpecObject): TableSourceRef | undefined {
  const sources = tableSourcesFor(object);
  return sources.find((source) => source.role === 'primary') ?? sources[0];
}

export function tableFieldBindingsFor(
  object: SpecObject,
  source: TableSourceRef,
): TableSourceFieldBinding[] {
  if (source.fieldBindings?.length) return source.fieldBindings;
  return object.fields.map((field) => ({
    fieldKey: field.key,
    sourceColumn: field.sourceColumn || field.key,
  }));
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
