import type { SpecObject } from '../types.js';

const FIELD_ALIAS_SEPARATOR = /[,，、;；]/u;
const EMPTY_FIELD_ALIAS_MARKERS = new Set(['-', '—', '–', '无', 'n/a']);

export type FieldNameClaimKind = 'key' | '中文名' | '别名';

export interface FieldNameClaim {
  kind: FieldNameClaimKind;
  value: string;
  fieldKey: string;
}

export interface FieldNameConflict {
  path: string;
  objectKey: string;
  objectTitle: string;
  displayName: string;
  fieldKeys: string[];
  claims: FieldNameClaim[];
}

export const FIELD_ALIAS_CONFLICT_CODE = 'DATA_SOURCE_PACKAGE_FIELD_ALIAS_CONFLICT';

export class FieldNameConflictError extends Error {
  readonly code = FIELD_ALIAS_CONFLICT_CODE;
  readonly conflicts: FieldNameConflict[];

  constructor(conflicts: FieldNameConflict[]) {
    super(`知识包字段别名预校验失败：${conflicts.map(describeFieldNameConflict).join('；')}`);
    this.name = 'FieldNameConflictError';
    this.conflicts = conflicts;
  }
}

export function fieldNameConflictDetails(conflicts: FieldNameConflict[]): Record<string, unknown> {
  return {
    conflicts: conflicts.slice(0, 100).map((conflict) => ({
      path: conflict.path,
      object_key: conflict.objectKey,
      object_title: conflict.objectTitle,
      display_name: conflict.displayName,
      field_keys: conflict.fieldKeys,
      claims: conflict.claims,
    })),
    truncated: conflicts.length > 100,
  };
}

function trimmed(value: unknown): string {
  return String(value ?? '').trim();
}

function emptyMarker(value: string): boolean {
  const normalized = value.normalize('NFKC').trim().toLocaleLowerCase('en-US');
  return EMPTY_FIELD_ALIAS_MARKERS.has(normalized);
}

export function fieldAliasTokens(value: unknown): string[] {
  const text = trimmed(value);
  if (!text) return [];
  return text
    .split(FIELD_ALIAS_SEPARATOR)
    .map((token) => token.trim())
    .filter((token) => token && !emptyMarker(token));
}

export function normalizeFieldAlias(value: unknown): string | undefined {
  const text = trimmed(value);
  if (!text) return undefined;
  const tokens = text.split(FIELD_ALIAS_SEPARATOR).map((token) => token.trim());
  const aliases = tokens.filter((token) => token && !emptyMarker(token));
  if (!aliases.length) return undefined;
  return aliases.length === tokens.length ? text : aliases.join('、');
}

export function normalizeFieldIdentifier(value: unknown): string {
  const normalized = trimmed(value).normalize('NFC');
  return /^[\x00-\x7F]*$/u.test(normalized)
    ? normalized.toLocaleLowerCase('en-US')
    : normalized;
}

export function normalizeObjectFieldAliases(object: SpecObject): SpecObject {
  return {
    ...object,
    fields: (object.fields ?? []).map((field) => {
      const { alias, ...rest } = field;
      const normalizedAlias = normalizeFieldAlias(alias);
      return normalizedAlias === undefined ? rest : { ...rest, alias: normalizedAlias };
    }),
  };
}

export function findFieldNameConflicts(objects: SpecObject[]): FieldNameConflict[] {
  const conflicts: FieldNameConflict[] = [];
  const claimPriority: Record<FieldNameClaimKind, number> = { key: 0, 中文名: 1, 别名: 2 };

  for (const object of objects) {
    const claimsByName = new Map<string, FieldNameClaim[]>();
    const addClaim = (claim: FieldNameClaim) => {
      const compare = normalizeFieldIdentifier(claim.value);
      if (!compare) return;
      claimsByName.set(compare, [...(claimsByName.get(compare) ?? []), claim]);
    };

    for (const field of object.fields ?? []) {
      addClaim({ kind: 'key', value: field.key, fieldKey: field.key });
      if (field.cnName) addClaim({ kind: '中文名', value: field.cnName, fieldKey: field.key });
      for (const alias of fieldAliasTokens(field.alias)) {
        addClaim({ kind: '别名', value: alias, fieldKey: field.key });
      }
    }

    for (const claims of claimsByName.values()) {
      const fieldKeys = [...new Set(claims.map((claim) => claim.fieldKey))]
        .sort((left, right) => normalizeFieldIdentifier(left).localeCompare(normalizeFieldIdentifier(right)));
      if (fieldKeys.length <= 1) continue;
      const displayClaim = [...claims].sort((left, right) => (
        claimPriority[left.kind] - claimPriority[right.kind]
        || normalizeFieldIdentifier(left.value).localeCompare(normalizeFieldIdentifier(right.value))
      ))[0];
      conflicts.push({
        path: `objects/${object.key}.md`,
        objectKey: object.key,
        objectTitle: object.title || object.key,
        displayName: displayClaim.value,
        fieldKeys,
        claims: [...claims].sort((left, right) => (
          normalizeFieldIdentifier(left.fieldKey).localeCompare(normalizeFieldIdentifier(right.fieldKey))
          || claimPriority[left.kind] - claimPriority[right.kind]
        )),
      });
    }
  }

  return conflicts.sort((left, right) => (
    left.path.localeCompare(right.path)
    || normalizeFieldIdentifier(left.displayName).localeCompare(normalizeFieldIdentifier(right.displayName))
  ));
}

export function describeFieldNameConflict(conflict: FieldNameConflict): string {
  const declarations = conflict.claims
    .map((claim) => `字段“${claim.fieldKey}”的${claim.kind}原值“${claim.value}”`)
    .join('，');
  return `对象文件“${conflict.path}”中的对象“${conflict.objectTitle}”的字段名称“${conflict.displayName}”`
    + `同时指向 [${conflict.fieldKeys.join(', ')}]；原始声明：[${declarations}]`;
}

export function assertNoFieldNameConflicts(objects: SpecObject[]): void {
  const conflicts = findFieldNameConflicts(objects);
  if (conflicts.length) throw new FieldNameConflictError(conflicts);
}
