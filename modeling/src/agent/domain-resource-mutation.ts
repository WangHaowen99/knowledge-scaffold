import { createHash } from 'node:crypto';

import type { StudioElement, StudioKind, StudioRelation } from './studio-read-service.js';

export type DomainKind = Exclude<StudioKind, 'script'>;

export interface DomainPatchCompilation {
  changes: Record<string, unknown>;
  summary: {
    actionCount: number;
    operations: string[];
  };
}

export interface ObjectPatchCompilation {
  metadata?: Record<string, unknown>;
  field_ops?: {
    add?: Record<string, unknown>[];
    update?: Array<{ key: string; changes: Record<string, unknown> }>;
    remove?: string[];
  };
  relation_ops?: {
    add?: Record<string, unknown>[];
    update?: Array<{ relationId: string; changes: Record<string, unknown> }>;
    remove?: string[];
  };
  summary: {
    actionCount: number;
    operations: string[];
  };
}

export class DomainPatchError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly path: string,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'DomainPatchError';
  }
}

const ALLOWED_ACTIONS: Record<DomainKind, string[]> = {
  object: [
    'set_title', 'set_description', 'set_aliases', 'set_query_boundary',
    'add_field', 'update_field', 'remove_field',
    'add_relation', 'update_relation', 'remove_relation',
  ],
  rule: [
    'set_title', 'set_description', 'set_content', 'attach_evidence', 'detach_evidence',
    'add_condition', 'update_condition', 'remove_condition',
    'add_exception', 'remove_exception', 'add_target', 'remove_target',
  ],
  operation: [
    'set_title', 'set_description', 'attach_evidence', 'detach_evidence',
    'add_input', 'update_input', 'remove_input',
    'add_output', 'update_output', 'remove_output',
    'insert_step', 'update_step', 'move_step', 'remove_step',
    'add_reference', 'remove_reference', 'add_note', 'remove_note',
  ],
  metric: [
    'set_title', 'set_description', 'set_caliber', 'set_unit', 'set_grain',
    'attach_evidence', 'detach_evidence',
    'add_boundary', 'update_boundary', 'remove_boundary',
    'upsert_null_policy', 'remove_null_policy',
    'upsert_dimension', 'remove_dimension',
    'add_object_reference', 'remove_object_reference',
  ],
};

export function domainResourcePayload(element: StudioElement) {
  const kind = assertDomainKind(element.kind);
  const values = structuredClone(element.values);
  const relations = element.relations.map(relationView);
  return {
    id: element.id,
    kind,
    key: element.key,
    title: element.title,
    description: element.description,
    source_path: element.sourcePath,
    resource_hash: domainResourceHash(element),
    values,
    relations,
    editable_items: editableItems(element),
    allowed_actions: ALLOWED_ACTIONS[kind],
  };
}

export function domainResourceHash(element: StudioElement): string {
  const relations = element.relations.map((relation) => ({
    kind: relation.kind,
    targetId: relation.targetId,
    label: relation.label,
    description: relation.description ?? '',
    evidence: relation.evidence ?? [],
  })).sort((left, right) => stableJson(left).localeCompare(stableJson(right)));
  return hashValue({
    schema: 'domain-resource-v2',
    kind: element.kind,
    key: element.key,
    values: element.values,
    relations,
  });
}

export function compileObjectPatchActions(
  element: StudioElement,
  rawActions: unknown,
): ObjectPatchCompilation {
  if (element.kind !== 'object') {
    throw new DomainPatchError('DOMAIN_KIND_MISMATCH', '当前资源不是对象', 'resource_id');
  }
  const actions = parseActions(rawActions);
  const metadata: Record<string, unknown> = {};
  const fieldOps: NonNullable<ObjectPatchCompilation['field_ops']> = {};
  const relationOps: NonNullable<ObjectPatchCompilation['relation_ops']> = {};
  const fields = arrayOfRecords(element.values.fields);
  const operations: string[] = [];
  const touched = new Set<string>();

  actions.forEach((action, index) => {
    const path = `actions[${index}]`;
    const op = actionName(action, path);
    assertAllowed('object', op, path);
    operations.push(op);
    if (op === 'set_title' || op === 'set_description') {
      touch(touched, op, path);
      metadata[op === 'set_title' ? 'title' : 'description'] = requiredText(action.value, `${path}.value`);
      return;
    }
    if (op === 'set_aliases') {
      touch(touched, op, path);
      metadata.aliases = stringArray(action.value, `${path}.value`);
      return;
    }
    if (op === 'set_query_boundary') {
      touch(touched, op, path);
      metadata.queryBoundary = optionalText(action.value, `${path}.value`);
      return;
    }
    if (op === 'add_field') {
      const field = camelCaseDomainRow(record(action.field, `${path}.field`));
      const key = requiredText(field.key, `${path}.field.key`);
      touch(touched, `field:${key}`, path);
      fieldOps.add = [...(fieldOps.add ?? []), field];
      return;
    }
    if (op === 'update_field') {
      const key = requiredText(action.field_key, `${path}.field_key`);
      touch(touched, `field:${key}`, path);
      const current = fields.find((field) => String(field.key ?? '') === key);
      if (!current) throw notFound('字段', key, `${path}.field_key`);
      assertExpectedItemHash(action.expected_field_hash, current, `${path}.expected_field_hash`);
      fieldOps.update = [...(fieldOps.update ?? []), {
        key,
        changes: camelCaseDomainRow(record(action.changes, `${path}.changes`)),
      }];
      return;
    }
    if (op === 'remove_field') {
      const key = requiredText(action.field_key, `${path}.field_key`);
      touch(touched, `field:${key}`, path);
      if (!fields.some((field) => String(field.key ?? '') === key)) {
        throw notFound('字段', key, `${path}.field_key`);
      }
      fieldOps.remove = [...(fieldOps.remove ?? []), key];
      return;
    }
    if (op === 'add_relation') {
      const relation = normalizeRelationInput(record(action.relation, `${path}.relation`), `${path}.relation`);
      touch(touched, `relation-target:${relation.targetId}`, path);
      relationOps.add = [...(relationOps.add ?? []), relation];
      return;
    }
    const relationId = requiredText(action.relation_id, `${path}.relation_id`);
    touch(touched, `relation:${relationId}`, path);
    if (!element.relations.some((relation) => relation.id === relationId)) {
      throw notFound('关系', relationId, `${path}.relation_id`);
    }
    if (op === 'update_relation') {
      relationOps.update = [...(relationOps.update ?? []), {
        relationId,
        changes: normalizeRelationChanges(record(action.changes, `${path}.changes`), `${path}.changes`),
      }];
    } else {
      relationOps.remove = [...(relationOps.remove ?? []), relationId];
    }
  });

  return {
    ...(Object.keys(metadata).length ? { metadata } : {}),
    ...(Object.keys(fieldOps).length ? { field_ops: fieldOps } : {}),
    ...(Object.keys(relationOps).length ? { relation_ops: relationOps } : {}),
    summary: { actionCount: actions.length, operations },
  };
}

export function compileDomainPatchActions(
  element: StudioElement,
  rawActions: unknown,
): DomainPatchCompilation {
  const kind = assertDomainKind(element.kind);
  if (kind === 'object') {
    throw new DomainPatchError('DOMAIN_TOOL_MISMATCH', '对象必须使用对象增量编译器', 'resource_id');
  }
  const actions = parseActions(rawActions);
  const values = structuredClone(element.values);
  let relations = element.relations.map(relationInputFromView);
  const changes: Record<string, unknown> = {};
  const touched = new Set<string>();
  const operations: string[] = [];

  const setValue = (field: string, value: unknown) => {
    values[field] = value;
    changes[field] = value;
  };
  const setRelations = (next: Record<string, unknown>[]) => {
    relations = next;
    changes.relations = next;
  };

  actions.forEach((action, index) => {
    const path = `actions[${index}]`;
    const op = actionName(action, path);
    assertAllowed(kind, op, path);
    operations.push(op);

    if (op === 'set_title' || op === 'set_description') {
      const field = op === 'set_title' ? 'title' : 'description';
      touch(touched, field, path);
      setValue(field, op === 'set_title'
        ? requiredText(action.value, `${path}.value`)
        : optionalText(action.value, `${path}.value`));
      return;
    }
    if (op === 'attach_evidence' || op === 'detach_evidence') {
      const token = requiredText(action.token, `${path}.token`);
      touch(touched, `evidence:${token}`, path);
      const evidence = stringArray(values.evidence ?? [], `${path}.evidence`);
      if (op === 'attach_evidence') {
        if (evidence.includes(token)) throw alreadyExists('证据', token, `${path}.token`);
        setValue('evidence', [...evidence, token]);
      } else {
        if (!evidence.includes(token)) throw notFound('证据', token, `${path}.token`);
        setValue('evidence', evidence.filter((item) => item !== token));
      }
      return;
    }

    if (kind === 'rule') {
      applyRuleAction(action, op, path, element, values, relations, touched, setValue, setRelations);
      return;
    }
    if (kind === 'operation') {
      applyOperationAction(action, op, path, element, values, relations, touched, setValue, setRelations);
      return;
    }
    applyMetricAction(action, op, path, element, values, relations, touched, setValue, setRelations);
  });

  return { changes, summary: { actionCount: actions.length, operations } };
}

function applyRuleAction(
  action: Record<string, unknown>,
  op: string,
  path: string,
  element: StudioElement,
  values: Record<string, unknown>,
  relations: Record<string, unknown>[],
  touched: Set<string>,
  setValue: (field: string, value: unknown) => void,
  setRelations: (value: Record<string, unknown>[]) => void,
): void {
  if (op === 'set_content') {
    touch(touched, 'content', path);
    setValue('content', requiredText(action.value, `${path}.value`));
    return;
  }
  if (op === 'add_condition') {
    const item = record(action.condition, `${path}.condition`);
    const condition = requiredText(item.condition, `${path}.condition.condition`);
    const items = arrayOfRecords(values.conditions);
    if (items.some((entry) => String(entry.condition ?? '') === condition)) {
      throw alreadyExists('规则条件', condition, `${path}.condition.condition`);
    }
    touch(touched, `condition:${condition}`, path);
    setValue('conditions', [...items, {
      condition,
      conclusion: requiredText(item.conclusion, `${path}.condition.conclusion`),
    }]);
    return;
  }
  if (op === 'update_condition' || op === 'remove_condition') {
    const items = arrayOfRecords(values.conditions);
    const index = findItemIndex(element.id, 'conditions', items, action.item_id, path);
    touch(touched, `condition:${itemId(element.id, 'conditions', items[index], index)}`, path);
    if (op === 'remove_condition') {
      setValue('conditions', items.filter((_, itemIndex) => itemIndex !== index));
      return;
    }
    const updates = record(action.changes, `${path}.changes`);
    assertOnlyKeys(updates, ['condition', 'conclusion'], `${path}.changes`);
    setValue('conditions', replaceAt(items, index, { ...items[index], ...updates }));
    return;
  }
  if (op === 'add_exception') {
    const value = requiredText(action.value, `${path}.value`);
    const items = stringArray(values.exceptions ?? [], `${path}.exceptions`);
    if (items.includes(value)) throw alreadyExists('规则例外', value, `${path}.value`);
    touch(touched, `exception:${value}`, path);
    setValue('exceptions', [...items, value]);
    return;
  }
  if (op === 'remove_exception') {
    const items = stringArray(values.exceptions ?? [], `${path}.exceptions`);
    const index = findItemIndex(element.id, 'exceptions', items, action.item_id, path);
    touch(touched, `exception:${itemId(element.id, 'exceptions', items[index], index)}`, path);
    setValue('exceptions', items.filter((_, itemIndex) => itemIndex !== index));
    return;
  }
  applyReferenceAction(action, op, path, 'rule', relations, touched, setRelations);
}

function applyOperationAction(
  action: Record<string, unknown>,
  op: string,
  path: string,
  element: StudioElement,
  values: Record<string, unknown>,
  relations: Record<string, unknown>[],
  touched: Set<string>,
  setValue: (field: string, value: unknown) => void,
  setRelations: (value: Record<string, unknown>[]) => void,
): void {
  if (['add_input', 'update_input', 'remove_input'].includes(op)) {
    applyKeyedTableAction(action, op, path, 'input', 'inputs', 'param', values, touched, setValue);
    return;
  }
  if (['add_output', 'update_output', 'remove_output'].includes(op)) {
    applyKeyedTableAction(action, op, path, 'output', 'outputs', 'field', values, touched, setValue);
    return;
  }
  if (op === 'insert_step') {
    const value = requiredText(action.value, `${path}.value`);
    const items = uniqueStrings(values.flow, `${path}.flow`);
    if (items.includes(value)) throw alreadyExists('流程步骤', value, `${path}.value`);
    touch(touched, `step:${value}`, path);
    const position = insertionIndex(element.id, 'flow', items, action.after_id, path);
    setValue('flow', [...items.slice(0, position), value, ...items.slice(position)]);
    return;
  }
  if (['update_step', 'move_step', 'remove_step'].includes(op)) {
    const items = uniqueStrings(values.flow, `${path}.flow`);
    const index = findItemIndex(element.id, 'flow', items, action.item_id, path);
    touch(touched, `step:${itemId(element.id, 'flow', items[index], index)}`, path);
    if (op === 'remove_step') {
      setValue('flow', items.filter((_, itemIndex) => itemIndex !== index));
    } else if (op === 'update_step') {
      const next = requiredText(action.value, `${path}.value`);
      if (items.some((item, itemIndex) => item === next && itemIndex !== index)) {
        throw alreadyExists('流程步骤', next, `${path}.value`);
      }
      setValue('flow', replaceAt(items, index, next));
    } else {
      const remaining = items.filter((_, itemIndex) => itemIndex !== index);
      const position = insertionIndex(element.id, 'flow', remaining, action.after_id, path);
      setValue('flow', [...remaining.slice(0, position), items[index], ...remaining.slice(position)]);
    }
    return;
  }
  if (op === 'add_note') {
    const value = requiredText(action.value, `${path}.value`);
    const items = uniqueStrings(values.notes ?? [], `${path}.notes`);
    if (items.includes(value)) throw alreadyExists('注意事项', value, `${path}.value`);
    touch(touched, `note:${value}`, path);
    setValue('notes', [...items, value]);
    return;
  }
  if (op === 'remove_note') {
    const items = uniqueStrings(values.notes ?? [], `${path}.notes`);
    const index = findItemIndex(element.id, 'notes', items, action.item_id, path);
    touch(touched, `note:${itemId(element.id, 'notes', items[index], index)}`, path);
    setValue('notes', items.filter((_, itemIndex) => itemIndex !== index));
    return;
  }
  applyReferenceAction(action, op, path, 'operation', relations, touched, setRelations);
}

function applyMetricAction(
  action: Record<string, unknown>,
  op: string,
  path: string,
  element: StudioElement,
  values: Record<string, unknown>,
  relations: Record<string, unknown>[],
  touched: Set<string>,
  setValue: (field: string, value: unknown) => void,
  setRelations: (value: Record<string, unknown>[]) => void,
): void {
  const scalarFields: Record<string, string> = {
    set_caliber: 'caliber', set_unit: 'unit', set_grain: 'grain',
  };
  if (scalarFields[op]) {
    const field = scalarFields[op];
    touch(touched, field, path);
    setValue(field, op === 'set_caliber'
      ? requiredText(action.value, `${path}.value`)
      : optionalText(action.value, `${path}.value`));
    return;
  }
  if (op === 'add_boundary') {
    const value = requiredText(action.value, `${path}.value`);
    const items = uniqueStrings(values.boundaries, `${path}.boundaries`);
    if (items.includes(value)) throw alreadyExists('计算边界', value, `${path}.value`);
    touch(touched, `boundary:${value}`, path);
    setValue('boundaries', [...items, value]);
    return;
  }
  if (op === 'update_boundary' || op === 'remove_boundary') {
    const items = uniqueStrings(values.boundaries, `${path}.boundaries`);
    const index = findItemIndex(element.id, 'boundaries', items, action.item_id, path);
    touch(touched, `boundary:${itemId(element.id, 'boundaries', items[index], index)}`, path);
    if (op === 'remove_boundary') {
      setValue('boundaries', items.filter((_, itemIndex) => itemIndex !== index));
    } else {
      const value = requiredText(action.value, `${path}.value`);
      if (items.some((item, itemIndex) => item === value && itemIndex !== index)) {
        throw alreadyExists('计算边界', value, `${path}.value`);
      }
      setValue('boundaries', replaceAt(items, index, value));
    }
    return;
  }
  if (op === 'upsert_null_policy' || op === 'remove_null_policy') {
    applyUpsertTableAction(action, op, path, 'null_policy', 'nullPolicy', 'field', values, touched, setValue);
    return;
  }
  if (op === 'upsert_dimension' || op === 'remove_dimension') {
    applyUpsertTableAction(action, op, path, 'dimension', 'dimensions', 'key', values, touched, setValue);
    return;
  }
  applyReferenceAction(action, op, path, 'metric', relations, touched, setRelations);
}

function applyKeyedTableAction(
  action: Record<string, unknown>,
  op: string,
  path: string,
  noun: string,
  field: string,
  keyField: string,
  values: Record<string, unknown>,
  touched: Set<string>,
  setValue: (field: string, value: unknown) => void,
): void {
  const items = arrayOfRecords(values[field]);
  if (op.startsWith('add_')) {
    const item = camelCaseDomainRow(record(action[noun], `${path}.${noun}`));
    const key = requiredText(item[keyField], `${path}.${noun}.${keyField}`);
    if (items.some((entry) => String(entry[keyField] ?? '') === key)) {
      throw alreadyExists(noun, key, `${path}.${noun}.${keyField}`);
    }
    touch(touched, `${field}:${key}`, path);
    setValue(field, [...items, item]);
    return;
  }
  const key = requiredText(action[`${noun}_key`], `${path}.${noun}_key`);
  const index = items.findIndex((entry) => String(entry[keyField] ?? '') === key);
  if (index < 0) throw notFound(noun, key, `${path}.${noun}_key`);
  touch(touched, `${field}:${key}`, path);
  if (op.startsWith('remove_')) {
    setValue(field, items.filter((_, itemIndex) => itemIndex !== index));
    return;
  }
  const updates = camelCaseDomainRow(record(action.changes, `${path}.changes`));
  if (updates[keyField] !== undefined && String(updates[keyField]) !== key) {
    throw new DomainPatchError(
      'DOMAIN_KEY_IMMUTABLE',
      `${keyField} 是稳定标识，不能通过 update 修改`,
      `${path}.changes.${keyField}`,
    );
  }
  setValue(field, replaceAt(items, index, { ...items[index], ...updates }));
}

function applyUpsertTableAction(
  action: Record<string, unknown>,
  op: string,
  path: string,
  noun: string,
  field: string,
  keyField: string,
  values: Record<string, unknown>,
  touched: Set<string>,
  setValue: (field: string, value: unknown) => void,
): void {
  const items = arrayOfRecords(values[field]);
  const key = requiredText(action[keyField], `${path}.${keyField}`);
  const index = items.findIndex((entry) => String(entry[keyField] ?? '') === key);
  touch(touched, `${field}:${key}`, path);
  if (op.startsWith('remove_')) {
    if (index < 0) throw notFound(noun, key, `${path}.${keyField}`);
    setValue(field, items.filter((_, itemIndex) => itemIndex !== index));
    return;
  }
  const rawValue = action.value;
  const valueField = field === 'nullPolicy' ? 'handling' : 'note';
  const next = { [keyField]: key, [valueField]: requiredText(rawValue, `${path}.value`) };
  setValue(field, index < 0 ? [...items, next] : replaceAt(items, index, next));
}

function applyReferenceAction(
  action: Record<string, unknown>,
  op: string,
  path: string,
  sourceKind: 'rule' | 'operation' | 'metric',
  relations: Record<string, unknown>[],
  touched: Set<string>,
  setRelations: (value: Record<string, unknown>[]) => void,
): void {
  const targetId = requiredText(action.target_id, `${path}.target_id`);
  const kindBySource: Record<typeof sourceKind, Record<string, string>> = {
    rule: { object: 'applies_to_object', operation: 'applies_to_operation' },
    operation: { object: 'uses_object', rule: 'uses_rule', metric: 'uses_metric' },
    metric: { object: 'uses_object' },
  };
  const targetKind = targetId.split(':', 1)[0];
  const relationKind = kindBySource[sourceKind][targetKind];
  if (!relationKind) {
    throw new DomainPatchError(
      'DOMAIN_REFERENCE_KIND_INVALID',
      `${sourceKind} 不能引用 ${targetKind || '未知'} 类型`,
      `${path}.target_id`,
    );
  }
  const existingIndex = relations.findIndex((item) => (
    String(item.kind ?? '') === relationKind && String(item.targetId ?? '') === targetId
  ));
  touch(touched, `reference:${relationKind}:${targetId}`, path);
  const adding = op.startsWith('add_');
  if (adding && existingIndex >= 0) throw alreadyExists('引用', targetId, `${path}.target_id`);
  if (!adding && existingIndex < 0) throw notFound('引用', targetId, `${path}.target_id`);
  setRelations(adding
    ? [...relations, { kind: relationKind, targetId }]
    : relations.filter((_, index) => index !== existingIndex));
}

function editableItems(element: StudioElement): Record<string, unknown[]> {
  const result: Record<string, unknown[]> = {};
  const addRecords = (field: string) => {
    const values = arrayOfRecords(element.values[field]);
    result[field] = values.map((value, index) => ({
      item_id: itemId(element.id, field, value, index),
      item_hash: hashValue(value),
      ...value,
    }));
  };
  const addStrings = (field: string) => {
    const values = stringArray(element.values[field] ?? [], field);
    result[field] = values.map((value, index) => ({
      item_id: itemId(element.id, field, value, index),
      item_hash: hashValue(value),
      value,
    }));
  };
  if (element.kind === 'object') addRecords('fields');
  if (element.kind === 'rule') {
    addRecords('conditions');
    addStrings('exceptions');
  }
  if (element.kind === 'operation') {
    addRecords('inputs');
    addRecords('outputs');
    addStrings('flow');
    addStrings('notes');
  }
  if (element.kind === 'metric') {
    addStrings('boundaries');
    addRecords('nullPolicy');
    addRecords('dimensions');
  }
  return result;
}

function findItemIndex(
  resourceId: string,
  collection: string,
  items: unknown[],
  rawItemId: unknown,
  path: string,
): number {
  const expected = requiredText(rawItemId, `${path}.item_id`);
  const matches = items.flatMap((item, itemIndex) => (
    itemId(resourceId, collection, item, itemIndex) === expected ? [itemIndex] : []
  ));
  if (!matches.length) throw notFound('子项', expected, `${path}.item_id`);
  if (matches.length > 1) {
    throw new DomainPatchError(
      'DOMAIN_ITEM_AMBIGUOUS',
      '历史资源包含重复子项，无法安全定位；请先消除重复内容',
      `${path}.item_id`,
      { itemId: expected, matches: matches.length },
    );
  }
  return matches[0];
}

function insertionIndex(
  resourceId: string,
  collection: string,
  items: unknown[],
  rawAfterId: unknown,
  path: string,
): number {
  if (rawAfterId === undefined || rawAfterId === null || rawAfterId === '') return items.length;
  if (rawAfterId === 'start') return 0;
  return findItemIndex(resourceId, collection, items, rawAfterId, `${path}.after`) + 1;
}

function itemId(resourceId: string, collection: string, value: unknown, index: number): string {
  const identity = semanticItemIdentity(collection, value, index);
  const digest = createHash('sha256')
    .update('domain-item-v2\0')
    .update(resourceId)
    .update('\0')
    .update(collection)
    .update('\0')
    .update(identity)
    .digest('hex')
    .slice(0, 20);
  return `${collection}:${digest}`;
}

function semanticItemIdentity(collection: string, value: unknown, index: number): string {
  if (typeof value === 'string') return `text:${value}`;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const item = value as Record<string, unknown>;
    const keyFields: Record<string, string> = {
      fields: 'key',
      conditions: 'condition',
      inputs: 'param',
      outputs: 'field',
      nullPolicy: 'field',
      dimensions: 'key',
    };
    const keyField = keyFields[collection];
    const key = keyField ? String(item[keyField] ?? '').trim() : '';
    if (key) return `${keyField}:${key}`;
    return `record:${stableJson(item)}`;
  }
  return `legacy:${index}:${stableJson(value)}`;
}

function relationView(relation: StudioRelation) {
  return {
    id: relation.id,
    kind: relation.kind,
    label: relation.label,
    target_id: relation.targetId,
    target_kind: relation.targetKind,
    target_key: relation.targetKey,
    target_exists: relation.targetExists,
    ...(relation.description ? { description: relation.description } : {}),
  };
}

function relationInputFromView(relation: StudioRelation): Record<string, unknown> {
  return {
    kind: relation.kind,
    targetId: relation.targetId,
    ...(relation.label ? { label: relation.label } : {}),
    ...(relation.description ? { description: relation.description } : {}),
  };
}

function normalizeRelationInput(value: Record<string, unknown>, path: string): Record<string, unknown> {
  assertOnlyKeys(value, ['kind', 'target_id', 'label', 'description'], path);
  return {
    kind: value.kind === undefined ? 'object_relation' : requiredText(value.kind, `${path}.kind`),
    targetId: requiredText(value.target_id, `${path}.target_id`),
    ...(value.label !== undefined ? { label: optionalText(value.label, `${path}.label`) } : {}),
    ...(value.description !== undefined
      ? { description: optionalText(value.description, `${path}.description`) }
      : {}),
  };
}

function camelCaseDomainRow(value: Record<string, unknown>): Record<string, unknown> {
  const normalized = { ...value };
  if (Object.hasOwn(normalized, 'cn_name')) {
    normalized.cnName = normalized.cn_name;
    delete normalized.cn_name;
  }
  if (Object.hasOwn(normalized, 'source_column')) {
    normalized.sourceColumn = normalized.source_column;
    delete normalized.source_column;
  }
  return normalized;
}

function normalizeRelationChanges(value: Record<string, unknown>, path: string): Record<string, unknown> {
  assertOnlyKeys(value, ['kind', 'target_id', 'label', 'description'], path);
  return {
    ...(value.kind !== undefined ? { kind: requiredText(value.kind, `${path}.kind`) } : {}),
    ...(value.target_id !== undefined ? { targetId: requiredText(value.target_id, `${path}.target_id`) } : {}),
    ...(value.label !== undefined ? { label: optionalText(value.label, `${path}.label`) } : {}),
    ...(value.description !== undefined
      ? { description: optionalText(value.description, `${path}.description`) }
      : {}),
  };
}

function assertExpectedItemHash(expected: unknown, value: unknown, path: string): void {
  if (expected === undefined || expected === null || expected === '') return;
  const normalized = requiredText(expected, path);
  const actual = hashValue(value);
  if (normalized !== actual) {
    throw new DomainPatchError(
      'ITEM_HASH_CONFLICT',
      '目标子项已经变化，请重新读取资源后生成 Patch',
      path,
      { expectedItemHash: normalized, actualItemHash: actual },
    );
  }
}

function assertDomainKind(value: StudioKind): DomainKind {
  if (value === 'script') {
    throw new DomainPatchError('DOMAIN_KIND_READ_ONLY', '脚本是只读资源', 'resource_id');
  }
  return value;
}

function parseActions(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 100) {
    throw new DomainPatchError('DOMAIN_ACTIONS_INVALID', 'actions 必须包含 1 到 100 个动作', 'actions');
  }
  return value.map((item, index) => record(item, `actions[${index}]`));
}

function actionName(action: Record<string, unknown>, path: string): string {
  return requiredText(action.op, `${path}.op`);
}

function assertAllowed(kind: DomainKind, op: string, path: string): void {
  if (!ALLOWED_ACTIONS[kind].includes(op)) {
    throw new DomainPatchError(
      'DOMAIN_ACTION_NOT_ALLOWED',
      `${kind} 不支持动作 ${op}`,
      `${path}.op`,
      { allowedActions: ALLOWED_ACTIONS[kind] },
    );
  }
}

function touch(touched: Set<string>, target: string, path: string): void {
  if (touched.has(target)) {
    throw new DomainPatchError(
      'DOMAIN_ACTION_CONFLICT',
      '同一请求不能重复修改同一目标',
      path,
      { target },
    );
  }
  touched.add(target);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new DomainPatchError('DOMAIN_VALUE_INVALID', '必须是对象', path);
  }
  return value as Record<string, unknown>;
}

function requiredText(value: unknown, path: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new DomainPatchError('DOMAIN_VALUE_REQUIRED', '不能为空', path);
  }
  return value.trim();
}

function optionalText(value: unknown, path: string): string {
  if (typeof value !== 'string') {
    throw new DomainPatchError('DOMAIN_VALUE_INVALID', '必须是文本', path);
  }
  return value.trim();
}

function stringArray(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) {
    throw new DomainPatchError('DOMAIN_VALUE_INVALID', '必须是文本数组', path);
  }
  return value.map((item, index) => requiredText(item, `${path}[${index}]`));
}

function uniqueStrings(value: unknown, path: string): string[] {
  const result = stringArray(value, path);
  if (new Set(result).size !== result.length) {
    throw new DomainPatchError('DOMAIN_DUPLICATE_ITEM', '列表中存在重复内容，无法稳定定位', path);
  }
  return result;
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => (
      Boolean(item) && typeof item === 'object' && !Array.isArray(item)
    )).map((item) => structuredClone(item))
    : [];
}

function assertOnlyKeys(value: Record<string, unknown>, allowed: string[], path: string): void {
  const unknown = Object.keys(value).find((key) => !allowed.includes(key));
  if (unknown) {
    throw new DomainPatchError('DOMAIN_FIELD_UNKNOWN', '包含未知字段', `${path}.${unknown}`);
  }
}

function replaceAt<T>(items: T[], index: number, value: T): T[] {
  return items.map((item, itemIndex) => (itemIndex === index ? value : item));
}

function notFound(noun: string, key: string, path: string): DomainPatchError {
  return new DomainPatchError('DOMAIN_ITEM_NOT_FOUND', `${noun}不存在：${key}`, path, { key });
}

function alreadyExists(noun: string, key: string, path: string): DomainPatchError {
  return new DomainPatchError('DOMAIN_ITEM_EXISTS', `${noun}已存在：${key}`, path, { key });
}

function hashValue(value: unknown): string {
  return `sha256:${createHash('sha256').update(stableJson(value)).digest('hex')}`;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}
