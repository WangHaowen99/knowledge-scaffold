import type { DataDictTable } from '../types.js';
import { assertGraphIdentifier } from '../evaluation/data-source.js';
import type {
  GraphImportEntityPlan,
  GraphImportMapping,
  GraphImportObjectSchema,
  GraphImportPackageSchema,
  GraphImportRelationPlan,
  GraphImportValidation,
  GraphRelationMapping,
} from './types.js';

interface EntitySelection {
  objectKey: string;
}

interface PrimaryColumn {
  property: string;
  sourceColumn: string;
  index: number;
}

export function normalizeGraphImportMapping(mapping: GraphImportMapping): GraphImportMapping {
  const relations = Array.isArray((mapping as any)?.relations) ? (mapping as any).relations : [];
  return {
    relations: relations.map((relation: any) => ({
      sourceObject: String(relation?.sourceObject ?? '').trim(),
      relationName: String(relation?.relationName ?? '').trim(),
      targetObject: String(relation?.targetObject ?? '').trim(),
      sourceJoinProperty: String(relation?.sourceJoinProperty ?? '').trim(),
      targetJoinProperty: String(relation?.targetJoinProperty ?? '').trim(),
    })),
  };
}

function tableForObject(tables: DataDictTable[], object: GraphImportObjectSchema): DataDictTable {
  const table = tables.find((item) => item.name === object.defaultSheet);
  if (!table) {
    throw new Error(`实体「${object.key}」在知识包中固定的 Sheet「${object.defaultSheet}」不存在于 Excel`);
  }
  if (!table.rows) throw new Error(`实体「${object.key}」固定 Sheet「${table.name}」没有可导入的数据行`);
  return table;
}

function objectByKey(schema: GraphImportPackageSchema, key: string): GraphImportObjectSchema {
  const object = schema.objects.find((item) => item.key === key);
  if (!object) throw new Error(`知识包中不存在实体「${key}」`);
  return object;
}

function primaryColumns(object: GraphImportObjectSchema, table: DataDictTable): PrimaryColumn[] {
  if (!object.primaryProperties.length) throw new Error(`实体「${object.key}」未声明主键`);
  return object.primaryProperties.map((property) => {
    const mapping = object.propertyMappings.find((item) => item.property === property);
    if (!mapping) throw new Error(`实体「${object.key}」主键属性「${property}」未声明字段映射`);
    const index = table.fields.findIndex((field) => field.name === mapping.sourceColumn);
    if (index < 0) {
      throw new Error(`实体「${object.key}」主键属性「${property}」对应的 Excel 字段「${mapping.sourceColumn}」不存在`);
    }
    return { property, sourceColumn: mapping.sourceColumn, index };
  });
}

function joinColumn(
  object: GraphImportObjectSchema,
  table: DataDictTable,
  property: string,
): PrimaryColumn {
  const mapping = object.propertyMappings.find((item) => item.property === property);
  if (!mapping) throw new Error(`关系关联属性「${property}」不属于实体「${object.key}」`);
  const index = table.fields.findIndex((field) => field.name === mapping.sourceColumn);
  if (index < 0) {
    throw new Error(
      `实体「${object.key}」关联属性「${property}」对应的 Excel 字段「${mapping.sourceColumn}」不存在`,
    );
  }
  return { property, sourceColumn: mapping.sourceColumn, index };
}

function rowIdentity(row: unknown[], columns: PrimaryColumn[]): string[] {
  return columns.map((column) => String(row[column.index] ?? '').trim());
}

function assertCompleteIdentity(objectKey: string, values: string[], propertiesHaveValue: boolean): boolean {
  const emptyCount = values.filter((value) => !value).length;
  if (emptyCount === values.length) {
    if (!propertiesHaveValue) return false;
    throw new Error(`实体「${objectKey}」存在空主键`);
  }
  if (emptyCount) throw new Error(`实体「${objectKey}」复合主键存在部分空值`);
  return true;
}

function selectEntity(
  selections: Map<string, EntitySelection>,
  objectKey: string,
): void {
  selections.set(objectKey, { objectKey });
}

function buildEntityPlan(
  schema: GraphImportPackageSchema,
  tables: DataDictTable[],
  selection: EntitySelection,
): { plan: GraphImportEntityPlan; warnings: string[] } {
  const object = objectByKey(schema, selection.objectKey);
  const table = tableForObject(tables, object);
  const identities = primaryColumns(object, table);
  const warnings: string[] = [];
  const available = object.propertyMappings.flatMap((mapping) => {
    const index = table.fields.findIndex((field) => field.name === mapping.sourceColumn);
    if (index >= 0) return [{ ...mapping, index }];
    if (!object.primaryProperties.includes(mapping.property)) {
      warnings.push(`实体「${object.key}」属性 ${mapping.property} 对应的 Excel 字段「${mapping.sourceColumn}」不存在，已忽略`);
    }
    return [];
  });
  const unique = new Map<string, { identityValues: string[]; properties: Record<string, string> }>();
  for (const row of table.rows!) {
    const properties = Object.fromEntries(available.map((mapping) => [
      mapping.property,
      String(row[mapping.index] ?? '').trim(),
    ]));
    const identityValues = rowIdentity(row, identities);
    const hasObjectValue = Object.values(properties).some(Boolean);
    if (!assertCompleteIdentity(object.key, identityValues, hasObjectValue)) continue;
    const identityKey = JSON.stringify(identityValues);
    const previous = unique.get(identityKey);
    if (previous && JSON.stringify(previous.properties) !== JSON.stringify(properties)) {
      throw new Error(`实体「${object.key}」唯一键 ${identityValues.join(' + ')} 对应的属性冲突`);
    }
    unique.set(identityKey, { identityValues, properties });
  }
  return {
    warnings,
    plan: {
      objectKey: object.key,
      label: object.label,
      sheet: table.name,
      identityProperties: identities.map((item) => item.property),
      propertyNames: available.map((item) => item.property),
      rows: [...unique.values()],
    },
  };
}

function relationPlan(
  schema: GraphImportPackageSchema,
  tables: DataDictTable[],
  mapping: GraphRelationMapping,
): {
  plan: GraphImportRelationPlan;
  skippedBlank: number;
  skippedUnmatched: number;
  warnings: string[];
} {
  const sourceObject = objectByKey(schema, mapping.sourceObject);
  const targetObject = objectByKey(schema, mapping.targetObject);
  const source = tableForObject(tables, sourceObject);
  const target = tableForObject(tables, targetObject);
  const sourceIdentities = primaryColumns(sourceObject, source);
  const targetIdentities = primaryColumns(targetObject, target);
  const sourceJoinIndex = joinColumn(sourceObject, source, mapping.sourceJoinProperty).index;
  const targetJoinIndex = joinColumn(targetObject, target, mapping.targetJoinProperty).index;

  const targetsByJoin = new Map<string, Map<string, string[]>>();
  for (const row of target.rows!) {
    const join = String(row[targetJoinIndex] ?? '').trim();
    if (!join) continue;
    const identityValues = rowIdentity(row, targetIdentities);
    if (!assertCompleteIdentity(targetObject.key, identityValues, true)) continue;
    const identities = targetsByJoin.get(join) ?? new Map<string, string[]>();
    identities.set(JSON.stringify(identityValues), identityValues);
    targetsByJoin.set(join, identities);
  }

  const pairs = new Map<string, {
    sourceIdentityValues: string[];
    targetIdentityValues: string[];
  }>();
  let skippedBlank = 0;
  let skippedUnmatched = 0;
  for (const row of source.rows!) {
    const join = String(row[sourceJoinIndex] ?? '').trim();
    if (!join) { skippedBlank++; continue; }
    const sourceIdentityValues = rowIdentity(row, sourceIdentities);
    if (!assertCompleteIdentity(sourceObject.key, sourceIdentityValues, true)) continue;
    const targetIdentityValues = targetsByJoin.get(join);
    if (!targetIdentityValues?.size) {
      skippedUnmatched++;
      continue;
    }
    for (const identityValues of targetIdentityValues.values()) {
      pairs.set(JSON.stringify([sourceIdentityValues, identityValues]), {
        sourceIdentityValues,
        targetIdentityValues: identityValues,
      });
    }
  }
  const warnings = skippedUnmatched
    ? [`关系「${mapping.relationName}」有 ${skippedUnmatched} 行非空关联值未匹配目标实例，已跳过对应关系`]
    : [];
  return {
    skippedBlank,
    skippedUnmatched,
    warnings,
    plan: {
      relationName: mapping.relationName,
      sourceObject: sourceObject.key,
      sourceLabel: sourceObject.label,
      sourceIdentityProperties: sourceIdentities.map((item) => item.property),
      targetObject: targetObject.key,
      targetLabel: targetObject.label,
      targetIdentityProperties: targetIdentities.map((item) => item.property),
      pairs: [...pairs.values()],
    },
  };
}

export function validateGraphImportMapping(
  schema: GraphImportPackageSchema,
  tables: DataDictTable[],
  mapping: GraphImportMapping,
): GraphImportValidation {
  const normalizedMapping = normalizeGraphImportMapping(mapping);
  if (!normalizedMapping.relations.length) throw new Error('请至少新增一条实体关系');
  const relationKeys = normalizedMapping.relations.map((item) => {
    const name = String(item.relationName ?? '').trim();
    if (!name) throw new Error('关系名称不能为空');
    assertGraphIdentifier(name, '关系名称');
    return `${item.sourceObject}\u0000${name}\u0000${item.targetObject}`;
  });
  const duplicate = relationKeys.find((key, index, all) => all.indexOf(key) !== index);
  if (duplicate) throw new Error('相同源实体、关系名称和目标实体的关系不能重复添加');

  const selections = new Map<string, EntitySelection>();
  for (const item of normalizedMapping.relations) {
    objectByKey(schema, item.sourceObject);
    objectByKey(schema, item.targetObject);
    selectEntity(selections, item.sourceObject);
    selectEntity(selections, item.targetObject);
  }
  const builtEntities = [...selections.values()].map((selection) => buildEntityPlan(schema, tables, selection));
  const entities = builtEntities.map((item) => item.plan);
  const builtRelations = normalizedMapping.relations.map((item) => relationPlan(schema, tables, item));
  const relations = builtRelations.map((item) => item.plan);
  return {
    nodeCount: entities.reduce((sum, entity) => sum + entity.rows.length, 0),
    relationshipCount: relations.reduce((sum, relation) => sum + relation.pairs.length, 0),
    skippedBlankJoinCount: builtRelations.reduce((sum, item) => sum + item.skippedBlank, 0),
    skippedUnmatchedJoinCount: builtRelations.reduce((sum, item) => sum + item.skippedUnmatched, 0),
    warnings: [...new Set([
      ...builtEntities.flatMap((item) => item.warnings),
      ...builtRelations.flatMap((item) => item.warnings),
    ])],
    entities,
    relations,
  };
}
