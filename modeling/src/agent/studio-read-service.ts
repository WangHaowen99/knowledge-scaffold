import { createHash } from 'node:crypto';

import type {
  PackageSpec,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRule,
} from '../types.js';
import { parseRenderedPackageFiles } from '../workflow/package-import.js';

export const STUDIO_SCHEMA_VERSION = 'studio-v3';

export type StudioKind = 'object' | 'operation' | 'rule' | 'metric' | 'concept';

export interface StudioWorkingPackage {
  workingRevision: string;
  baseRevision: string;
  candidateRevision: string | null;
  files: Record<string, string>;
}

export interface StudioRelation {
  id: string;
  kind: string;
  label: string;
  sourceId: string;
  targetId: string;
  targetKind: StudioKind;
  targetKey: string;
  targetExists: boolean;
  description?: string;
  evidence?: string[];
}

export interface StudioElementSummary {
  id: string;
  kind: StudioKind;
  key: string;
  title: string;
  description: string;
  sourcePath: string;
  relationCount: number;
}

export interface StudioElement extends StudioElementSummary {
  values: Record<string, unknown>;
  relations: StudioRelation[];
}

type StudioSpec = SpecObject | SpecOperation | SpecRule | SpecMetric;

interface SpecElementSeed {
  kind: Exclude<StudioKind, 'concept'>;
  spec: StudioSpec;
}

interface ConceptElementSeed {
  kind: 'concept';
  spec: {
    key: string;
    title: string;
    description: string;
    layer: 'common' | 'parameterized';
    decision: 'preserve_views';
    members: Array<{
      relation: 'equivalent' | 'broader' | 'narrower' | 'overlap';
      targetKind: Exclude<StudioKind, 'concept'>;
      targetKey: string;
    }>;
  };
}

type ElementSeed = SpecElementSeed | ConceptElementSeed;

interface ListOptions {
  kind?: unknown;
  query?: unknown;
  sort?: unknown;
  order?: unknown;
  cursor?: unknown;
  limit?: unknown;
}

interface GraphOptions {
  kind?: unknown;
  query?: unknown;
}

const KIND_ORDER: StudioKind[] = ['object', 'operation', 'rule', 'metric', 'concept'];
const KIND_SET = new Set<string>(KIND_ORDER);
const KIND_DIRECTORY: Record<StudioKind, string> = {
  object: 'objects',
  operation: 'operations',
  rule: 'rules',
  metric: 'metrics',
  concept: 'mappings',
};
const KIND_LABEL: Record<StudioKind, string> = {
  object: '对象',
  operation: '操作',
  rule: '规则',
  metric: '指标',
  concept: '融合概念',
};

const COMMON_COLUMNS = [
  { key: 'title', label: '名称', sortable: true },
  { key: 'key', label: 'Key', sortable: true },
  { key: 'description', label: '说明', sortable: false },
  { key: 'relationCount', label: '关系数', sortable: false },
] as const;

export const STUDIO_SCHEMA = {
  schemaVersion: STUDIO_SCHEMA_VERSION,
  idFormat: 'kind:key',
  capabilities: {
    read: true,
    create: true,
    update: true,
    delete: true,
    batch: true,
  },
  kinds: [
    {
      kind: 'object', label: KIND_LABEL.object, directory: KIND_DIRECTORY.object,
      capabilities: { create: true, update: true, delete: true, batch: true },
      fields: [
        { key: 'title', label: '名称', type: 'text', required: true },
        { key: 'description', label: '业务定义', type: 'long_text', required: true },
        { key: 'aliases', label: '别名', type: 'text_list', required: false },
        { key: 'fields', label: '字段', type: 'field_table', required: true, readOnly: true },
        { key: 'queryBoundary', label: '查询边界', type: 'long_text', required: false },
        { key: 'sources', label: '知识来源', type: 'source_list', required: false, readOnly: true },
      ],
      tableColumns: COMMON_COLUMNS,
      legalRelations: [{ kind: 'object_relation', targetKinds: ['object'] }],
      bulkEditableFields: ['description'],
    },
    {
      kind: 'operation', label: KIND_LABEL.operation, directory: KIND_DIRECTORY.operation,
      capabilities: { create: true, update: true, delete: true, batch: true },
      fields: [
        { key: 'title', label: '名称', type: 'text', required: true },
        { key: 'description', label: '业务目标', type: 'long_text', required: true },
        { key: 'intentExamples', label: '胜任力问题', type: 'text_list', required: false },
        { key: 'inputs', label: '输入', type: 'parameter_table', required: true },
        { key: 'outputs', label: '输出', type: 'output_table', required: true },
        { key: 'flow', label: '流程', type: 'ordered_text_list', required: true },
        { key: 'notes', label: '注意事项', type: 'text_list', required: false },
        { key: 'evidence', label: '知识证据', type: 'evidence_list', required: false, readOnly: true },
      ],
      tableColumns: COMMON_COLUMNS,
      legalRelations: [
        { kind: 'uses_object', targetKinds: ['object'] },
        { kind: 'uses_rule', targetKinds: ['rule'] },
        { kind: 'uses_metric', targetKinds: ['metric'] },
      ],
      bulkEditableFields: ['description'],
    },
    {
      kind: 'rule', label: KIND_LABEL.rule, directory: KIND_DIRECTORY.rule,
      capabilities: { create: true, update: true, delete: true, batch: true },
      fields: [
        { key: 'title', label: '名称', type: 'text', required: true },
        { key: 'description', label: '说明', type: 'long_text', required: false },
        { key: 'content', label: '规则内容', type: 'long_text', required: true },
        { key: 'conditions', label: '判断条件', type: 'condition_table', required: false },
        { key: 'exceptions', label: '例外', type: 'text_list', required: false },
        { key: 'evidence', label: '知识证据', type: 'evidence_list', required: true, readOnly: true },
      ],
      tableColumns: COMMON_COLUMNS,
      legalRelations: [{ kind: 'applies_to_object', targetKinds: ['object'] }],
      bulkEditableFields: ['description'],
    },
    {
      kind: 'metric', label: KIND_LABEL.metric, directory: KIND_DIRECTORY.metric,
      capabilities: { create: true, update: true, delete: true, batch: true },
      fields: [
        { key: 'title', label: '名称', type: 'text', required: true },
        { key: 'description', label: '说明', type: 'long_text', required: false },
        { key: 'unit', label: '单位', type: 'text', required: false },
        { key: 'grain', label: '粒度', type: 'text', required: false },
        { key: 'caliber', label: '业务口径', type: 'long_text', required: true },
        { key: 'boundaries', label: '计算边界', type: 'text_list', required: true },
        { key: 'nullPolicy', label: '空值策略', type: 'key_value_table', required: false },
        { key: 'dimensions', label: '维度', type: 'key_value_table', required: false },
        { key: 'evidence', label: '知识证据', type: 'evidence_list', required: true, readOnly: true },
      ],
      tableColumns: COMMON_COLUMNS,
      legalRelations: [{ kind: 'uses_object', targetKinds: ['object'] }],
      bulkEditableFields: ['description'],
    },
    {
      kind: 'concept', label: KIND_LABEL.concept, directory: KIND_DIRECTORY.concept,
      capabilities: { create: false, update: false, delete: false, batch: false },
      fields: [
        { key: 'title', label: '名称', type: 'text', required: true, readOnly: true },
        { key: 'description', label: '裁决说明', type: 'long_text', required: true, readOnly: true },
        { key: 'layer', label: '语义层', type: 'text', required: true, readOnly: true },
        { key: 'decision', label: '融合策略', type: 'text', required: true, readOnly: true },
        { key: 'members', label: '来源成员', type: 'key_value_table', required: true, readOnly: true },
      ],
      tableColumns: COMMON_COLUMNS,
      legalRelations: [{
        kind: 'concept_member',
        targetKinds: ['object', 'operation', 'rule', 'metric'],
      }],
      bulkEditableFields: [],
    },
  ],
} as const;

export class StudioReadError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly statusCode: number,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'StudioReadError';
  }
}

export class StudioReadService {
  private readonly elements: StudioElement[];
  private readonly byId: Map<string, StudioElement>;

  constructor(private readonly workingPackage: StudioWorkingPackage) {
    let spec: PackageSpec;
    try {
      spec = parseRenderedPackageFiles(workingPackage.files);
    } catch (error) {
      throw new StudioReadError(
        'INVALID_WORKING_PACKAGE',
        error instanceof Error ? error.message : '当前工作修订无法解析',
        409,
        { workingRevision: workingPackage.workingRevision },
      );
    }
    const seeds = elementSeeds(spec, workingPackage.files);
    const ids = new Set(seeds.map((seed) => elementId(seed.kind, seed.spec.key)));
    this.elements = seeds.map((seed) => projectElement(seed, ids));
    this.byId = new Map(this.elements.map((element) => [element.id, element]));
  }

  assertRevision(expected: unknown): void {
    if (expected === undefined || expected === null || String(expected).trim() === '') return;
    const normalized = String(expected).trim();
    if (normalized !== this.workingPackage.workingRevision) {
      throw new StudioReadError(
        'WORKING_REVISION_CHANGED',
        '知识包工作修订已经变化，请刷新后重试',
        409,
        {
          expectedRevision: normalized,
          actualRevision: this.workingPackage.workingRevision,
        },
      );
    }
  }

  list(options: ListOptions = {}) {
    const filtered = this.filter(options);
    const { sort, direction } = parseSort(options.sort, options.order);
    filtered.sort((left, right) => compareElements(left, right, sort, direction));
    const offset = decodeCursor(options.cursor, this.workingPackage.workingRevision);
    const limit = parseLimit(options.limit);
    const items = filtered.slice(offset, offset + limit).map(summaryOf);
    const nextOffset = offset + items.length;
    return {
      workingRevision: this.workingPackage.workingRevision,
      items,
      total: filtered.length,
      nextCursor: nextOffset < filtered.length
        ? encodeCursor(nextOffset, this.workingPackage.workingRevision)
        : null,
    };
  }

  detail(rawElementId: unknown) {
    const id = String(rawElementId ?? '').trim();
    const element = this.byId.get(id);
    if (!element) {
      throw new StudioReadError('ELEMENT_NOT_FOUND', '知识条目不存在', 404, { elementId: id });
    }
    return {
      workingRevision: this.workingPackage.workingRevision,
      element,
    };
  }

  graph(options: GraphOptions = {}) {
    const elements = this.filter(options);
    const positions = layoutElements(elements);
    const ids = new Set(elements.map((element) => element.id));
    const edges = elements.flatMap((element) => element.relations)
      .filter((relation) => ids.has(relation.targetId))
      .map((relation) => ({
        id: relation.id,
        source: relation.sourceId,
        target: relation.targetId,
        kind: relation.kind,
        label: relation.label,
      }));
    return {
      workingRevision: this.workingPackage.workingRevision,
      nodes: elements.map((element) => ({
        id: element.id,
        nodeType: 'element' as const,
        kind: element.kind,
        label: element.title,
        ...positions.get(element.id)!,
        size: 32,
      })),
      edges,
      aggregates: [],
      nextCursor: null,
      totals: { elements: elements.length, relations: edges.length },
    };
  }

  private filter(options: GraphOptions): StudioElement[] {
    const kind = parseKind(options.kind);
    const query = String(options.query ?? '').trim().toLocaleLowerCase('zh-CN');
    return this.elements.filter((element) => (
      (!kind || element.kind === kind)
      && (!query || searchableText(element).includes(query))
    ));
  }
}

function elementSeeds(spec: PackageSpec, files: Record<string, string>): ElementSeed[] {
  return [
    ...spec.objects.map((item) => ({ kind: 'object' as const, spec: item })),
    ...spec.operations.map((item) => ({ kind: 'operation' as const, spec: item })),
    ...(spec.rules ?? []).map((item) => ({ kind: 'rule' as const, spec: item })),
    ...(spec.metrics ?? []).map((item) => ({ kind: 'metric' as const, spec: item })),
    ...fusionConceptSeeds(files),
  ];
}

function fusionConceptSeeds(files: Record<string, string>): ConceptElementSeed[] {
  const raw = files['mappings/package-fusion.json'];
  if (!raw) return [];
  let ledger: any;
  try {
    ledger = JSON.parse(raw);
  } catch {
    throw new StudioReadError(
      'INVALID_WORKING_PACKAGE',
      '融合来源映射无法解析',
      409,
      { sourcePath: 'mappings/package-fusion.json' },
    );
  }
  const resources = Array.isArray(ledger?.resources) ? ledger.resources : [];
  const fusedKeyByMember = new Map(resources.map((entry: any) => [
    `${String(entry?.sourceId ?? '')}\0${String(entry?.kind ?? '')}\0${String(entry?.sourceKey ?? '')}`,
    String(entry?.fusedKey ?? ''),
  ]));
  const studioKinds = new Set(['object', 'operation', 'rule', 'metric']);
  const concepts = Array.isArray(ledger?.concepts) ? ledger.concepts : [];
  return concepts.flatMap((concept: any): ConceptElementSeed[] => {
    const key = String(concept?.id ?? '').trim();
    const layer = String(concept?.layer ?? '');
    if (!key || !['common', 'parameterized'].includes(layer)) return [];
    const members = (Array.isArray(concept?.members) ? concept.members : []).flatMap((member: any) => {
      const targetKind = String(member?.kind ?? '');
      if (!studioKinds.has(targetKind)) return [];
      const fusedKey = String(member?.fusedKey ?? '').trim() || fusedKeyByMember.get(
        `${String(member?.sourceId ?? '')}\0${targetKind}\0${String(member?.key ?? '')}`,
      ) || '';
      if (!fusedKey) return [];
      const relation = String(member?.relation ?? 'overlap');
      return [{
        relation: ['equivalent', 'broader', 'narrower', 'overlap'].includes(relation)
          ? relation as 'equivalent' | 'broader' | 'narrower' | 'overlap'
          : 'overlap' as const,
        targetKind: targetKind as Exclude<StudioKind, 'concept'>,
        targetKey: fusedKey,
      }];
    });
    return [{
      kind: 'concept',
      spec: {
        key,
        title: String(concept?.title ?? '').trim() || key,
        description: String(concept?.rationale ?? '').trim(),
        layer: layer as 'common' | 'parameterized',
        decision: 'preserve_views',
        members,
      },
    }];
  });
}

function projectElement(seed: ElementSeed, knownIds: Set<string>): StudioElement {
  const id = elementId(seed.kind, seed.spec.key);
  const sourcePath = seed.kind === 'concept'
    ? 'mappings/package-fusion.json'
    : `${KIND_DIRECTORY[seed.kind]}/${seed.spec.key}.md`;
  const values = valuesOf(seed);
  const relations = relationsOf(seed, id, knownIds);
  return {
    id,
    kind: seed.kind,
    key: seed.spec.key,
    title: seed.spec.title || seed.spec.key,
    description: descriptionOf(seed),
    sourcePath,
    relationCount: relations.length,
    values,
    relations,
  };
}

function valuesOf(seed: ElementSeed): Record<string, unknown> {
  if (seed.kind === 'concept') {
    return {
      title: seed.spec.title,
      description: seed.spec.description,
      layer: seed.spec.layer,
      decision: seed.spec.decision,
      members: seed.spec.members.map((member) => ({
        relation: member.relation,
        target: elementId(member.targetKind, member.targetKey),
      })),
    };
  }
  if (seed.kind === 'object') {
    const item = seed.spec as SpecObject;
    return {
      title: item.title,
      description: item.description,
      aliases: item.aliases ?? [],
      fields: item.fields,
      queryBoundary: item.queryBoundary ?? '',
      sources: item.sourceRefs ?? [],
    };
  }
  if (seed.kind === 'operation') {
    const item = seed.spec as SpecOperation;
    return {
      title: item.title,
      description: item.description,
      intentExamples: item.competencyQuestion ? [item.competencyQuestion] : [],
      inputs: item.inputs,
      outputs: item.outputs,
      flow: item.flow,
      notes: item.notes ?? [],
      script: item.script ?? null,
      evidence: item.evidence ?? [],
    };
  }
  if (seed.kind === 'rule') {
    const item = seed.spec as SpecRule;
    return {
      title: item.title,
      description: item.description ?? '',
      content: item.content,
      conditions: item.conditions ?? [],
      exceptions: item.exceptions ?? [],
      evidence: item.evidence ?? [],
    };
  }
  const item = seed.spec as SpecMetric;
  return {
    title: item.title,
    description: item.description ?? '',
    unit: item.unit ?? '',
    grain: item.grain ?? '',
    caliber: item.caliber,
    boundaries: item.boundaries,
    nullPolicy: item.nullPolicy ?? [],
    dimensions: item.dimensions ?? [],
    evidence: item.evidence ?? [],
  };
}

function descriptionOf(seed: ElementSeed): string {
  if (seed.kind === 'concept') return seed.spec.description;
  if (seed.kind === 'object' || seed.kind === 'operation') return seed.spec.description ?? '';
  if (seed.kind === 'rule') {
    const rule = seed.spec as SpecRule;
    return rule.description || rule.content || '';
  }
  const metric = seed.spec as SpecMetric;
  return metric.description || metric.caliber || '';
}

function relationsOf(seed: ElementSeed, sourceId: string, knownIds: Set<string>): StudioRelation[] {
  const inputs: Array<{
    kind: string;
    label: string;
    targetKind: StudioKind;
    targetKey: string;
    description?: string;
    evidence?: string[];
  }> = [];
  if (seed.kind === 'concept') {
    inputs.push(...seed.spec.members.map((member) => ({
      kind: 'concept_member',
      label: conceptRelationLabel(member.relation),
      targetKind: member.targetKind,
      targetKey: member.targetKey,
    })));
  } else if (seed.kind === 'object') {
    for (const relation of (seed.spec as SpecObject).relations ?? []) {
      const target = parseTarget(relation.target, 'object');
      inputs.push({
        kind: 'object_relation',
        label: relation.relation,
        targetKind: target.kind,
        targetKey: target.key,
        description: relation.note,
        evidence: relation.evidence,
      });
    }
  } else if (seed.kind === 'operation') {
    const operation = seed.spec as SpecOperation;
    inputs.push(...operation.relatedObjects.map((key) => ({
      kind: 'uses_object', label: '使用对象', targetKind: 'object' as const, targetKey: key,
    })));
    inputs.push(...(operation.usesRules ?? []).map((key) => ({
      kind: 'uses_rule', label: '使用规则', targetKind: 'rule' as const, targetKey: key,
    })));
    inputs.push(...(operation.usesMetrics ?? []).map((key) => ({
      kind: 'uses_metric', label: '使用指标', targetKind: 'metric' as const, targetKey: key,
    })));
  } else if (seed.kind === 'rule') {
    inputs.push(...((seed.spec as SpecRule).appliesToObjects ?? []).map((key) => ({
      kind: 'applies_to_object', label: '适用于对象', targetKind: 'object' as const, targetKey: key,
    })));
  } else {
    inputs.push(...((seed.spec as SpecMetric).usesObjects ?? []).map((key) => ({
      kind: 'uses_object', label: '使用对象', targetKind: 'object' as const, targetKey: key,
    })));
  }
  const relations = inputs.map((input) => {
    const targetId = elementId(input.targetKind, input.targetKey);
    const identity = JSON.stringify([sourceId, input.kind, targetId, input.label, input.description, input.evidence]);
    return {
      id: `edge:${createHash('sha256').update(identity).digest('hex').slice(0, 20)}`,
      ...input,
      sourceId,
      targetId,
      targetExists: knownIds.has(targetId),
    };
  });
  return [...new Map(relations.map((relation) => [relation.id, relation])).values()];
}

function conceptRelationLabel(relation: ConceptElementSeed['spec']['members'][number]['relation']): string {
  if (relation === 'equivalent') return '同一概念视图';
  if (relation === 'broader') return '上位概念';
  if (relation === 'narrower') return '下位概念';
  return '部分重叠';
}

function parseTarget(raw: string, fallbackKind: StudioKind): { kind: StudioKind; key: string } {
  const value = String(raw ?? '').trim();
  const separator = value.indexOf(':');
  if (separator > 0) {
    const possibleKind = value.slice(0, separator);
    if (KIND_SET.has(possibleKind)) {
      return { kind: possibleKind as StudioKind, key: value.slice(separator + 1) };
    }
  }
  return { kind: fallbackKind, key: value };
}

function elementId(kind: StudioKind, key: string): string {
  return `${kind}:${key}`;
}

function summaryOf(element: StudioElement): StudioElementSummary {
  const { values: _values, relations: _relations, ...summary } = element;
  return summary;
}

function searchableText(element: StudioElement): string {
  const aliases = element.kind === 'object' && Array.isArray(element.values.aliases)
    ? element.values.aliases.join(' ')
    : '';
  return `${element.kind} ${KIND_LABEL[element.kind]} ${element.key} ${element.title} ${element.description} ${aliases}`
    .toLocaleLowerCase('zh-CN');
}

function parseKind(value: unknown): StudioKind | undefined {
  if (value === undefined || value === null || String(value).trim() === '') return undefined;
  const kind = String(value).trim();
  if (!KIND_SET.has(kind)) {
    throw new StudioReadError('INVALID_STUDIO_QUERY', '不支持的知识类型', 400, { kind });
  }
  return kind as StudioKind;
}

function parseSort(sortValue: unknown, orderValue: unknown): {
  sort: 'title' | 'key' | 'kind';
  direction: 1 | -1;
} {
  const raw = String(sortValue ?? 'title').trim();
  const descendingPrefix = raw.startsWith('-');
  const sort = (descendingPrefix ? raw.slice(1) : raw) || 'title';
  if (!['title', 'key', 'kind'].includes(sort)) {
    throw new StudioReadError('INVALID_STUDIO_QUERY', '不支持的排序字段', 400, { sort });
  }
  const order = String(orderValue ?? '').trim().toLowerCase();
  if (order && !['asc', 'desc'].includes(order)) {
    throw new StudioReadError('INVALID_STUDIO_QUERY', '排序方向只能是 asc 或 desc', 400, { order });
  }
  return {
    sort: sort as 'title' | 'key' | 'kind',
    direction: descendingPrefix || order === 'desc' ? -1 : 1,
  };
}

function compareElements(
  left: StudioElement,
  right: StudioElement,
  sort: 'title' | 'key' | 'kind',
  direction: 1 | -1,
): number {
  const primary = left[sort].localeCompare(right[sort], 'zh-CN') * direction;
  return primary || left.id.localeCompare(right.id, 'en');
}

function parseLimit(value: unknown): number {
  if (value === undefined || value === null || String(value).trim() === '') return 50;
  const limit = Number(value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
    throw new StudioReadError('INVALID_STUDIO_QUERY', 'limit 必须是 1 到 200 的整数', 400, { limit: value });
  }
  return limit;
}

function encodeCursor(offset: number, workingRevision: string): string {
  return Buffer.from(JSON.stringify({ offset, workingRevision }), 'utf8').toString('base64url');
}

function decodeCursor(value: unknown, workingRevision: string): number {
  if (value === undefined || value === null || String(value).trim() === '') return 0;
  try {
    const parsed = JSON.parse(Buffer.from(String(value), 'base64url').toString('utf8')) as {
      offset?: unknown;
      workingRevision?: unknown;
    };
    if (!Number.isInteger(parsed.offset) || Number(parsed.offset) < 0) throw new Error('invalid offset');
    if (parsed.workingRevision !== workingRevision) {
      throw new StudioReadError(
        'WORKING_REVISION_CHANGED',
        '分页期间知识包工作修订已经变化，请从第一页重新加载',
        409,
        { expectedRevision: parsed.workingRevision, actualRevision: workingRevision },
      );
    }
    return Number(parsed.offset);
  } catch (error) {
    if (error instanceof StudioReadError) throw error;
    throw new StudioReadError('INVALID_STUDIO_CURSOR', '分页游标无效', 400);
  }
}

function layoutElements(elements: StudioElement[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  let x = 0;
  for (const kind of KIND_ORDER) {
    const group = elements.filter((element) => element.kind === kind)
      .sort((left, right) => left.id.localeCompare(right.id, 'en'));
    if (!group.length) continue;
    const rows = Math.min(8, group.length);
    const columns = Math.ceil(group.length / rows);
    group.forEach((element, index) => {
      const column = Math.floor(index / rows);
      const row = index % rows;
      const columnRows = Math.min(rows, group.length - column * rows);
      positions.set(element.id, {
        x: x + column * 180,
        y: (row - (columnRows - 1) / 2) * 92,
      });
    });
    x += columns * 180 + 60;
  }
  const values = [...positions.values()];
  const centerX = values.length
    ? (Math.min(...values.map((item) => item.x)) + Math.max(...values.map((item) => item.x))) / 2
    : 0;
  for (const [id, position] of positions) positions.set(id, { ...position, x: position.x - centerX });
  return positions;
}
