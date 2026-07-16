import type { DataDictTable, PackageSpec } from '../types.js';

export interface ObjectDataMappingProblem {
  objectKey: string;
  message: string;
}

export function findObjectDataMappingProblems(
  spec: PackageSpec,
  tables: DataDictTable[],
): ObjectDataMappingProblem[] {
  const tableByName = new Map(tables.map((table) => [table.name, table]));
  const problems: ObjectDataMappingProblem[] = [];

  for (const object of spec.objects ?? []) {
    if (!object.sourceTable) {
      problems.push({
        objectKey: object.key,
        message: `对象「${object.title || object.key}」必须声明来源 Sheet，且来源 Sheet 必须是已上传 Excel 数据表中的一个`,
      });
      continue;
    }
    const table = tableByName.get(object.sourceTable);
    if (!table) {
      problems.push({
        objectKey: object.key,
        message: `对象「${object.title || object.key}」的来源表「${object.sourceTable}」在已上传 Excel 数据表中不存在`,
      });
      continue;
    }

    const tableColumns = new Set(table.fields.map((field) => field.name));
    const objectFields = new Map((object.fields ?? []).map((field) => [field.key, field]));
    for (const field of object.fields ?? []) {
      const sourceColumn = field.sourceColumn || field.key;
      if (!tableColumns.has(sourceColumn)) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」属性「${field.key}」对应的来源字段「${sourceColumn}」在表「${table.name}」中不存在`,
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
          message: `对象「${object.title || object.key}」主键「${primaryKey}」对应的来源字段「${sourceColumn}」在表「${table.name}」中不存在`,
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
