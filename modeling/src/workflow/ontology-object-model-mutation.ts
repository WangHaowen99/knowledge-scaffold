import { createHash } from 'node:crypto';

import type { DataDictTable, PackageSpec, SpecField, SpecObject } from '../types.js';
import { findObjectDataMappingProblems } from './data-mapping.js';
import {
  applyValidationIssues,
  introducedSpecIntegrityProblems,
  normalizeObjectFieldsTable,
  normalizeObjectMetadataPatch,
  normalizeStudioRelations,
  toSpecObjectRelations,
  validateOntologyRelationTargets,
  type NormalizedOntologyRelation,
  type OntologyValidationIssue,
} from './ontology-model-validation.js';

export interface OntologyObjectMutationInput {
  metadata?: unknown;
  field_ops?: unknown;
  relation_ops?: unknown;
}

export interface OntologyObjectRelationView {
  id: string;
  kind: string;
  targetId: string;
  targetKey: string;
  label: string;
  description?: string;
}

export interface OntologyObjectImpactSummary {
  metadataChanged: Array<'title' | 'description' | 'aliases' | 'queryBoundary'>;
  fieldDelta: { added: number; updated: number; removed: number; renamed: number };
  relationDelta: { added: number; updated: number; removed: number };
  affectedObjects: string[];
  primaryKeyAffected: string[];
  mappingAffected: boolean;
}

export interface OntologyObjectMutationPreview {
  ok: boolean;
  issues: OntologyValidationIssue[];
  fieldErrors: Record<string, string>;
  warnings: string[];
  impactSummary: OntologyObjectImpactSummary;
  normalizedMutation: {
    metadata: Partial<Pick<SpecObject, 'title' | 'description' | 'aliases' | 'queryBoundary'>>;
    fieldOps: { addKeys: string[]; updateKeys: string[]; removeKeys: string[] };
    relationOps: { addTargets: string[]; updateIds: string[]; removeIds: string[] };
  };
  patchChanges: Record<string, unknown>;
  nextSpec?: PackageSpec;
  nextSpecObject?: SpecObject;
  nextObject: {
    id: string;
    key: string;
    title: string;
    description: string;
    aliases: string[];
    queryBoundary: string;
    fields: SpecField[];
    relations: OntologyObjectRelationView[];
  };
  reviewRecommendation: {
    tier: 'structural';
    title: string;
    summary: string;
    paths: string[];
  };
}

interface RawObjectRelation extends OntologyObjectRelationView {
  evidence?: string[];
}

export function previewOntologyObjectMutation(
  spec: PackageSpec,
  objectKey: string,
  input: OntologyObjectMutationInput,
  options: {
    tables?: DataDictTable[];
  } = {},
): OntologyObjectMutationPreview {
  const current = spec.objects.find((item) => item.key === objectKey);
  if (!current) {
    const issues = [validationIssue('OBJECT_NOT_FOUND', 'object_id', '对象不存在')];
    return emptyPreview(objectKey, issues);
  }

  const previousIntegrityProblems = listProblemMessages(spec);
  const previousMappingProblems = listMappingProblemMessages(spec, options.tables ?? []);
  const issues: OntologyValidationIssue[] = [];

  const metadata = input.metadata === undefined
    ? { changes: {}, issues: [] }
    : normalizeObjectMetadataPatch(input.metadata, 'metadata');
  const metadataChanges = metadata.changes ?? {};
  issues.push(...metadata.issues);

  const fieldPreview = mergeObjectFields(current, input.field_ops);
  const relationPreview = mergeObjectRelations(current, input.relation_ops);
  issues.push(...fieldPreview.issues, ...relationPreview.issues);

  const nextObject: SpecObject = {
    ...structuredClone(current),
    ...metadataChanges,
    ...(fieldPreview.fields ? { fields: fieldPreview.fields } : {}),
    ...(relationPreview.relations ? { relations: toSpecObjectRelations(relationPreview.relations) } : {}),
  };
  const nextSpec: PackageSpec = {
    ...structuredClone(spec),
    objects: spec.objects.map((item) => (item.key === objectKey ? nextObject : structuredClone(item))),
  };

  const nextIntegrityProblems = introducedSpecIntegrityProblems(nextSpec, previousIntegrityProblems);
  nextIntegrityProblems.forEach((message) => {
    issues.push(validationIssue('SPEC_INTEGRITY_INVALID', '_batch', message));
  });

  const nextMappingProblems = introducedMessages(
    listMappingProblemMessages(nextSpec, options.tables ?? []),
    previousMappingProblems,
  );
  nextMappingProblems.forEach((message) => {
    issues.push(validationIssue('OBJECT_MAPPING_INVALID', 'field_ops', message));
  });

  const patchChanges = effectivePatchChanges(current, nextObject, relationPreview.relations);
  if ('relations' in patchChanges && relationPreview.relations) {
    issues.push(...validateOntologyRelationTargets(nextSpec, [{
      path: 'relation_ops.final_relations',
      relations: relationPreview.relations,
    }]));
  }
  if (!Object.keys(patchChanges).length && issues.length === 0) {
    issues.push(validationIssue('NO_CHANGES', '_batch', '没有产生有效修改'));
  }

  const fieldErrors: Record<string, string> = {};
  applyValidationIssues(fieldErrors, issues);

  return {
    ok: !issues.some((item) => item.level === 'error'),
    issues,
    fieldErrors,
    warnings: buildWarnings(current, patchChanges),
    impactSummary: {
      metadataChanged: (Object.keys(patchChanges)
        .filter((key) => ['title', 'description', 'aliases', 'queryBoundary'].includes(key))
      ) as Array<'title' | 'description' | 'aliases' | 'queryBoundary'>,
      fieldDelta: fieldPreview.delta,
      relationDelta: relationPreview.delta,
      affectedObjects: relationPreview.affectedObjects,
      primaryKeyAffected: fieldPreview.primaryKeyAffected,
      mappingAffected: nextMappingProblems.length > 0
        || (fieldPreview.delta.added + fieldPreview.delta.updated + fieldPreview.delta.removed) > 0,
    },
    normalizedMutation: {
      metadata: pickMetadataChanges(patchChanges),
      fieldOps: {
        addKeys: fieldPreview.addKeys,
        updateKeys: fieldPreview.updateKeys,
        removeKeys: fieldPreview.removeKeys,
      },
      relationOps: {
        addTargets: relationPreview.addTargets,
        updateIds: relationPreview.updateIds,
        removeIds: relationPreview.removeIds,
      },
    },
    patchChanges,
    nextSpec,
    nextSpecObject: nextObject,
    nextObject: {
      id: `object:${nextObject.key}`,
      key: nextObject.key,
      title: nextObject.title,
      description: nextObject.description,
      aliases: nextObject.aliases ?? [],
      queryBoundary: nextObject.queryBoundary ?? '',
      fields: nextObject.fields ?? [],
      relations: projectRelations(nextObject),
    },
    reviewRecommendation: {
      tier: 'structural',
      title: `确认对象《${nextObject.title || nextObject.key}》模型修改`,
      summary: summarizeMutation(nextObject, patchChanges, fieldPreview.delta, relationPreview.delta),
      paths: [`objects/${nextObject.key}.md`],
    },
  };
}

function emptyPreview(objectKey: string, issues: OntologyValidationIssue[]): OntologyObjectMutationPreview {
  const fieldErrors: Record<string, string> = {};
  applyValidationIssues(fieldErrors, issues);
  return {
    ok: false,
    issues,
    fieldErrors,
    warnings: [],
    impactSummary: {
      metadataChanged: [],
      fieldDelta: { added: 0, updated: 0, removed: 0, renamed: 0 },
      relationDelta: { added: 0, updated: 0, removed: 0 },
      affectedObjects: [],
      primaryKeyAffected: [],
      mappingAffected: false,
    },
    normalizedMutation: {
      metadata: {},
      fieldOps: { addKeys: [], updateKeys: [], removeKeys: [] },
      relationOps: { addTargets: [], updateIds: [], removeIds: [] },
    },
    patchChanges: {},
    nextSpec: undefined,
    nextSpecObject: undefined,
    nextObject: {
      id: `object:${objectKey}`,
      key: objectKey,
      title: objectKey,
      description: '',
      aliases: [],
      queryBoundary: '',
      fields: [],
      relations: [],
    },
    reviewRecommendation: {
      tier: 'structural',
      title: `确认对象《${objectKey}》模型修改`,
      summary: '对象不存在，无法形成变更预览',
      paths: [`objects/${objectKey}.md`],
    },
  };
}

function mergeObjectFields(
  current: SpecObject,
  raw: unknown,
): {
  fields?: SpecField[];
  issues: OntologyValidationIssue[];
  delta: { added: number; updated: number; removed: number; renamed: number };
  addKeys: string[];
  updateKeys: string[];
  removeKeys: string[];
  primaryKeyAffected: string[];
} {
  const issues: OntologyValidationIssue[] = [];
  if (raw === undefined) {
    return {
      fields: structuredClone(current.fields ?? []),
      issues,
      delta: { added: 0, updated: 0, removed: 0, renamed: 0 },
      addKeys: [],
      updateKeys: [],
      removeKeys: [],
      primaryKeyAffected: [],
    };
  }
  if (!isRecord(raw)) {
    issues.push(validationIssue('FIELD_OPS_INVALID', 'field_ops', 'field_ops 必须是对象'));
    return {
      issues,
      delta: { added: 0, updated: 0, removed: 0, renamed: 0 },
      addKeys: [],
      updateKeys: [],
      removeKeys: [],
      primaryKeyAffected: [],
    };
  }
  for (const key of Object.keys(raw)) {
    if (!['add', 'update', 'remove'].includes(key)) {
      issues.push(validationIssue('FIELD_OPS_UNKNOWN_FIELD', `field_ops.${key}`, '未知字段'));
    }
  }
  const add = Array.isArray(raw.add) ? raw.add : raw.add === undefined ? [] : invalidArrayIssue('field_ops.add', issues);
  const update = Array.isArray(raw.update) ? raw.update : raw.update === undefined ? [] : invalidArrayIssue('field_ops.update', issues);
  const remove = Array.isArray(raw.remove) ? raw.remove : raw.remove === undefined ? [] : invalidArrayIssue('field_ops.remove', issues);

  const working = structuredClone(current.fields ?? []);
  const removeKeys: string[] = [];
  const updateKeys: string[] = [];
  const addKeys: string[] = [];
  const primaryKeyAffected = new Set<string>();
  const removeSet = new Set<string>();
  remove.forEach((item, index) => {
    const key = typeof item === 'string' ? item.trim() : '';
    if (!key) {
      issues.push(validationIssue('FIELD_REMOVE_KEY_REQUIRED', `field_ops.remove[${index}]`, '字段 key 不能为空'));
      return;
    }
    if (removeSet.has(key)) {
      issues.push(validationIssue('FIELD_REMOVE_DUPLICATE', `field_ops.remove[${index}]`, '重复删除同一字段'));
      return;
    }
    const fieldIndex = working.findIndex((field) => field.key === key);
    if (fieldIndex < 0) {
      issues.push(validationIssue('FIELD_REMOVE_NOT_FOUND', `field_ops.remove[${index}]`, '要删除的字段不存在'));
      return;
    }
    if ((current.primaryKey ?? []).includes(key)) primaryKeyAffected.add(key);
    removeSet.add(key);
    removeKeys.push(key);
    working.splice(fieldIndex, 1);
  });

  let renamed = 0;
  update.forEach((item, index) => {
    if (!isRecord(item)) {
      issues.push(validationIssue('FIELD_UPDATE_INVALID', `field_ops.update[${index}]`, '必须是对象'));
      return;
    }
    for (const key of Object.keys(item)) {
      if (!['key', 'changes'].includes(key)) {
        issues.push(validationIssue('FIELD_UPDATE_UNKNOWN_FIELD', `field_ops.update[${index}].${key}`, '未知字段'));
      }
    }
    const key = typeof item.key === 'string' ? item.key.trim() : '';
    if (!key) {
      issues.push(validationIssue('FIELD_UPDATE_KEY_REQUIRED', `field_ops.update[${index}].key`, '字段 key 不能为空'));
      return;
    }
    if (removeSet.has(key)) {
      issues.push(validationIssue('FIELD_OPS_CONFLICT', `field_ops.update[${index}].key`, '同一字段不能同时更新和删除'));
      return;
    }
    if (!isRecord(item.changes)) {
      issues.push(validationIssue('FIELD_UPDATE_CHANGES_INVALID', `field_ops.update[${index}].changes`, 'changes 必须是对象'));
      return;
    }
    const fieldIndex = working.findIndex((field) => field.key === key);
    if (fieldIndex < 0) {
      issues.push(validationIssue('FIELD_UPDATE_NOT_FOUND', `field_ops.update[${index}].key`, '要更新的字段不存在'));
      return;
    }
    const before = working[fieldIndex];
    const next = { ...before, ...item.changes };
    if (typeof item.changes.key === 'string' && item.changes.key.trim() && item.changes.key.trim() !== key) {
      renamed += 1;
      if ((current.primaryKey ?? []).includes(key)) primaryKeyAffected.add(key);
    }
    working[fieldIndex] = next;
    updateKeys.push(key);
  });

  add.forEach((item, index) => {
    if (!isRecord(item)) {
      issues.push(validationIssue('FIELD_ADD_INVALID', `field_ops.add[${index}]`, '必须是对象'));
      return;
    }
    const key = typeof item.key === 'string' ? item.key.trim() : '';
    if (key) addKeys.push(key);
    working.push({ ...item } as SpecField);
  });

  const normalized = normalizeObjectFieldsTable(working, 'field_ops.final_fields');
  issues.push(...normalized.issues);
  const fields = normalized.fields;
  const missingPrimaryKeys = (current.primaryKey ?? []).filter((key) => !fields?.some((field) => field.key === key));
  missingPrimaryKeys.forEach((key) => {
    primaryKeyAffected.add(key);
    issues.push(validationIssue(
      'PRIMARY_KEY_REMOVED',
      'field_ops',
      `主键字段 ${key} 不能被删除，除非同步调整对象主键定义`,
    ));
  });
  return {
    fields,
    issues,
    delta: { added: addKeys.length, updated: updateKeys.length, removed: removeKeys.length, renamed },
    addKeys,
    updateKeys,
    removeKeys,
    primaryKeyAffected: [...primaryKeyAffected],
  };
}

function mergeObjectRelations(
  current: SpecObject,
  raw: unknown,
): {
  relations?: NormalizedOntologyRelation[];
  issues: OntologyValidationIssue[];
  delta: { added: number; updated: number; removed: number };
  addTargets: string[];
  updateIds: string[];
  removeIds: string[];
  affectedObjects: string[];
} {
  const issues: OntologyValidationIssue[] = [];
  if (raw === undefined) {
    return {
      issues,
      delta: { added: 0, updated: 0, removed: 0 },
      addTargets: [],
      updateIds: [],
      removeIds: [],
      affectedObjects: [],
    };
  }
  if (!isRecord(raw)) {
    issues.push(validationIssue('RELATION_OPS_INVALID', 'relation_ops', 'relation_ops 必须是对象'));
    return {
      issues,
      delta: { added: 0, updated: 0, removed: 0 },
      addTargets: [],
      updateIds: [],
      removeIds: [],
      affectedObjects: [],
    };
  }
  for (const key of Object.keys(raw)) {
    if (!['add', 'update', 'remove'].includes(key)) {
      issues.push(validationIssue('RELATION_OPS_UNKNOWN_FIELD', `relation_ops.${key}`, '未知字段'));
    }
  }
  const add = Array.isArray(raw.add) ? raw.add : raw.add === undefined ? [] : invalidArrayIssue('relation_ops.add', issues);
  const update = Array.isArray(raw.update) ? raw.update : raw.update === undefined ? [] : invalidArrayIssue('relation_ops.update', issues);
  const remove = Array.isArray(raw.remove) ? raw.remove : raw.remove === undefined ? [] : invalidArrayIssue('relation_ops.remove', issues);

  const working = currentObjectRelations(current);
  const addTargets: string[] = [];
  const updateIds: string[] = [];
  const removeIds: string[] = [];
  const affectedObjects = new Set<string>();
  const removeSet = new Set<string>();

  remove.forEach((item, index) => {
    const relationId = typeof item === 'string' ? item.trim() : '';
    if (!relationId) {
      issues.push(validationIssue('RELATION_REMOVE_ID_REQUIRED', `relation_ops.remove[${index}]`, 'relationId 不能为空'));
      return;
    }
    if (removeSet.has(relationId)) {
      issues.push(validationIssue('RELATION_REMOVE_DUPLICATE', `relation_ops.remove[${index}]`, '重复删除同一关系'));
      return;
    }
    const relationIndex = working.findIndex((relation) => relation.id === relationId);
    if (relationIndex < 0) {
      issues.push(validationIssue('RELATION_REMOVE_NOT_FOUND', `relation_ops.remove[${index}]`, '要删除的关系不存在'));
      return;
    }
    removeSet.add(relationId);
    removeIds.push(relationId);
    affectedObjects.add(working[relationIndex].targetId);
    working.splice(relationIndex, 1);
  });

  update.forEach((item, index) => {
    if (!isRecord(item)) {
      issues.push(validationIssue('RELATION_UPDATE_INVALID', `relation_ops.update[${index}]`, '必须是对象'));
      return;
    }
    for (const key of Object.keys(item)) {
      if (!['relationId', 'changes'].includes(key)) {
        issues.push(validationIssue('RELATION_UPDATE_UNKNOWN_FIELD', `relation_ops.update[${index}].${key}`, '未知字段'));
      }
    }
    const relationId = typeof item.relationId === 'string' ? item.relationId.trim() : '';
    if (!relationId) {
      issues.push(validationIssue('RELATION_UPDATE_ID_REQUIRED', `relation_ops.update[${index}].relationId`, 'relationId 不能为空'));
      return;
    }
    if (removeSet.has(relationId)) {
      issues.push(validationIssue('RELATION_OPS_CONFLICT', `relation_ops.update[${index}].relationId`, '同一关系不能同时更新和删除'));
      return;
    }
    if (!isRecord(item.changes)) {
      issues.push(validationIssue('RELATION_UPDATE_CHANGES_INVALID', `relation_ops.update[${index}].changes`, 'changes 必须是对象'));
      return;
    }
    for (const key of Object.keys(item.changes)) {
      if (!['kind', 'targetId', 'label', 'description'].includes(key)) {
        issues.push(validationIssue('RELATION_CHANGE_UNKNOWN_FIELD', `relation_ops.update[${index}].changes.${key}`, '未知字段'));
      }
    }
    const relationIndex = working.findIndex((relation) => relation.id === relationId);
    if (relationIndex < 0) {
      issues.push(validationIssue('RELATION_UPDATE_NOT_FOUND', `relation_ops.update[${index}].relationId`, '要更新的关系不存在'));
      return;
    }
    const next: RawObjectRelation = {
      ...working[relationIndex],
      ...item.changes,
      targetKey: working[relationIndex].targetKey,
    };
    if (typeof item.changes.targetId === 'string' && item.changes.targetId.trim()) {
      next.targetId = item.changes.targetId.trim();
      next.targetKey = parseTargetKey(next.targetId);
    }
    working[relationIndex] = next;
    updateIds.push(relationId);
    affectedObjects.add(next.targetId);
  });

  add.forEach((item, index) => {
    if (!isRecord(item)) {
      issues.push(validationIssue('RELATION_ADD_INVALID', `relation_ops.add[${index}]`, '必须是对象'));
      return;
    }
    for (const key of Object.keys(item)) {
      if (!['kind', 'targetId', 'label', 'description'].includes(key)) {
        issues.push(validationIssue('RELATION_ADD_UNKNOWN_FIELD', `relation_ops.add[${index}].${key}`, '未知字段'));
      }
    }
    const targetId = typeof item.targetId === 'string' ? item.targetId.trim() : '';
    if (targetId) addTargets.push(targetId);
    affectedObjects.add(targetId);
    working.push({
      id: `pending:${index}`,
      kind: typeof item.kind === 'string' ? item.kind : '',
      targetId,
      targetKey: parseTargetKey(targetId),
      label: typeof item.label === 'string' ? item.label : '',
      ...(typeof item.description === 'string' ? { description: item.description } : {}),
    });
  });

  const normalized = normalizeStudioRelations(
    'object',
    working.map((relation) => ({
      kind: relation.kind,
      targetId: relation.targetId,
      label: relation.label,
      ...(relation.description !== undefined ? { description: relation.description } : {}),
      ...(relation.evidence?.length ? { evidence: [...relation.evidence] } : {}),
    })),
    'relation_ops.final_relations',
    [{ kind: 'object_relation', targetKinds: ['object'] }],
    { preserveEvidence: true },
  );
  issues.push(...normalized.issues);
  return {
    relations: normalized.relations,
    issues,
    delta: { added: addTargets.length, updated: updateIds.length, removed: removeIds.length },
    addTargets,
    updateIds,
    removeIds,
    affectedObjects: [...affectedObjects].filter(Boolean).sort(),
  };
}

function currentObjectRelations(object: SpecObject): RawObjectRelation[] {
  const sourceId = `object:${object.key}`;
  return (object.relations ?? []).map((relation) => {
    const targetId = relation.target.includes(':') ? relation.target : `object:${relation.target}`;
    const identity = JSON.stringify([
      sourceId,
      'object_relation',
      targetId,
      relation.relation,
      relation.note,
      relation.evidence,
    ]);
    return {
      id: `edge:${createHash('sha256').update(identity).digest('hex').slice(0, 20)}`,
      kind: 'object_relation',
      targetId,
      targetKey: parseTargetKey(targetId),
      label: relation.relation,
      ...(relation.note ? { description: relation.note } : {}),
      ...(relation.evidence?.length ? { evidence: relation.evidence } : {}),
    };
  });
}

function projectRelations(object: SpecObject): OntologyObjectRelationView[] {
  return currentObjectRelations(object).map(({ evidence: _evidence, ...relation }) => relation);
}

function effectivePatchChanges(
  current: SpecObject,
  next: SpecObject,
  relations: NormalizedOntologyRelation[] | undefined,
): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  if (stableJson(current.title) !== stableJson(next.title)) changes.title = next.title;
  if (stableJson(current.description) !== stableJson(next.description)) changes.description = next.description;
  if (stableJson(current.aliases ?? []) !== stableJson(next.aliases ?? [])) changes.aliases = next.aliases ?? [];
  if (stableJson(current.queryBoundary ?? '') !== stableJson(next.queryBoundary ?? '')) {
    changes.queryBoundary = next.queryBoundary ?? '';
  }
  if (stableJson(current.fields ?? []) !== stableJson(next.fields ?? [])) changes.fields = next.fields ?? [];
  if (relations) {
    const currentRelations = currentObjectRelations(current).map((item) => ({
      kind: item.kind,
      targetId: item.targetId,
      label: item.label,
      ...(item.description ? { description: item.description } : {}),
    }));
    const nextRelations = relations.map((item) => ({
      kind: item.kind,
      targetId: `object:${item.targetKey}`,
      ...(item.label ? { label: item.label } : {}),
      ...(item.description ? { description: item.description } : {}),
    }));
    if (stableJson(currentRelations) !== stableJson(nextRelations)) {
      changes.relations = nextRelations;
    }
  }
  return changes;
}

function pickMetadataChanges(
  patchChanges: Record<string, unknown>,
): Partial<Pick<SpecObject, 'title' | 'description' | 'aliases' | 'queryBoundary'>> {
  const result: Partial<Pick<SpecObject, 'title' | 'description' | 'aliases' | 'queryBoundary'>> = {};
  if ('title' in patchChanges) result.title = String(patchChanges.title ?? '');
  if ('description' in patchChanges) result.description = String(patchChanges.description ?? '');
  if ('aliases' in patchChanges) result.aliases = Array.isArray(patchChanges.aliases)
    ? patchChanges.aliases.map(String)
    : [];
  if ('queryBoundary' in patchChanges) result.queryBoundary = String(patchChanges.queryBoundary ?? '');
  return result;
}

function summarizeMutation(
  object: SpecObject,
  patchChanges: Record<string, unknown>,
  fieldDelta: OntologyObjectImpactSummary['fieldDelta'],
  relationDelta: OntologyObjectImpactSummary['relationDelta'],
): string {
  const parts: string[] = [];
  const metadataFields = Object.keys(patchChanges).filter((key) => (
    ['title', 'description', 'aliases', 'queryBoundary'].includes(key)
  ));
  if (metadataFields.length) parts.push(`修改元数据 ${metadataFields.join('、')}`);
  if (fieldDelta.added || fieldDelta.updated || fieldDelta.removed) {
    parts.push(`字段 +${fieldDelta.added} / ~${fieldDelta.updated} / -${fieldDelta.removed}`);
  }
  if (relationDelta.added || relationDelta.updated || relationDelta.removed) {
    parts.push(`关系 +${relationDelta.added} / ~${relationDelta.updated} / -${relationDelta.removed}`);
  }
  return parts.length
    ? `对象《${object.title || object.key}》${parts.join('；')}`
    : `对象《${object.title || object.key}》未产生有效修改`;
}

function buildWarnings(current: SpecObject, patchChanges: Record<string, unknown>): string[] {
  const warnings: string[] = [];
  if ('fields' in patchChanges && (current.sourceRefs?.length || current.sourceTable)) {
    warnings.push('对象已绑定来源表，字段修改可能联动数据 mapping 与主键约束');
  }
  if ('relations' in patchChanges) {
    warnings.push('对象关系变更会影响依赖该对象图谱的检索与推理链路');
  }
  return warnings;
}

function invalidArrayIssue(path: string, issues: OntologyValidationIssue[]): never[] {
  issues.push(validationIssue('ARRAY_REQUIRED', path, '必须是数组'));
  return [];
}

function parseTargetKey(targetId: string): string {
  const normalized = targetId.trim();
  const separator = normalized.indexOf(':');
  return separator > 0 ? normalized.slice(separator + 1) : normalized;
}

function introducedMessages(current: string[], previous: string[]): string[] {
  const previousCounts = new Map<string, number>();
  previous.forEach((message) => {
    previousCounts.set(message, (previousCounts.get(message) ?? 0) + 1);
  });
  return current.filter((message) => {
    const remaining = previousCounts.get(message) ?? 0;
    if (!remaining) return true;
    previousCounts.set(message, remaining - 1);
    return false;
  });
}

function listProblemMessages(spec: PackageSpec): string[] {
  return introducedSpecIntegrityProblems(spec, []);
}

function listMappingProblemMessages(spec: PackageSpec, tables: DataDictTable[]): string[] {
  return findObjectDataMappingProblems(spec, tables).map((problem) => problem.message).sort();
}

function validationIssue(code: string, path: string, message: string): OntologyValidationIssue {
  return { code, level: 'error', path, message };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
