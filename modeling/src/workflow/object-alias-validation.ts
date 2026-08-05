import type { SpecObject } from '../types.js';

const OBJECT_ALIAS_SPLIT_RE = /[,，、;\/；]/u;
const ASCII_RE = /^[\x00-\x7F]*$/;

export type ObjectIdentityKind = 'key' | 'title';

interface ObjectIdentityClaim {
  objectKey: string;
  kind: ObjectIdentityKind;
  value: string;
}

export interface SharedObjectAliasConflict {
  kind: 'shared_alias';
  alias: string;
  objectKeys: string[];
}

export interface ObjectAliasIdentityConflict {
  kind: 'identity_conflict';
  alias: string;
  aliasObjectKeys: string[];
  identityObjectKey: string;
  identityKind: ObjectIdentityKind;
  identityValue: string;
}

export type ObjectAliasConflict = SharedObjectAliasConflict | ObjectAliasIdentityConflict;

/** 与 Consumer normalize_object_identifier 保持一致。 */
export function normalizeObjectIdentifier(value: unknown): string {
  const normalized = String(value ?? '').trim().normalize('NFC');
  return ASCII_RE.test(normalized) ? normalized.toLowerCase() : normalized;
}

function splitObjectAliases(values: readonly string[] | undefined): string[] {
  return (values ?? []).flatMap((value) => String(value ?? '').trim().split(OBJECT_ALIAS_SPLIT_RE))
    .map((value) => value.trim())
    .filter(Boolean);
}

function compareObjectIdentifiers(left: string, right: string): number {
  const normalizedLeft = normalizeObjectIdentifier(left);
  const normalizedRight = normalizeObjectIdentifier(right);
  if (normalizedLeft < normalizedRight) return -1;
  if (normalizedLeft > normalizedRight) return 1;
  return 0;
}

function highestPriorityIdentityClaims(claims: ObjectIdentityClaim[]): ObjectIdentityClaim[] {
  const priority: Record<ObjectIdentityKind, number> = { key: 0, title: 1 };
  const byObject = new Map<string, ObjectIdentityClaim>();
  for (const claim of claims) {
    const current = byObject.get(claim.objectKey);
    if (!current || priority[claim.kind] < priority[current.kind]) {
      byObject.set(claim.objectKey, claim);
    }
  }
  return [...byObject.values()].sort((left, right) => (
    compareObjectIdentifiers(left.objectKey, right.objectKey)
    || priority[left.kind] - priority[right.kind]
  ));
}

export function findObjectAliasConflicts(objects: readonly SpecObject[]): ObjectAliasConflict[] {
  const identityClaims = new Map<string, ObjectIdentityClaim[]>();
  const aliasClaims = new Map<string, Set<string>>();

  for (const object of objects) {
    for (const [kind, value] of [
      ['key', object.key],
      ['title', object.title],
    ] as const) {
      const identifier = normalizeObjectIdentifier(value);
      if (!identifier) continue;
      const claims = identityClaims.get(identifier) ?? [];
      claims.push({ objectKey: object.key, kind, value });
      identityClaims.set(identifier, claims);
    }

    for (const rawAlias of splitObjectAliases(object.aliases)) {
      const alias = normalizeObjectIdentifier(rawAlias);
      if (!alias) continue;
      const objectKeys = aliasClaims.get(alias) ?? new Set<string>();
      objectKeys.add(object.key);
      aliasClaims.set(alias, objectKeys);
    }
  }

  const conflicts: ObjectAliasConflict[] = [];
  for (const [alias, claimedKeys] of aliasClaims) {
    const objectKeys = [...claimedKeys].sort(compareObjectIdentifiers);
    if (objectKeys.length > 1) {
      conflicts.push({ kind: 'shared_alias', alias, objectKeys });
    }

    for (const identity of highestPriorityIdentityClaims(identityClaims.get(alias) ?? [])) {
      if (claimedKeys.has(identity.objectKey)) continue;
      conflicts.push({
        kind: 'identity_conflict',
        alias,
        aliasObjectKeys: objectKeys,
        identityObjectKey: identity.objectKey,
        identityKind: identity.kind,
        identityValue: identity.value,
      });
    }
  }

  return conflicts.sort((left, right) => (
    compareObjectIdentifiers(left.alias, right.alias)
    || left.kind.localeCompare(right.kind)
  ));
}

export function describeObjectAliasConflict(conflict: ObjectAliasConflict): string {
  if (conflict.kind === 'shared_alias') {
    return `对象别名「${conflict.alias}」同时指向多个对象：${conflict.objectKeys.join('、')}；同一知识包内对象别名必须唯一`;
  }
  return `对象别名「${conflict.alias}」（属于对象 ${conflict.aliasObjectKeys.join('、')}）与 ${conflict.identityKind} 为「${conflict.identityValue}」的对象「${conflict.identityObjectKey}」冲突`;
}
