import type { DataDictTable, PackageSpec, SpecObject } from '../types.js';
import {
  tableFieldBindingsFor,
  tableSourceRoleFor,
  tableSourcesFor,
} from './source-references.js';

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
  return findObjectDataMappingProblemsForObjects(spec.objects ?? [], tables, {
    allowMultiSource: spec.dataSourceType === 'neo4j',
  });
}

export function findObjectDataMappingProblemsForObjects(
  objects: SpecObject[],
  tables: DataDictTable[],
  options: { allowMultiSource?: boolean } = {},
): ObjectDataMappingProblem[] {
  const problems: ObjectDataMappingProblem[] = [];

  const resolveTable = (documentId: string | undefined, name: string): DataDictTable | undefined => {
    const exact = tables.find((table) => (
      table.name === name && documentId && table.documentId === documentId
    ));
    if (exact) return exact;
    const matches = tables.filter((table) => table.name === name);
    return matches.length === 1 ? matches[0] : undefined;
  };

  for (const object of objects) {
    const sources = tableSourcesFor(object);
    // 概念对象、运行时输入和专家补充对象允许暂不绑定建模资料。
    if (!sources.length) continue;
    if (sources.length > 1 && !options.allowMultiSource) {
      problems.push({
        objectKey: object.key,
        message: `对象「${object.title || object.key}」同时绑定了 ${sources.length} 张来源表；当前数据源类型不支持跨 Sheet 对象，请拆分为逐来源对象，或改用支持主表与扩展表合并的 Neo4j 图导入`,
      });
      continue;
    }

    if (sources.length > 1) {
      const primarySources = sources.filter((source) => source.role === 'primary');
      const incompleteSources = sources.filter((source) => (
        !source.role || !source.fieldBindings?.length
      ));
      if (primarySources.length !== 1 || incompleteSources.length) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」的跨 Sheet 来源必须声明唯一 primary 主表，并为每张表声明 role 和 fieldBindings`,
        });
        continue;
      }
    }

    const resolvedSources = sources.flatMap((source) => {
      const table = resolveTable(source.documentId, source.table);
      if (table) return [table];
      problems.push({
        objectKey: object.key,
        message: `对象「${object.title || object.key}」的来源表「${source.table}」不存在或同名 Sheet 缺少资料 ID`,
      });
      return [];
    });
    if (resolvedSources.length !== sources.length) continue;

    const objectFields = new Map((object.fields ?? []).map((field) => [field.key, field]));
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
      if (!objectFields.has(primaryKey)) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」主键「${primaryKey}」不属于对象字段`,
        });
      }
    }

    const boundSourcesByField = new Map<string, string[]>();
    for (const [index, source] of sources.entries()) {
      const table = resolvedSources[index];
      const columns = new Set(table.fields.map((field) => field.name));
      const bindings = tableFieldBindingsFor(object, source);
      const bindingKeys = new Set<string>();
      const bindingColumns = new Set<string>();
      for (const binding of bindings) {
        if (!objectFields.has(binding.fieldKey)) {
          problems.push({
            objectKey: object.key,
            message: `对象「${object.title || object.key}」来源表「${source.table}」绑定了不存在的对象字段「${binding.fieldKey}」`,
          });
        }
        if (bindingKeys.has(binding.fieldKey) || bindingColumns.has(binding.sourceColumn)) {
          problems.push({
            objectKey: object.key,
            message: `对象「${object.title || object.key}」来源表「${source.table}」的字段绑定存在重复`,
          });
        }
        bindingKeys.add(binding.fieldKey);
        bindingColumns.add(binding.sourceColumn);
        if (!columns.has(binding.sourceColumn)) {
          problems.push({
            objectKey: object.key,
            message: `对象「${object.title || object.key}」属性「${binding.fieldKey}」对应的来源字段「${binding.sourceColumn}」在来源表「${source.table}」中不存在`,
          });
        }
        boundSourcesByField.set(binding.fieldKey, [
          ...(boundSourcesByField.get(binding.fieldKey) ?? []),
          source.table,
        ]);
      }
      for (const primaryKey of primaryKeys) {
        if (!bindingKeys.has(primaryKey)) {
          problems.push({
            objectKey: object.key,
            message: `对象「${object.title || object.key}」来源表「${source.table}」缺少主键字段「${primaryKey}」的绑定，无法合并跨 Sheet 实例`,
          });
        }
      }
      if (sources.length > 1 && tableSourceRoleFor(object, source) === 'extension' && !bindings.length) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」的扩展来源表「${source.table}」没有字段绑定`,
        });
      }
    }

    for (const field of object.fields ?? []) {
      const boundSources = boundSourcesByField.get(field.key) ?? [];
      if (!boundSources.length) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」属性「${field.key}」没有绑定任何来源字段`,
        });
      } else if (!primaryKeys.includes(field.key) && boundSources.length > 1) {
        problems.push({
          objectKey: object.key,
          message: `对象「${object.title || object.key}」属性「${field.key}」同时由 ${boundSources.map((name) => `「${name}」`).join('、')} 提供，跨 Sheet 合并值存在歧义`,
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
