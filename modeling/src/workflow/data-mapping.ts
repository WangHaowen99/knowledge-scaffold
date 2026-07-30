import type { DataDictTable, PackageSpec, SpecObject } from '../types.js';
import { tableSourcesFor } from './source-references.js';

export interface ObjectDataMappingProblem {
  objectKey: string;
  message: string;
}

export function normalizeObjectDataMappings(
  spec: PackageSpec,
  tables: DataDictTable[],
): PackageSpec {
  return { ...spec, objects: normalizeObjectDataMappingsForObjects(spec.objects ?? [], tables) };
}

export function normalizeObjectDataMappingsForObjects(
  objects: SpecObject[],
  tables: DataDictTable[],
): SpecObject[] {
  const tableByName = new Map(tables.map((table) => [table.name, table]));
  return objects.map((object) => {
    const resolvedTables = tableSourcesFor(object)
      .map((source) => tableByName.get(source.table))
      .filter((table): table is DataDictTable => Boolean(table));
    if (!resolvedTables.length) return object;

    const columns = resolvedTables.flatMap((table) => table.fields.map((field) => field.name));
    const exactColumns = new Set(columns);
    const columnsByLowerName = new Map<string, string[]>();
    for (const column of columns) {
      const lowerName = column.toLowerCase();
      columnsByLowerName.set(lowerName, [...new Set([...(columnsByLowerName.get(lowerName) ?? []), column])]);
    }
    const fields = (object.fields ?? []).map((field) => {
      const sourceColumn = field.sourceColumn || field.key;
      if (exactColumns.has(sourceColumn)) return field;
      const matches = columnsByLowerName.get(sourceColumn.toLowerCase()) ?? [];
      return matches.length === 1 ? { ...field, sourceColumn: matches[0] } : field;
    });
    return fields.some((field, index) => field !== object.fields[index])
      ? { ...object, fields }
      : object;
  });
}

export function findObjectDataMappingProblems(
  spec: PackageSpec,
  tables: DataDictTable[],
): ObjectDataMappingProblem[] {
  return findObjectDataMappingProblemsForObjects(spec.objects ?? [], tables);
}

export function findObjectDataMappingProblemsForObjects(
  objects: SpecObject[],
  tables: DataDictTable[],
): ObjectDataMappingProblem[] {
  const tableByName = new Map(tables.map((table) => [table.name, table]));
  const problems: ObjectDataMappingProblem[] = [];

  for (const object of objects) {
    const sources = tableSourcesFor(object);
    // 概念对象、运行时输入和专家补充对象允许暂不绑定建模资料。
    if (!sources.length) continue;

    const resolvedTables = sources.flatMap((source) => {
      const table = tableByName.get(source.table);
      if (table) return [table];
      problems.push({
        objectKey: object.key,
        message: `对象「${object.title || object.key}」的来源表「${source.table}」不存在`,
      });
      return [];
    });
    if (!resolvedTables.length) continue;

    const tableColumns = new Set(resolvedTables.flatMap((table) => table.fields.map((field) => field.name)));
    const sourceLabel = resolvedTables.map((table) => `「${table.name}」`).join('、');
    const objectFields = new Map((object.fields ?? []).map((field) => [field.key, field]));
    for (const field of object.fields ?? []) {
      const sourceColumn = field.sourceColumn || field.key;
      if (!tableColumns.has(sourceColumn)) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」属性「${field.key}」对应的来源字段「${sourceColumn}」在来源表 ${sourceLabel} 中不存在`,
        });
      }
    }

    const primaryKeys = object.primaryKey?.length
      ? object.primaryKey
      : [object.fields?.[0]?.key].filter((key): key is string => !!key);
    if (!primaryKeys.length) {
      problems.push({
        objectKey: object.key,
        message: `对象「${object.title || object.key}」没有可用主键`,
      });
    }
    for (const primaryKey of primaryKeys) {
      const field = objectFields.get(primaryKey);
      if (!field) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」主键「${primaryKey}」不属于对象字段`,
        });
        continue;
      }
      const sourceColumn = field.sourceColumn || field.key;
      if (!tableColumns.has(sourceColumn)) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」主键「${primaryKey}」对应的来源字段「${sourceColumn}」在来源表 ${sourceLabel} 中不存在`,
        });
      }
    }
  }

  return problems;
}

export function assertObjectDataMappings(spec: PackageSpec, tables: DataDictTable[]): void {
  const problems = findObjectDataMappingProblems(spec, tables);
  if (problems.length) {
    throw new Error(`知识包数据映射无效：${problems.map((problem) => problem.message).join('；')}`);
  }
}
