import type { DataDictTable, PackageSpec, SpecObject } from '../types.js';
import { buildPhysicalSchemaObjects, type PhysicalSchemaObject } from './schema-manifest.js';
import { tableSourceFor, tableSourcesFor } from './source-references.js';

export const OBJECT_DATA_MAPPING_PATH = 'mappings/object-data-mappings.json';

export type ObjectSourceMode = 'schema' | 'virtual';

export interface SchemaObjectPolicyIssue {
  code: string;
  message: string;
  path?: string;
  objectKey?: string;
  table?: string;
}

export function schemaTables(tables: DataDictTable[]): DataDictTable[] {
  return tables.filter((table) => table.materialPurpose === 'schema');
}

export function objectSourceMode(tables: DataDictTable[]): ObjectSourceMode {
  return schemaTables(tables).length ? 'schema' : 'virtual';
}

export function expectedSchemaObjects(tables: DataDictTable[]): PhysicalSchemaObject[] {
  return buildPhysicalSchemaObjects(schemaTables(tables));
}

export function packageNeedsObjectDataMapping(spec: PackageSpec): boolean {
  return spec.objects.some((object) => Boolean(tableSourceFor(object)))
    || Boolean(spec.valueBindings?.length);
}

/**
 * 兼容旧批生成链路：模型只负责虚拟对象的领域语义，物理绑定始终由 Schema 编译器决定。
 * 返回新对象，不修改调用方传入的 spec。
 */
export function enforceObjectSourcePolicy(spec: PackageSpec, tables: DataDictTable[]): PackageSpec {
  const physical = expectedSchemaObjects(tables);
  if (physical.length) {
    return {
      ...spec,
      objects: physical.map((item) => item.object),
    };
  }
  return {
    ...spec,
    objects: spec.objects.map((object) => {
      const next: SpecObject = {
        ...object,
        fields: object.fields.map((field) => {
          const nextField = { ...field };
          delete nextField.sourceColumn;
          delete nextField.valueBindingRef;
          return nextField;
        }),
        sourceRefs: (object.sourceRefs ?? []).filter((source) => source.kind !== 'table'),
      };
      delete next.sourceTable;
      if (!next.sourceRefs?.length) delete next.sourceRefs;
      return next;
    }),
    valueBindings: undefined,
  };
}

function normalizedColumns(object: SpecObject): string[] {
  return object.fields.map((field) => String(field.sourceColumn ?? '').trim()).sort();
}

function expectedColumns(table: DataDictTable): string[] {
  return table.fields.map((field) => field.name.trim()).sort();
}

function fieldSignatures(object: SpecObject): string[] {
  return object.fields.map((field) => JSON.stringify({
    key: field.key,
    cnName: field.cnName ?? '',
    type: field.type ?? 'string',
    required: field.required === true,
    sourceColumn: field.sourceColumn ?? '',
  })).sort();
}

function sameStrings(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

/**
 * 物理 Schema 与虚拟对象是互斥模式。这里校验最终 typed spec 和文件产物，
 * 不依赖模型是否走了正确工具，因此旧运行和恢复运行也无法绕过。
 */
export function schemaObjectPolicyIssues(
  spec: PackageSpec,
  files: Record<string, string>,
  tables: DataDictTable[],
): SchemaObjectPolicyIssue[] {
  const expected = expectedSchemaObjects(tables);
  const mappingPresent = Object.hasOwn(files, OBJECT_DATA_MAPPING_PATH)
    && Boolean(files[OBJECT_DATA_MAPPING_PATH]?.trim());
  const issues: SchemaObjectPolicyIssue[] = [];

  if (!expected.length) {
    for (const object of spec.objects) {
      const source = tableSourceFor(object);
      if (source) {
        issues.push({
          code: 'VIRTUAL_OBJECT_HAS_PHYSICAL_SOURCE',
          objectKey: object.key,
          table: source.table,
          message: `对象 ${object.key} 处于虚拟对象模式，不得绑定物理表 ${source.table}`,
        });
      }
    }
    if (spec.valueBindings?.length) {
      issues.push({
        code: 'VIRTUAL_MODE_HAS_VALUE_BINDINGS',
        message: '没有确定数据 Schema 时不得生成 value bindings 或数据 mapping',
      });
    }
    if (mappingPresent) {
      issues.push({
        code: 'VIRTUAL_MODE_HAS_MAPPING',
        path: OBJECT_DATA_MAPPING_PATH,
        message: '没有确定数据 Schema 时不应生成 object data mapping 文件',
      });
    }
    return issues;
  }

  const expectedByKey = new Map(expected.map((item) => [item.object.key, item]));
  const actualByKey = new Map(spec.objects.map((object) => [object.key, object]));
  for (const { table, object: expectedObject } of expected) {
    const actual = actualByKey.get(expectedObject.key);
    if (!actual) {
      issues.push({
        code: 'SCHEMA_OBJECT_MISSING',
        objectKey: expectedObject.key,
        table: table.name,
        message: `Schema Sheet ${table.name} 缺少对应对象 ${expectedObject.key}`,
      });
      continue;
    }
    const sources = tableSourcesFor(actual);
    const source = sources[0];
    if (sources.length !== 1 || !source || source.table !== table.name || source.documentId !== table.documentId) {
      issues.push({
        code: 'SCHEMA_OBJECT_SOURCE_MISMATCH',
        objectKey: actual.key,
        table: table.name,
        message: `对象 ${actual.key} 必须绑定 Schema Sheet ${table.name} 及其资料版本`,
      });
    }
    if (!sameStrings(normalizedColumns(actual), expectedColumns(table))) {
      issues.push({
        code: 'SCHEMA_OBJECT_FIELDS_MISMATCH',
        objectKey: actual.key,
        table: table.name,
        message: `对象 ${actual.key} 的字段必须完整且一一对应 Schema Sheet ${table.name} 的全部列`,
      });
    }
    if (!sameStrings(fieldSignatures(actual), fieldSignatures(expectedObject))
      || !sameStrings([...(actual.primaryKey ?? [])].sort(), [...(expectedObject.primaryKey ?? [])].sort())) {
      issues.push({
        code: 'SCHEMA_OBJECT_DEFINITION_MISMATCH',
        objectKey: actual.key,
        table: table.name,
        message: `对象 ${actual.key} 的字段 key、类型或主键不符合 Schema Sheet ${table.name} 的确定性定义`,
      });
    }
  }
  for (const object of spec.objects) {
    if (!expectedByKey.has(object.key)) {
      issues.push({
        code: 'DERIVED_OBJECT_FORBIDDEN_IN_SCHEMA_MODE',
        objectKey: object.key,
        message: `已存在确定数据 Schema，不允许额外生成派生对象 ${object.key}`,
      });
    }
  }
  if (!mappingPresent) {
    issues.push({
      code: 'SCHEMA_MAPPING_MISSING',
      path: OBJECT_DATA_MAPPING_PATH,
      message: 'Schema 模式必须生成完整的 object data mapping 文件',
    });
  }
  return issues;
}
