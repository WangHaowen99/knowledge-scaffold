import type { DataDictTable } from '../types.js';
import { assertGraphIdentifier } from '../evaluation/data-source.js';
import type {
  GraphImportEntityPlan,
  GraphImportMapping,
  GraphImportObjectSchema,
  GraphImportPackageSchema,
  GraphImportRelationPlan,
  GraphImportValidation,
  GraphImportValueBindingSchema,
  GraphRelationMapping,
} from './types.js';
import {
  graphImportCellValue,
  inferGraphImportColumns,
  type GraphImportColumnProfile,
  type GraphImportInferenceIssue,
} from './value-inference.js';

interface MappedColumn {
  property: string;
  sourceColumn: string;
  index: number;
}

interface TypedMappedColumn extends MappedColumn {
  profile: GraphImportColumnProfile;
}

interface BuiltEntity {
  plan: GraphImportEntityPlan;
  warnings: string[];
  recordKeyByRowIndex: Map<number, string>;
}

type GraphImportEntitySchema = GraphImportObjectSchema | GraphImportValueBindingSchema;

export function normalizeGraphImportMapping(mapping: GraphImportMapping): GraphImportMapping {
  const relations = (mapping as any)?.relations;
  if (!Array.isArray(relations)) throw new Error('mapping.relations 必须是数组');
  return {
    relations: relations.map((relation: any) => {
      const base = {
        sourceObject: String(relation?.sourceObject ?? '').trim(),
        relationName: String(relation?.relationName ?? '').trim(),
        targetObject: String(relation?.targetObject ?? '').trim(),
      };
      const matchMode = String(relation?.matchMode ?? '').trim() || 'field_match';
      if (matchMode === 'same_row') return { ...base, matchMode };
      if (matchMode !== 'field_match') throw new Error(`未知的关系关联方式「${matchMode}」`);
      return {
        ...base,
        matchMode,
        sourceJoinProperty: String(relation?.sourceJoinProperty ?? '').trim(),
        targetJoinProperty: String(relation?.targetJoinProperty ?? '').trim(),
      };
    }),
  };
}

function tableForObject(tables: DataDictTable[], object: GraphImportEntitySchema): DataDictTable {
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

function joinColumn(
  object: GraphImportEntitySchema,
  table: DataDictTable,
  property: string,
): MappedColumn {
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

function recordKey(objectKey: string, rowIndex: number): string {
  return `node:${objectKey}:${String(rowIndex + 1).padStart(8, '0')}`;
}

function inferenceWarning(
  objectKey: string,
  property: string,
  issue: GraphImportInferenceIssue,
): string {
  const prefix = `实体「${objectKey}」属性 ${property}`;
  if (issue === 'mixed_types') return `${prefix} 对应列包含混合类型，已按字符串导入`;
  if (issue === 'integer_out_of_range') {
    return `${prefix} 存在超出 Neo4j 64 位整数范围的值，已按字符串导入`;
  }
  if (issue === 'unsafe_excel_integer') {
    return `${prefix} 存在超出 JavaScript 安全整数范围的 Excel 数值，已按字符串导入`;
  }
  return `${prefix} 存在超过 Neo4j FLOAT 精度的数值，已按字符串导入`;
}

function buildEntityPlan(
  tables: DataDictTable[],
  profilesBySheet: Map<string, GraphImportColumnProfile[]>,
  object: GraphImportEntitySchema,
): BuiltEntity {
  const table = tableForObject(tables, object);
  const columnProfiles = profilesBySheet.get(table.name)!;
  const warnings: string[] = [];
  const available: TypedMappedColumn[] = object.propertyMappings.flatMap((mapping) => {
    const index = table.fields.findIndex((field) => field.name === mapping.sourceColumn);
    if (index >= 0) {
      const profile = columnProfiles[index];
      warnings.push(...profile.issues.map((issue) => inferenceWarning(object.key, mapping.property, issue)));
      return [{ ...mapping, index, profile }];
    }
    warnings.push(
      `实体「${object.key}」属性 ${mapping.property} 对应的 Excel 字段「${mapping.sourceColumn}」不存在，已忽略`,
    );
    return [];
  });
  const rows: GraphImportEntityPlan['rows'] = [];
  const recordKeyByRowIndex = new Map<number, string>();
  for (const [rowIndex, row] of table.rows!.entries()) {
    const properties = Object.fromEntries(available.map((mapping) => [
      mapping.property,
      graphImportCellValue(table, rowIndex, mapping.index, mapping.profile.type),
    ]));
    if (!Object.values(properties).some(Boolean)) continue;
    const key = recordKey(object.key, rowIndex);
    rows.push({ recordKey: key, properties });
    recordKeyByRowIndex.set(rowIndex, key);
  }
  return {
    warnings,
    recordKeyByRowIndex,
    plan: {
      objectKey: object.key,
      label: object.label,
      sheet: table.name,
      propertyNames: available.map((item) => item.property),
      propertyTypes: Object.fromEntries(available.map((item) => [item.property, item.profile.type])),
      rows,
    },
  };
}

function relationPlan(
  schema: GraphImportPackageSchema,
  tables: DataDictTable[],
  builtEntities: Map<string, BuiltEntity>,
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
  const sourceBuilt = builtEntities.get(sourceObject.key)!;
  const targetBuilt = builtEntities.get(targetObject.key)!;
  if (mapping.matchMode === 'same_row') {
    if (source.name !== target.name) {
      throw new Error(`关系「${mapping.relationName}」仅同一 Sheet 的实体支持同一数据行关联`);
    }
    const pairs = [...sourceBuilt.recordKeyByRowIndex.entries()].flatMap(([rowIndex, sourceRecordKey]) => {
      const targetRecordKey = targetBuilt.recordKeyByRowIndex.get(rowIndex);
      return targetRecordKey ? [{ sourceRecordKey, targetRecordKey }] : [];
    });
    return {
      skippedBlank: 0,
      skippedUnmatched: 0,
      warnings: [],
      plan: {
        relationName: mapping.relationName,
        sourceObject: sourceObject.key,
        sourceLabel: sourceObject.label,
        targetObject: targetObject.key,
        targetLabel: targetObject.label,
        pairs,
      },
    };
  }
  if (!mapping.sourceJoinProperty || !mapping.targetJoinProperty) {
    throw new Error(`关系「${mapping.relationName}」按字段值匹配时必须配置源和目标关联属性`);
  }
  const sourceJoinIndex = joinColumn(sourceObject, source, mapping.sourceJoinProperty).index;
  const targetJoinIndex = joinColumn(targetObject, target, mapping.targetJoinProperty).index;

  const targetsByJoin = new Map<string, string[]>();
  for (const [rowIndex, row] of target.rows!.entries()) {
    const targetRecordKey = targetBuilt.recordKeyByRowIndex.get(rowIndex);
    if (!targetRecordKey) continue;
    const join = String(row[targetJoinIndex] ?? '').trim();
    if (!join) continue;
    const targetRecordKeys = targetsByJoin.get(join) ?? [];
    targetRecordKeys.push(targetRecordKey);
    targetsByJoin.set(join, targetRecordKeys);
  }

  const pairs: GraphImportRelationPlan['pairs'] = [];
  let skippedBlank = 0;
  let skippedUnmatched = 0;
  for (const [rowIndex, row] of source.rows!.entries()) {
    const sourceRecordKey = sourceBuilt.recordKeyByRowIndex.get(rowIndex);
    if (!sourceRecordKey) continue;
    const join = String(row[sourceJoinIndex] ?? '').trim();
    if (!join) {
      skippedBlank++;
      continue;
    }
    const targetRecordKeys = targetsByJoin.get(join);
    if (!targetRecordKeys?.length) {
      skippedUnmatched++;
      continue;
    }
    for (const targetRecordKey of targetRecordKeys) {
      pairs.push({ sourceRecordKey, targetRecordKey });
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
      targetObject: targetObject.key,
      targetLabel: targetObject.label,
      pairs,
    },
  };
}

export function validateGraphImportMapping(
  schema: GraphImportPackageSchema,
  tables: DataDictTable[],
  mapping: GraphImportMapping,
): GraphImportValidation {
  const normalizedMapping = normalizeGraphImportMapping(mapping);
  const relationKeys = normalizedMapping.relations.map((item) => {
    const name = String(item.relationName ?? '').trim();
    if (!name) throw new Error('关系名称不能为空');
    assertGraphIdentifier(name, '关系名称');
    return `${item.sourceObject}\u0000${name}\u0000${item.targetObject}`;
  });
  const duplicate = relationKeys.find((key, index, all) => all.indexOf(key) !== index);
  if (duplicate) throw new Error('相同源实体、关系名称和目标实体的关系不能重复添加');

  for (const item of normalizedMapping.relations) {
    objectByKey(schema, item.sourceObject);
    objectByKey(schema, item.targetObject);
  }
  const profilesBySheet = new Map(tables.map((table) => [table.name, inferGraphImportColumns(table)]));
  const importableEntities = [...schema.objects, ...(schema.valueBindings ?? [])];
  const builtEntityList = importableEntities.map((object) => (
    buildEntityPlan(tables, profilesBySheet, object)
  ));
  const builtEntities = new Map(builtEntityList.map((item) => [item.plan.objectKey, item]));
  const entities = builtEntityList.map((item) => item.plan);
  const builtRelations = normalizedMapping.relations.map((item) => (
    relationPlan(schema, tables, builtEntities, item)
  ));
  const relations = builtRelations.map((item) => item.plan);
  return {
    nodeCount: entities.reduce((sum, entity) => sum + entity.rows.length, 0),
    relationshipCount: relations.reduce((sum, relation) => sum + relation.pairs.length, 0),
    skippedBlankJoinCount: builtRelations.reduce((sum, item) => sum + item.skippedBlank, 0),
    skippedUnmatchedJoinCount: builtRelations.reduce((sum, item) => sum + item.skippedUnmatched, 0),
    warnings: [...new Set([
      ...builtEntityList.flatMap((item) => item.warnings),
      ...builtRelations.flatMap((item) => item.warnings),
    ])],
    entities,
    relations,
  };
}
