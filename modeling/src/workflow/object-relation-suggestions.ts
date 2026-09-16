import { createHash } from 'node:crypto';

import type {
  DataDictTable,
  PackageSpec,
  SpecField,
  SpecObject,
  SpecOperation,
  SpecRelation,
  SpecRule,
} from '../types.js';
import { tableSourceFor } from './source-references.js';

export type ObjectRelationSuggestionConfidence = 'high' | 'medium' | 'low';
export type ObjectRelationSuggestionMatchMode = 'field_match' | 'same_row' | 'shared_key' | 'semantic_text';
export type PreviousObjectRelationStatus = 'previous' | 'discarded' | 'needs_review';

export interface PreviousObjectRelation {
  sourceObject: string;
  targetObject: string;
  relation: string;
  note?: string;
  evidence?: string[];
  status?: PreviousObjectRelationStatus;
}

export interface ObjectRelationSuggestionScope {
  affectedObjects?: string[];
  affectedOperations?: string[];
}

export interface SuggestObjectRelationsInput {
  limit?: number;
  cursor?: string;
  includeLowConfidence?: boolean;
  previousRelations?: PreviousObjectRelation[];
  rejectedFingerprints?: string[];
  scope?: ObjectRelationSuggestionScope;
}

export interface ObjectRelationSuggestion {
  candidateFingerprint: string;
  sourceObject: string;
  targetObject: string;
  relation: string;
  note: string;
  confidence: ObjectRelationSuggestionConfidence;
  reason: string;
  evidence?: string[];
  sourceField?: string;
  targetField?: string;
  matchMode?: ObjectRelationSuggestionMatchMode;
  score: number;
}

export interface SuggestObjectRelationsResult {
  schemaInputHash: string;
  totalCandidateCount: number;
  returnedCount: number;
  hasMore: boolean;
  nextCursor?: string;
  candidates: ObjectRelationSuggestion[];
}

interface CandidateDraft {
  sourceObject: string;
  targetObject: string;
  relation: string;
  note: string;
  confidence: ObjectRelationSuggestionConfidence;
  reason: string;
  evidence?: string[];
  sourceField?: string;
  targetField?: string;
  matchMode?: ObjectRelationSuggestionMatchMode;
  score: number;
}

interface ObjectContext {
  object: SpecObject;
  primaryFields: SpecField[];
  table?: DataDictTable;
}

const DETAIL_SUFFIXES = ['detail', 'details', 'item', 'items', 'line', 'lines', '明细', '详情', '子项'];
const ID_SUFFIXES = ['id', 'key', 'code', 'no', 'number', '编号', '编码', '标识', '代码'];

export function suggestObjectRelations(
  spec: PackageSpec,
  tables: DataDictTable[],
  input: SuggestObjectRelationsInput = {},
): SuggestObjectRelationsResult {
  const schemaInputHash = schemaHash(spec, tables);
  const limit = clampInteger(input.limit, 10, 1, 20);
  const rejected = new Set(input.rejectedFingerprints ?? []);
  const existing = existingRelationKeys(spec.objects ?? []);
  const contexts = objectContexts(spec.objects ?? [], tables);
  const objectKeys = new Set(contexts.map((item) => item.object.key));
  const scopedObjects = scopedObjectKeys(spec, input.scope);
  const drafts = new Map<string, ObjectRelationSuggestion>();

  const addDraft = (draft: CandidateDraft) => {
    if (!objectKeys.has(draft.sourceObject) || !objectKeys.has(draft.targetObject)) return;
    if (draft.sourceObject === draft.targetObject) return;
    const relation = draft.relation.trim();
    if (!relation) return;
    const existingKey = relationKey(draft.sourceObject, draft.targetObject, relation);
    if (existing.has(existingKey)) return;
    const candidateFingerprint = candidateHash({
      sourceObject: draft.sourceObject,
      targetObject: draft.targetObject,
      relation,
      sourceField: draft.sourceField ?? '',
      targetField: draft.targetField ?? '',
    });
    if (rejected.has(candidateFingerprint)) return;
    const candidate: ObjectRelationSuggestion = { ...draft, relation, candidateFingerprint };
    const current = drafts.get(candidateFingerprint);
    if (!current || candidate.score > current.score) drafts.set(candidateFingerprint, candidate);
  };

  for (const previous of input.previousRelations ?? []) {
    const sourceObject = previous.sourceObject.trim();
    const targetObject = previous.targetObject.trim();
    if (!sourceObject || !targetObject) continue;
    const status = previous.status ?? 'previous';
    const confidence = status === 'previous' ? 'high' : 'medium';
    addDraft({
      sourceObject,
      targetObject,
      relation: previous.relation || 'references',
      note: previous.note || 'Previously confirmed object relation should be reviewed against the current schema.',
      confidence,
      reason: status === 'previous'
        ? 'Historical relation endpoints still exist in the current object set.'
        : `Historical relation status is ${status}; review before restoring it.`,
      evidence: safeEvidence(previous.evidence),
      score: status === 'previous' ? 95 : 65,
    });
  }

  for (const source of contexts) {
    for (const target of contexts) {
      if (source.object.key === target.object.key) continue;
      addFieldMatchCandidates(source, target, addDraft);
      addDetailTableCandidate(source, target, addDraft);
      addSameRowCandidate(source, target, addDraft);
    }
  }

  addSharedKeyCandidates(contexts, addDraft);
  addSemanticTextCandidates(spec, contexts, addDraft);
  addOperationCoReferenceCandidates(spec, addDraft);

  let candidates = [...drafts.values()]
    .filter((candidate) => input.includeLowConfidence === true || candidate.confidence !== 'low')
    .filter((candidate) => !scopedObjects || scopedObjects.has(candidate.sourceObject) || scopedObjects.has(candidate.targetObject))
    .sort(compareCandidates);

  const totalCandidateCount = candidates.length;
  const offset = objectRelationSuggestionCursorOffset(input.cursor, schemaInputHash) ?? 0;
  candidates = candidates.slice(offset, offset + limit);
  const nextOffset = offset + candidates.length;
  const hasMore = nextOffset < totalCandidateCount;
  return {
    schemaInputHash,
    totalCandidateCount,
    returnedCount: candidates.length,
    hasMore,
    ...(hasMore ? { nextCursor: encodeCursor(schemaInputHash, nextOffset) } : {}),
    candidates,
  };
}

export function collectPreviousObjectRelations(
  previousObjects: SpecObject[],
  nextObjects: SpecObject[],
): PreviousObjectRelation[] {
  const nextKeys = new Set(nextObjects.map((object) => object.key));
  return previousObjects.flatMap((object) => (object.relations ?? []).map((relation) => ({
    sourceObject: object.key,
    targetObject: relation.target,
    relation: relation.relation,
    ...(relation.note ? { note: relation.note } : {}),
    ...(relation.evidence?.length ? { evidence: safeEvidence(relation.evidence) } : {}),
    status: nextKeys.has(object.key) && nextKeys.has(relation.target) ? 'previous' : 'discarded',
  } satisfies PreviousObjectRelation)));
}

interface KeyHolder {
  context: ObjectContext;
  field: SpecField;
  label: string;
}

/* 多张表复用同一个标识列是跨表关系的结构事实，不依赖对象名称。 */
function addSharedKeyCandidates(
  contexts: ObjectContext[],
  addDraft: (draft: CandidateDraft) => void,
): void {
  const byColumn = new Map<string, KeyHolder[]>();
  for (const context of contexts) {
    for (const field of context.object.fields ?? []) {
      if (!isKeyLikeField(context.object, field)) continue;
      const label = String(field.sourceColumn || field.key).trim();
      const column = normalized(label);
      if (!column) continue;
      const holders = byColumn.get(column) ?? [];
      holders.push({ context, field, label });
      byColumn.set(column, holders);
    }
  }
  for (const holders of byColumn.values()) {
    if (holders.length < 2) continue;
    const ranked = [...holders].sort(compareKeyHolders);
    const hub = ranked[0];
    const hubUnique = keyUniqueRatio(hub);
    const tied = ranked.filter((item) => Math.abs(keyUniqueRatio(item) - hubUnique) < 0.02).length > 1;
    for (const holder of ranked.slice(1)) {
      const stats = fieldMatchStats(holder.context, holder.field, hub.context, hub.field);
      const oneToOne = keyUniqueRatio(holder) >= 0.95;
      addDraft({
        sourceObject: holder.context.object.key,
        targetObject: hub.context.object.key,
        relation: 'belongs_to',
        note: `${holder.context.object.key} 和 ${hub.context.object.key} 共用键列 ${hub.label}，${oneToOne ? '一行对一行' : '多行对一行'}。`,
        confidence: hubUnique >= 0.95 && !tied ? 'high' : 'medium',
        reason: [
          `Objects share key column ${hub.label}.`,
          `hubUniqueRatio=${formatRatio(hubUnique)}`,
          `sourceUniqueRatio=${formatRatio(keyUniqueRatio(holder))}`,
          `joinHitRatio=${formatRatio(stats.joinHitRatio)}`,
          `sampleSize=${stats.sampleSize}`,
          ...(tied ? [`Several objects are equally unique on ${hub.label}; hub choice needs expert review.`] : []),
        ].join('; '),
        sourceField: holder.field.key,
        targetField: hub.field.key,
        matchMode: 'shared_key',
        score: hubUnique >= 0.95 && !tied ? 94 : 86,
      });
    }
  }
}

/** 主键或带有常见标识后缀的字段才参与 shared-key，避免普通属性列制造关系。 */
function isKeyLikeField(object: SpecObject, field: SpecField): boolean {
  if (isPrimaryField(object, field)) return true;
  return fieldNameTokens(field).some((token) => idStem(token).length >= 2);
}

function keyUniqueRatio(holder: KeyHolder): number {
  const profile = holder.context.table ? tableFieldProfile(holder.context.table, holder.field) : undefined;
  return profile?.uniqueRatio ?? (isPrimaryField(holder.context.object, holder.field) ? 1 : 0);
}

function compareKeyHolders(left: KeyHolder, right: KeyHolder): number {
  return keyUniqueRatio(right) - keyUniqueRatio(left)
    || (right.context.table?.rowCount ?? 0) - (left.context.table?.rowCount ?? 0)
    || left.context.object.key.localeCompare(right.context.object.key);
}

interface SemanticRelationSource {
  kind: 'object' | 'operation' | 'rule' | 'package';
  key: string;
  text: string;
  objectKeys: string[];
}

/*
 * 规则和业务描述是关系证据，不应只用于生成规则/操作。这里只提取明确的
 * “关联、引用、匹配、外键、关联键”等关系语句，并保留来源 key 供审阅追溯。
 */
function addSemanticTextCandidates(
  spec: PackageSpec,
  contexts: ObjectContext[],
  addDraft: (draft: CandidateDraft) => void,
): void {
  const contextByKey = new Map(contexts.map((context) => [context.object.key, context]));
  const sources: SemanticRelationSource[] = [];
  const packageText = [spec.name, spec.description].filter(Boolean).join('\n');
  if (packageText) sources.push({ kind: 'package', key: spec.id, text: packageText, objectKeys: [] });

  for (const object of spec.objects ?? []) {
    sources.push({
      kind: 'object',
      key: object.key,
      text: [object.title, object.description, object.queryBoundary, ...(object.aliases ?? [])]
        .filter(Boolean).join('\n'),
      objectKeys: [object.key],
    });
  }
  for (const operation of spec.operations ?? []) {
    sources.push({
      kind: 'operation',
      key: operation.key,
      text: operationText(operation),
      objectKeys: operation.relatedObjects ?? [],
    });
  }
  for (const rule of spec.rules ?? []) {
    sources.push({
      kind: 'rule',
      key: rule.key,
      text: ruleText(rule),
      objectKeys: [
        ...(rule.appliesToObjects ?? []),
        ...objectsFromOperations(spec, rule.appliesToOperations ?? []),
      ],
    });
  }

  for (const source of sources) {
    if (!relationLanguage(source.text)) continue;
    const explicitKeys = new Set(source.objectKeys.filter((key) => contextByKey.has(key)));
    for (const context of contexts) {
      if (textMentionsObject(source.text, context.object)) explicitKeys.add(context.object.key);
    }
    const objectKeys = [...explicitKeys];
    if (objectKeys.length < 2) continue;
    for (let i = 0; i < objectKeys.length; i += 1) {
      for (let j = i + 1; j < objectKeys.length; j += 1) {
        const left = contextByKey.get(objectKeys[i]);
        const right = contextByKey.get(objectKeys[j]);
        if (!left || !right) continue;
        const shared = sharedFields(left, right);
        const sourceObject = semanticSourceObject(left, right, source.text);
        const targetObject = sourceObject === left.object.key ? right : left;
        const relation = semanticRelationName(source.text);
        const evidence = `${source.kind}:${source.key}`;
        addDraft({
          sourceObject,
          targetObject: targetObject.object.key,
          relation,
          note: `${left.object.key} 与 ${right.object.key} 在${sourceLabel(source)}中被描述为${relation === 'related_to' ? '关联' : relation === 'belongs_to' ? '从属' : '引用'}。`,
          confidence: shared.length ? 'high' : 'medium',
          reason: [
            `Semantic text ${evidence} describes a relation between ${left.object.key} and ${right.object.key}.`,
            ...(shared.length ? [`sharedFields=${shared.map((item) => item.label).join(',')}`] : []),
            `relationSignal=${relationLanguage(source.text)}`,
          ].join('; '),
          evidence: [evidence],
          ...(shared[0] ? { sourceField: shared[0].source.key, targetField: shared[0].target.key } : {}),
          matchMode: 'semantic_text',
          score: shared.length ? 96 : 88,
        });
      }
    }
  }
}

function operationText(operation: SpecOperation): string {
  return [
    operation.title,
    operation.description,
    ...(operation.flow ?? []),
    ...(operation.notes ?? []),
  ].filter(Boolean).join('\n');
}

function ruleText(rule: SpecRule): string {
  return [
    rule.title,
    rule.description,
    rule.content,
    ...(rule.conditions ?? []).flatMap((item) => [item.condition, item.conclusion]),
    ...(rule.exceptions ?? []),
  ].filter(Boolean).join('\n');
}

function objectsFromOperations(spec: PackageSpec, keys: string[]): string[] {
  const wanted = new Set(keys);
  return (spec.operations ?? [])
    .filter((operation) => wanted.has(operation.key))
    .flatMap((operation) => operation.relatedObjects ?? []);
}

function sourceLabel(source: SemanticRelationSource): string {
  if (source.kind === 'rule') return `规则 ${source.key}`;
  if (source.kind === 'operation') return `操作 ${source.key}`;
  if (source.kind === 'object') return `对象说明 ${source.key}`;
  return '知识包业务说明';
}

function relationLanguage(text: string): string {
  const normalizedText = String(text ?? '').toLocaleLowerCase('zh-CN');
  const match = normalizedText.match(/关联键|关联|外键|主键|引用|属于|隶属|匹配|连接|join|对应|映射/iu);
  return match?.[0] ?? '';
}

function semanticRelationName(text: string): string {
  const normalizedText = String(text ?? '').toLocaleLowerCase('zh-CN');
  if (/(?:引用|依赖|使用)/u.test(normalizedText)) return 'references';
  if (/(?:属于|隶属)/u.test(normalizedText)) return 'belongs_to';
  return 'related_to';
}

function semanticSourceObject(left: ObjectContext, right: ObjectContext, text: string): string {
  const normalizedText = String(text ?? '').toLocaleLowerCase('zh-CN');
  if (/(?:引用|依赖|使用)/u.test(normalizedText)) {
    const leftMention = objectTokens(left.object).some((token) => normalizedText.includes(token));
    const rightMention = objectTokens(right.object).some((token) => normalizedText.includes(token));
    if (leftMention && rightMention) return left.object.key;
  }
  return left.object.key.localeCompare(right.object.key) <= 0 ? left.object.key : right.object.key;
}

function textMentionsObject(text: string, object: SpecObject): boolean {
  const normalizedText = normalized(text);
  return objectTokens(object).some((token) => token.length >= 2 && normalizedText.includes(token));
}

function sharedFields(
  left: ObjectContext,
  right: ObjectContext,
): Array<{ source: SpecField; target: SpecField; label: string }> {
  const rightByColumn = new Map<string, SpecField>();
  for (const field of right.object.fields ?? []) {
    const column = normalized(field.sourceColumn || field.key);
    if (column) rightByColumn.set(column, field);
  }
  return (left.object.fields ?? []).flatMap((field) => {
    const column = normalized(field.sourceColumn || field.key);
    const target = column ? rightByColumn.get(column) : undefined;
    return target && isKeyLikeField(left.object, field) && isKeyLikeField(right.object, target)
      ? [{ source: field, target, label: String(field.sourceColumn || field.key) }]
      : [];
  });
}

function addFieldMatchCandidates(
  source: ObjectContext,
  target: ObjectContext,
  addDraft: (draft: CandidateDraft) => void,
): void {
  for (const field of source.object.fields ?? []) {
    if (isPrimaryField(source.object, field)) continue;
    const fieldTokens = fieldNameTokens(field);
    const foreignStem = fieldTokens.map(idStem).find((token) => token.length >= 2);
    if (!foreignStem) continue;
    const matchesTarget = objectTokens(target.object).some((token) => token === foreignStem || token.endsWith(foreignStem));
    if (!matchesTarget) continue;
    const targetField = target.primaryFields[0];
    if (!targetField) continue;
    const stats = fieldMatchStats(source, field, target, targetField);
    const reasonParts = [
      `Field ${source.object.key}.${field.key} looks like a reference to ${target.object.key}.`,
      `sourceNonEmptyRatio=${formatRatio(stats.sourceNonEmptyRatio)}`,
      `targetUniqueRatio=${formatRatio(stats.targetUniqueRatio)}`,
      `joinHitRatio=${formatRatio(stats.joinHitRatio)}`,
      `sampleSize=${stats.sampleSize}`,
    ];
    addDraft({
      sourceObject: source.object.key,
      targetObject: target.object.key,
      relation: 'belongs_to',
      note: `${source.object.key}.${field.key} can match primary key ${target.object.key}.${targetField.key}.`,
      confidence: stats.joinHitRatio >= 0.9 || stats.sampleSize === 0 ? 'high' : 'medium',
      reason: reasonParts.join('; '),
      sourceField: field.key,
      targetField: targetField.key,
      matchMode: 'field_match',
      score: stats.joinHitRatio >= 0.9 ? 92 : 82,
    });
  }
}

function addDetailTableCandidate(
  source: ObjectContext,
  target: ObjectContext,
  addDraft: (draft: CandidateDraft) => void,
): void {
  const sourceNames = objectTokens(source.object);
  const targetNames = objectTokens(target.object);
  const sourceLooksLikeDetail = sourceNames.some((sourceName) => targetNames.some((targetName) => (
    sourceName.startsWith(targetName)
    && DETAIL_SUFFIXES.some((suffix) => sourceName.endsWith(normalized(suffix)))
  )));
  if (!sourceLooksLikeDetail) return;
  const sourceField = bestForeignField(source.object, target.object);
  const targetField = target.primaryFields[0];
  addDraft({
    sourceObject: source.object.key,
    targetObject: target.object.key,
    relation: 'belongs_to',
    note: `${source.object.key} appears to be a detail table of ${target.object.key}.`,
    confidence: sourceField && targetField ? 'high' : 'medium',
    reason: 'Object and source table names match a header/detail naming pattern.',
    ...(sourceField ? { sourceField: sourceField.key } : {}),
    ...(targetField ? { targetField: targetField.key } : {}),
    matchMode: sourceField && targetField ? 'field_match' : undefined,
    score: sourceField && targetField ? 88 : 72,
  });
}

function addSameRowCandidate(
  source: ObjectContext,
  target: ObjectContext,
  addDraft: (draft: CandidateDraft) => void,
): void {
  const sourceRef = tableSourceFor(source.object);
  const targetRef = tableSourceFor(target.object);
  if (!sourceRef || !targetRef) return;
  if (sourceRef.table !== targetRef.table || sourceRef.documentId !== targetRef.documentId) return;
  if (source.object.key.localeCompare(target.object.key) > 0) return;
  addDraft({
    sourceObject: source.object.key,
    targetObject: target.object.key,
    relation: 'related_to',
    note: `${source.object.key} and ${target.object.key} are derived from the same source row.`,
    confidence: 'medium',
    reason: 'Both objects use the same physical table source; row-level relation may be configured later in graph import.',
    matchMode: 'same_row',
    score: 58,
  });
}

function addOperationCoReferenceCandidates(
  spec: PackageSpec,
  addDraft: (draft: CandidateDraft) => void,
): void {
  const pairCounts = new Map<string, { left: string; right: string; operations: string[] }>();
  for (const operation of spec.operations ?? []) {
    const keys = [...new Set(operation.relatedObjects ?? [])].sort();
    for (let i = 0; i < keys.length; i += 1) {
      for (let j = i + 1; j < keys.length; j += 1) {
        const key = `${keys[i]}\u0000${keys[j]}`;
        const current = pairCounts.get(key) ?? { left: keys[i], right: keys[j], operations: [] };
        current.operations.push(operation.key);
        pairCounts.set(key, current);
      }
    }
  }
  for (const pair of pairCounts.values()) {
    const count = pair.operations.length;
    addDraft({
      sourceObject: pair.left,
      targetObject: pair.right,
      relation: 'related_to',
      note: `${pair.left} and ${pair.right} are frequently referenced by the same operations.`,
      confidence: count >= 2 ? 'medium' : 'low',
      reason: `Objects are co-referenced by ${count} operation(s); direction requires semantic review.`,
      score: count >= 2 ? 56 + Math.min(count, 10) : 30,
    });
  }
}

function objectContexts(objects: SpecObject[], tables: DataDictTable[]): ObjectContext[] {
  return objects.map((object) => ({
    object,
    primaryFields: primaryFields(object),
    table: tableForObject(object, tables),
  }));
}

function primaryFields(object: SpecObject): SpecField[] {
  const keys = new Set(object.primaryKey ?? []);
  const selected = (object.fields ?? []).filter((field) => keys.has(field.key));
  if (selected.length) return selected;
  const likely = (object.fields ?? []).find((field) => fieldNameTokens(field).some((token) => ID_SUFFIXES.includes(token)));
  return likely ? [likely] : [];
}

function tableForObject(object: SpecObject, tables: DataDictTable[]): DataDictTable | undefined {
  const source = tableSourceFor(object);
  if (!source) return undefined;
  return tables.find((table) => table.name === source.table && table.documentId === source.documentId)
    ?? tables.find((table) => table.name === source.table);
}

function isPrimaryField(object: SpecObject, field: SpecField): boolean {
  return new Set(object.primaryKey ?? []).has(field.key);
}

function bestForeignField(source: SpecObject, target: SpecObject): SpecField | undefined {
  const targetTokens = objectTokens(target);
  return (source.fields ?? []).find((field) => {
    const stem = fieldNameTokens(field).map(idStem).find(Boolean);
    return stem ? targetTokens.includes(stem) : false;
  });
}

function fieldMatchStats(
  source: ObjectContext,
  sourceField: SpecField,
  target: ObjectContext,
  targetField: SpecField,
): {
  sourceNonEmptyRatio: number;
  targetUniqueRatio: number;
  joinHitRatio: number;
  unmatchedCount: number;
  duplicateTargetCount: number;
  sampleSize: number;
} {
  const sourceData = source.table ? tableFieldProfile(source.table, sourceField) : undefined;
  const targetData = target.table ? tableFieldProfile(target.table, targetField) : undefined;
  if (!sourceData || !targetData) {
    return {
      sourceNonEmptyRatio: 0,
      targetUniqueRatio: 0,
      joinHitRatio: 0,
      unmatchedCount: 0,
      duplicateTargetCount: 0,
      sampleSize: 0,
    };
  }
  const targetValues = new Set(targetData.values);
  const hits = sourceData.values.filter((value) => targetValues.has(value)).length;
  return {
    sourceNonEmptyRatio: sourceData.nonEmptyRatio,
    targetUniqueRatio: targetData.uniqueRatio,
    joinHitRatio: sourceData.values.length ? hits / sourceData.values.length : 0,
    unmatchedCount: Math.max(sourceData.values.length - hits, 0),
    duplicateTargetCount: Math.max(targetData.values.length - targetValues.size, 0),
    sampleSize: sourceData.values.length,
  };
}

function tableFieldProfile(table: DataDictTable, field: SpecField): {
  values: string[];
  nonEmptyRatio: number;
  uniqueRatio: number;
} | undefined {
  const column = field.sourceColumn || field.cnName || field.key;
  const index = table.fields.findIndex((item) => item.name === column || item.sourceColumn === column || item.name === field.key);
  if (index < 0) return undefined;
  const values = (table.rows ?? [])
    .map((row) => String(row[index] ?? '').trim())
    .filter(Boolean);
  const unique = new Set(values);
  const fieldStats = table.fields[index];
  return {
    values,
    nonEmptyRatio: fieldStats?.nonEmptyRatio ?? (table.rowCount ? values.length / table.rowCount : 0),
    uniqueRatio: fieldStats?.distinctRatio ?? (values.length ? unique.size / values.length : 0),
  };
}

function scopedObjectKeys(spec: PackageSpec, scope?: ObjectRelationSuggestionScope): Set<string> | undefined {
  const result = new Set((scope?.affectedObjects ?? []).map((key) => key.trim()).filter(Boolean));
  const affectedOperations = new Set((scope?.affectedOperations ?? []).map((key) => key.trim()).filter(Boolean));
  if (affectedOperations.size) {
    for (const operation of spec.operations ?? []) {
      if (!affectedOperations.has(operation.key)) continue;
      for (const key of operation.relatedObjects ?? []) result.add(key);
    }
  }
  return result.size ? result : undefined;
}

function existingRelationKeys(objects: SpecObject[]): Set<string> {
  const keys = new Set<string>();
  for (const object of objects) {
    for (const relation of object.relations ?? []) {
      keys.add(relationKey(object.key, relation.target, relation.relation));
    }
  }
  return keys;
}

function relationKey(sourceObject: string, targetObject: string, relation: string): string {
  return [sourceObject, targetObject, relation].map((item) => normalized(item)).join('\u0000');
}

function safeEvidence(evidence: string[] | undefined): string[] | undefined {
  const result = (evidence ?? [])
    .map((item) => String(item ?? '').trim())
    .filter(Boolean)
    .slice(0, 10);
  return result.length ? result : undefined;
}

function objectTokens(object: SpecObject): string[] {
  return [...new Set([
    object.key,
    object.title,
    object.sourceTable,
    tableSourceFor(object)?.table,
    ...(object.aliases ?? []),
  ].map(normalized).filter((value) => value.length >= 2))];
}

function fieldNameTokens(field: SpecField): string[] {
  return [...new Set([field.key, field.cnName, field.sourceColumn, field.alias].map(normalized).filter(Boolean))];
}

function idStem(token: string): string {
  for (const suffix of ID_SUFFIXES.map(normalized)) {
    if (token === suffix) return '';
    if (token.endsWith(suffix) && token.length > suffix.length) return token.slice(0, -suffix.length);
  }
  return '';
}

function normalized(value: unknown): string {
  return String(value ?? '')
    .normalize('NFKC')
    .trim()
    .toLocaleLowerCase('zh-CN')
    .replace(/[\s_\-\p{P}\p{S}]+/gu, '');
}

function formatRatio(value: number): string {
  return Number.isFinite(value) ? value.toFixed(3) : '0.000';
}

function compareCandidates(left: ObjectRelationSuggestion, right: ObjectRelationSuggestion): number {
  return right.score - left.score
    || confidenceRank(right.confidence) - confidenceRank(left.confidence)
    || left.sourceObject.localeCompare(right.sourceObject)
    || left.targetObject.localeCompare(right.targetObject)
    || left.relation.localeCompare(right.relation)
    || left.candidateFingerprint.localeCompare(right.candidateFingerprint);
}

function confidenceRank(confidence: ObjectRelationSuggestionConfidence): number {
  if (confidence === 'high') return 3;
  if (confidence === 'medium') return 2;
  return 1;
}

function candidateHash(value: Record<string, string>): string {
  const payload = [
    value.sourceObject,
    value.targetObject,
    value.relation,
    value.sourceField,
    value.targetField,
  ].join('\u0000');
  return `rel:${createHash('sha256').update(payload).digest('hex').slice(0, 24)}`;
}

function schemaHash(spec: PackageSpec, tables: DataDictTable[]): string {
  return `sha256:${createHash('sha256').update(JSON.stringify({
    objects: (spec.objects ?? []).map((object) => ({
      key: object.key,
      title: object.title,
      description: object.description,
      aliases: object.aliases ?? [],
      queryBoundary: object.queryBoundary ?? '',
      fields: (object.fields ?? []).map((field) => ({
        key: field.key,
        sourceColumn: field.sourceColumn ?? '',
        type: field.type ?? '',
      })),
      primaryKey: object.primaryKey ?? [],
      sourceTable: object.sourceTable ?? '',
      sourceRefs: object.sourceRefs ?? [],
      relations: (object.relations ?? []).map((relation: SpecRelation) => ({
        relation: relation.relation,
        target: relation.target,
      })),
    })),
    operations: (spec.operations ?? []).map((operation) => ({
      key: operation.key,
      title: operation.title,
      description: operation.description,
      flow: operation.flow ?? [],
      notes: operation.notes ?? [],
      relatedObjects: operation.relatedObjects ?? [],
    })),
    rules: (spec.rules ?? []).map((rule) => ({
      key: rule.key,
      title: rule.title,
      description: rule.description ?? '',
      content: rule.content,
      appliesToObjects: rule.appliesToObjects ?? [],
      appliesToOperations: rule.appliesToOperations ?? [],
      conditions: rule.conditions ?? [],
      exceptions: rule.exceptions ?? [],
    })),
    packageDescription: spec.description ?? '',
    tables: tables.map((table) => ({
      documentId: table.documentId ?? '',
      name: table.name,
      rowCount: table.rowCount,
      fields: table.fields.map((field) => ({
        name: field.name,
        inferredType: field.inferredType,
        distinctRatio: field.distinctRatio,
        nonEmptyRatio: field.nonEmptyRatio ?? null,
      })),
    })),
    relationStatisticProfile: relationStatisticProfile(spec, tables),
  })).digest('hex')}`;
}

function relationStatisticProfile(spec: PackageSpec, tables: DataDictTable[]): Array<Record<string, unknown>> {
  const contexts = objectContexts(spec.objects ?? [], tables);
  const profile: Array<Record<string, unknown>> = [];
  for (const source of contexts) {
    for (const target of contexts) {
      if (source.object.key === target.object.key) continue;
      const targetField = target.primaryFields[0];
      if (!targetField) continue;
      for (const field of source.object.fields ?? []) {
        if (isPrimaryField(source.object, field)) continue;
        const foreignStem = fieldNameTokens(field).map(idStem).find((token) => token.length >= 2);
        if (!foreignStem) continue;
        const matchesTarget = objectTokens(target.object).some((token) => token === foreignStem || token.endsWith(foreignStem));
        if (!matchesTarget) continue;
        const stats = fieldMatchStats(source, field, target, targetField);
        profile.push({
          sourceObject: source.object.key,
          targetObject: target.object.key,
          sourceField: field.key,
          targetField: targetField.key,
          sourceNonEmptyRatio: roundedStat(stats.sourceNonEmptyRatio),
          targetUniqueRatio: roundedStat(stats.targetUniqueRatio),
          joinHitRatio: roundedStat(stats.joinHitRatio),
          unmatchedCount: stats.unmatchedCount,
          duplicateTargetCount: stats.duplicateTargetCount,
          sampleSize: stats.sampleSize,
        });
      }
    }
  }
  return profile.sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
}

function roundedStat(value: number): number {
  return Number.isFinite(value) ? Math.round(value * 1000) / 1000 : 0;
}

function clampInteger(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

export function objectRelationSuggestionCursorOffset(
  cursor: unknown,
  schemaInputHash: string,
): number | null {
  if (typeof cursor !== 'string' || !cursor.trim()) return 0;
  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')) as {
      schemaInputHash?: unknown;
      offset?: unknown;
    };
    if (parsed.schemaInputHash !== schemaInputHash) return null;
    const offset = Number(parsed.offset);
    return Number.isSafeInteger(offset) && offset > 0 ? offset : null;
  } catch {
    return null;
  }
}

function encodeCursor(schemaInputHash: string, offset: number): string {
  return Buffer.from(JSON.stringify({ schemaInputHash, offset }), 'utf8').toString('base64url');
}
