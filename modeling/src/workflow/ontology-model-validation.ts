import type { PackageSpec, SpecField, SpecObject, SpecRelation } from '../types.js';
import { validateKey } from './keys.js';
import { validateSpecIntegrity } from './spec-validation.js';

export type OntologyStudioKind = 'object' | 'operation' | 'rule' | 'metric';

export interface OntologyRelationRule {
  kind: string;
  targetKinds: readonly OntologyStudioKind[];
}

export interface NormalizedOntologyRelation {
  kind: string;
  targetKind: OntologyStudioKind;
  targetKey: string;
  label?: string;
  description?: string;
  evidence?: string[];
}

export interface OntologyValidationIssue {
  code: string;
  level: 'error' | 'warning';
  path: string;
  message: string;
  suggestion?: string;
}

export interface NormalizeStudioRelationsOptions {
  maxListItems?: number;
  preserveEvidence?: boolean;
}

export interface NormalizeStudioRelationsResult {
  relations?: NormalizedOntologyRelation[];
  issues: OntologyValidationIssue[];
}

export interface NormalizeObjectMetadataResult {
  changes?: Partial<Pick<SpecObject, 'title' | 'description' | 'aliases' | 'queryBoundary'>>;
  issues: OntologyValidationIssue[];
}

export interface NormalizeObjectFieldsOptions {
  maxListItems?: number;
  maxTextLength?: number;
}

export interface NormalizeObjectFieldsResult {
  fields?: SpecField[];
  issues: OntologyValidationIssue[];
}

export interface RelationTargetValidationInput {
  path: string;
  relations: readonly NormalizedOntologyRelation[];
}

const VALID_KINDS = new Set<OntologyStudioKind>(['object', 'operation', 'rule', 'metric']);
const DEFAULT_MAX_LIST_ITEMS = 500;
const DEFAULT_MAX_TEXT_LENGTH = 50_000;

export function normalizeObjectMetadataPatch(
  value: unknown,
  path: string,
  options: NormalizeObjectFieldsOptions = {},
): NormalizeObjectMetadataResult {
  const issues: OntologyValidationIssue[] = [];
  if (!isRecord(value)) {
    issues.push(issue('OBJECT_METADATA_NOT_OBJECT', path, 'changes 必须是对象'));
    return { issues };
  }
  const maxTextLength = options.maxTextLength ?? DEFAULT_MAX_TEXT_LENGTH;
  const changes: Partial<Pick<SpecObject, 'title' | 'description' | 'aliases' | 'queryBoundary'>> = {};
  for (const [key, raw] of Object.entries(value)) {
    const fieldPath = `${path}.${key}`;
    if (key === 'title' || key === 'description') {
      const normalized = normalizeTextValue(raw, fieldPath, true, maxTextLength, issues);
      if (normalized !== undefined) changes[key] = normalized;
      continue;
    }
    if (key === 'aliases') {
      const normalized = normalizeStringArray(raw, fieldPath, issues, options);
      if (normalized !== undefined) changes.aliases = normalized;
      continue;
    }
    if (key === 'queryBoundary') {
      const normalized = normalizeTextValue(raw, fieldPath, false, maxTextLength, issues);
      if (normalized !== undefined) changes.queryBoundary = normalized;
      continue;
    }
    issues.push(issue('OBJECT_METADATA_UNKNOWN_FIELD', fieldPath, '未知字段'));
  }
  if (!Object.keys(changes).length && issues.length === 0) {
    issues.push(issue('EMPTY_CHANGES', path, '至少提供一个可修改字段'));
  }
  return { changes, issues };
}

export function normalizeObjectFieldsTable(
  value: unknown,
  path: string,
  options: NormalizeObjectFieldsOptions = {},
): NormalizeObjectFieldsResult {
  const issues: OntologyValidationIssue[] = [];
  if (!Array.isArray(value)) {
    issues.push(issue('FIELDS_NOT_ARRAY', path, 'fields 必须是数组'));
    return { issues };
  }
  const maxListItems = options.maxListItems ?? DEFAULT_MAX_LIST_ITEMS;
  const maxTextLength = options.maxTextLength ?? DEFAULT_MAX_TEXT_LENGTH;
  if (value.length > maxListItems) {
    issues.push(issue('FIELDS_TOO_MANY_ITEMS', path, `最多包含 ${maxListItems} 项`));
  }
  const fields: SpecField[] = [];
  const seen = new Set<string>();
  value.forEach((item, index) => {
    const itemPath = `${path}[${index}]`;
    if (!isRecord(item)) {
      issues.push(issue('FIELD_ITEM_NOT_OBJECT', itemPath, '必须是对象'));
      return;
    }
    const field: SpecField = { key: '' };
    const rawKey = String(item.key ?? '').trim();
    if (!rawKey) {
      issues.push(issue('FIELD_KEY_REQUIRED', `${itemPath}.key`, '不能为空'));
    } else {
      try {
        field.key = validateKey(rawKey, '字段');
      } catch (error) {
        issues.push(issue('FIELD_KEY_INVALID', `${itemPath}.key`, errorMessage(error)));
      }
    }
    const cnName = normalizeOptionalText(item.cnName, `${itemPath}.cnName`, maxTextLength, issues);
    if (cnName !== undefined) field.cnName = cnName;
    const type = normalizeOptionalText(item.type, `${itemPath}.type`, maxTextLength, issues);
    if (type !== undefined) field.type = type;
    if (item.required !== undefined) {
      if (typeof item.required !== 'boolean') {
        issues.push(issue('FIELD_REQUIRED_INVALID', `${itemPath}.required`, '必须是布尔值'));
      } else {
        field.required = item.required;
      }
    }
    const note = normalizeOptionalText(item.note, `${itemPath}.note`, maxTextLength, issues);
    if (note !== undefined) field.note = note;
    const alias = normalizeOptionalText(item.alias, `${itemPath}.alias`, maxTextLength, issues);
    if (alias !== undefined) field.alias = alias;
    const sourceColumn = normalizeOptionalText(item.sourceColumn, `${itemPath}.sourceColumn`, maxTextLength, issues);
    if (sourceColumn !== undefined) field.sourceColumn = sourceColumn;
    const valueBindingRef = normalizeOptionalText(item.valueBindingRef, `${itemPath}.valueBindingRef`, maxTextLength, issues);
    if (valueBindingRef !== undefined) field.valueBindingRef = valueBindingRef;
    for (const key of Object.keys(item)) {
      if (!['key', 'cnName', 'type', 'required', 'note', 'alias', 'sourceColumn', 'valueBindingRef'].includes(key)) {
        issues.push(issue('FIELD_UNKNOWN_FIELD', `${itemPath}.${key}`, '未知字段'));
      }
    }
    if (!field.key) return;
    if (seen.has(field.key)) {
      issues.push(issue('FIELD_KEY_DUPLICATE', `${itemPath}.key`, '字段 key 重复'));
      return;
    }
    seen.add(field.key);
    fields.push(field);
  });
  if (!fields.length && issues.length === 0) {
    issues.push(issue('FIELDS_EMPTY', path, '至少需要一个字段'));
  }
  return { fields, issues };
}

export function normalizeStudioRelations(
  sourceKind: OntologyStudioKind,
  value: unknown,
  path: string,
  legalRelations: readonly OntologyRelationRule[],
  options: NormalizeStudioRelationsOptions = {},
): NormalizeStudioRelationsResult {
  const issues: OntologyValidationIssue[] = [];
  if (!Array.isArray(value)) {
    issues.push(issue('RELATIONS_NOT_ARRAY', path, 'relations 必须是数组'));
    return { issues };
  }
  const maxListItems = options.maxListItems ?? DEFAULT_MAX_LIST_ITEMS;
  if (value.length > maxListItems) {
    issues.push(issue('RELATIONS_TOO_MANY_ITEMS', path, `最多包含 ${maxListItems} 项`));
  }
  const legal = new Map<string, readonly OntologyStudioKind[]>(
    legalRelations.map((item) => [item.kind, item.targetKinds]),
  );
  const relations: NormalizedOntologyRelation[] = [];
  value.forEach((item, index) => {
    const itemPath = `${path}[${index}]`;
    if (!isRecord(item)) {
      issues.push(issue('RELATION_ITEM_NOT_OBJECT', itemPath, '必须是对象'));
      return;
    }
    const kind = String(item.kind ?? '').trim();
    const targetId = String(item.targetId ?? '').trim();
    const parsed = parseElementId(targetId);
    const allowedTargets = legal.get(kind);
    /* 只说「不支持」而不说支持什么，调用方只能靠猜。这里三个接口对同一件事用了三套叫法：
       推荐接口返回 relation: "belongs_to"，写入接口要 kind + label，业务谓词归 label。
       实测助手会反复试 belongs_to、"归属" 等值，原地打转数轮后放弃整批任务，所以报错必须
       同时给出合法值和「业务谓词写哪里」。合法值从 legalRelations 生成，不手写平行清单。 */
    if (!allowedTargets) {
      const allowedKinds = [...legal.keys()];
      issues.push(issue('RELATION_KIND_INVALID', `${itemPath}.kind`,
        `不支持的关系类型${kind ? ` ${kind}` : ''}；kind 只接受 ${allowedKinds.join('、')}`
        + '，业务谓词（例如「归属于」）写进 label，不要写进 kind'));
    }
    if (!parsed) {
      issues.push(issue('RELATION_TARGET_ID_INVALID', `${itemPath}.targetId`,
        `targetId 必须是 kind:key，例如 ${(legalRelations[0]?.targetKinds[0] ?? 'object')}:对象名`));
    } else if (allowedTargets && !(allowedTargets as readonly string[]).includes(parsed.kind)) {
      issues.push(issue('RELATION_TARGET_KIND_MISMATCH', `${itemPath}.targetId`,
        `目标知识类型与关系类型不匹配；${kind} 的目标只能是 ${(allowedTargets as readonly string[]).join('、')}`));
    }
    if (item.label !== undefined && typeof item.label !== 'string') {
      issues.push(issue('RELATION_LABEL_INVALID', `${itemPath}.label`, '必须是文本'));
    }
    if (item.description !== undefined && typeof item.description !== 'string') {
      issues.push(issue('RELATION_DESCRIPTION_INVALID', `${itemPath}.description`, '必须是文本'));
    }
    for (const key of Object.keys(item)) {
      const allowedFields = ['kind', 'targetId', 'label', 'description',
        ...(options.preserveEvidence ? ['evidence'] : [])];
      if (!allowedFields.includes(key)) {
        issues.push(issue('RELATION_UNKNOWN_FIELD', `${itemPath}.${key}`,
          `未知字段；关系只接受 ${allowedFields.join('、')}`));
      }
    }
    if (parsed && allowedTargets) {
      relations.push({
        kind,
        targetKind: parsed.kind,
        targetKey: parsed.key,
        ...(typeof item.label === 'string' && item.label.trim() ? { label: item.label.trim() } : {}),
        ...(typeof item.description === 'string' && item.description.trim()
          ? { description: item.description.trim() }
          : {}),
        ...(options.preserveEvidence && Array.isArray(item.evidence)
          ? { evidence: item.evidence.map(String) }
          : {}),
      });
    }
  });
  void sourceKind;
  return { relations, issues };
}

export function validateOntologyRelationTargets(
  spec: PackageSpec,
  changes: readonly RelationTargetValidationInput[],
): OntologyValidationIssue[] {
  const existing = new Set([
    ...spec.objects.map((item) => elementId('object', item.key)),
    ...spec.operations.map((item) => elementId('operation', item.key)),
    ...(spec.rules ?? []).map((item) => elementId('rule', item.key)),
    ...(spec.metrics ?? []).map((item) => elementId('metric', item.key)),
  ]);
  const issues: OntologyValidationIssue[] = [];
  for (const change of changes) {
    change.relations.forEach((relation, index) => {
      if (!existing.has(elementId(relation.targetKind, relation.targetKey))) {
        issues.push(issue(
          'RELATION_TARGET_NOT_FOUND',
          `${change.path}[${index}].targetId`,
          '关系目标不存在',
        ));
      }
    });
  }
  return issues;
}

export function listSpecIntegrityProblems(spec: PackageSpec): string[] {
  try {
    validateSpecIntegrity(spec, [], undefined, { validateScriptExecutionContract: false });
    return [];
  } catch (error) {
    const message = errorMessage(error).trim();
    const marker = '知识包规格不完整：';
    const detail = message.startsWith(marker) ? message.slice(marker.length) : message;
    const problems = detail.split('\n')
      .map((line) => line.trim().replace(/^-\s*/u, ''))
      .filter(Boolean);
    return (problems.length ? problems : [message]).sort();
  }
}

export function introducedSpecIntegrityProblems(
  spec: PackageSpec,
  previousProblems: readonly string[],
): string[] {
  return multisetDifference(listSpecIntegrityProblems(spec), [...previousProblems]);
}

export function applyValidationIssues(
  target: Record<string, string>,
  issues: readonly OntologyValidationIssue[],
): void {
  for (const current of issues) {
    if (current.level !== 'error') continue;
    if (target[current.path]) continue;
    target[current.path] = current.message;
  }
}

export function toSpecObjectRelations(relations: readonly NormalizedOntologyRelation[]): SpecRelation[] {
  return relations.map((relation) => ({
    relation: relation.label || '关联',
    target: relation.targetKey,
    ...(relation.description ? { note: relation.description } : {}),
    ...(relation.evidence?.length ? { evidence: [...relation.evidence] } : {}),
  }));
}

function multisetDifference(current: string[], previous: string[]): string[] {
  const previousCounts = new Map<string, number>();
  for (const problem of previous) {
    previousCounts.set(problem, (previousCounts.get(problem) ?? 0) + 1);
  }
  return current.filter((problem) => {
    const remaining = previousCounts.get(problem) ?? 0;
    if (!remaining) return true;
    previousCounts.set(problem, remaining - 1);
    return false;
  });
}

function issue(code: string, path: string, message: string): OntologyValidationIssue {
  return { code, level: 'error', path, message };
}

function normalizeTextValue(
  value: unknown,
  path: string,
  required: boolean,
  maxTextLength: number,
  issues: OntologyValidationIssue[],
): string | undefined {
  if (typeof value !== 'string') {
    issues.push(issue('TEXT_INVALID', path, '必须是文本'));
    return undefined;
  }
  const text = value.trim();
  if (required && !text) {
    issues.push(issue('TEXT_REQUIRED', path, '必填文本不能为空'));
  }
  if (text.length > maxTextLength) {
    issues.push(issue('TEXT_TOO_LONG', path, `不能超过 ${maxTextLength} 字符`));
  }
  return text;
}

function normalizeOptionalText(
  value: unknown,
  path: string,
  maxTextLength: number,
  issues: OntologyValidationIssue[],
): string | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string') {
    issues.push(issue('TEXT_INVALID', path, '必须是文本'));
    return undefined;
  }
  const text = value.trim();
  if (!text) return undefined;
  if (text.length > maxTextLength) {
    issues.push(issue('TEXT_TOO_LONG', path, `不能超过 ${maxTextLength} 字符`));
  }
  return text;
}

function normalizeStringArray(
  value: unknown,
  path: string,
  issues: OntologyValidationIssue[],
  options: NormalizeObjectFieldsOptions = {},
): string[] | undefined {
  if (!Array.isArray(value)) {
    issues.push(issue('STRING_ARRAY_INVALID', path, '必须是文本数组'));
    return undefined;
  }
  const maxListItems = options.maxListItems ?? DEFAULT_MAX_LIST_ITEMS;
  const maxTextLength = options.maxTextLength ?? DEFAULT_MAX_TEXT_LENGTH;
  if (value.length > maxListItems) {
    issues.push(issue('STRING_ARRAY_TOO_MANY_ITEMS', path, `最多包含 ${maxListItems} 项`));
  }
  const result: string[] = [];
  value.forEach((item, index) => {
    if (typeof item !== 'string') {
      issues.push(issue('STRING_ARRAY_ITEM_INVALID', `${path}[${index}]`, '不能为空'));
      return;
    }
    const text = item.trim();
    if (!text) {
      issues.push(issue('STRING_ARRAY_ITEM_INVALID', `${path}[${index}]`, '不能为空'));
      return;
    }
    if (text.length > maxTextLength) {
      issues.push(issue('TEXT_TOO_LONG', `${path}[${index}]`, `不能超过 ${maxTextLength} 字符`));
      return;
    }
    result.push(text);
  });
  return result;
}

function parseElementId(value: string): { kind: OntologyStudioKind; key: string } | null {
  const separator = value.indexOf(':');
  if (separator <= 0) return null;
  const kind = value.slice(0, separator) as OntologyStudioKind;
  const key = value.slice(separator + 1);
  if (!VALID_KINDS.has(kind) || !key) return null;
  try {
    return { kind, key: validateKey(key, '知识条目') };
  } catch {
    return null;
  }
}

function elementId(kind: OntologyStudioKind, key: string): string {
  return `${kind}:${key}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
