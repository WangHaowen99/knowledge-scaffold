import { createHash } from 'node:crypto';

import type { DataDictTable, PackageSpec, SpecField, SpecObject } from '../types.js';
import { expectedSchemaObjects, schemaTables } from './schema-object-policy.js';
import { isSchemaFallbackDescription } from './schema-semantic-shared.js';

export const SCHEMA_SEMANTICS_VALIDATOR_VERSION = 'schema-semantics-v3';

export type SchemaSemanticIssueLevel = 'error' | 'warning';

export type SchemaSemanticIssueCode =
  | 'OBJECT_TITLE_PLACEHOLDER'
  | 'OBJECT_DESCRIPTION_PLACEHOLDER'
  | 'FIELD_CN_NAME_PLACEHOLDER'
  | 'FIELD_REQUIRED_INVALID';

export interface SchemaSemanticIssue {
  id: string;
  code: SchemaSemanticIssueCode;
  ruleId: string;
  level: SchemaSemanticIssueLevel;
  path: string;
  objectKey: string;
  fieldKey?: string;
  table: string;
  documentId?: string;
  sourceColumn?: string;
  message: string;
  suggestion: string;
}

export interface SchemaSemanticStatus {
  available: boolean;
  pass: boolean;
  validatorVersion: string;
  fingerprint: string;
  objectCount: number;
  fieldCount: number;
  errorCount: number;
  warningCount: number;
  issues: SchemaSemanticIssue[];
}

const normalize = (value: unknown) => String(value ?? '').trim().normalize('NFKC');
const identity = (value: unknown) => normalize(value).toLocaleLowerCase('zh-CN').replace(/[\s_-]+/gu, '');
const containsCjk = (value: unknown) => /[\u3400-\u9fff]/u.test(normalize(value));
const isAsciiTechnical = (value: unknown) => {
  const text = normalize(value);
  return Boolean(text) && !containsCjk(text) && /^[A-Za-z_][A-Za-z0-9_.-]*$/u.test(text);
};

function isObjectTitlePlaceholder(object: SpecObject, tableName: string, requireLocalizedName: boolean): boolean {
  const title = normalize(object.title);
  if (!title) return true;
  if (!requireLocalizedName) return false;
  if (!isAsciiTechnical(title)) return false;
  return [object.key, tableName].some((candidate) => identity(candidate) === identity(title));
}

function isFieldNamePlaceholder(field: SpecField, requireLocalizedName: boolean): boolean {
  const name = normalize(field.cnName);
  if (!name) return true;
  if (!requireLocalizedName) return false;
  if (!isAsciiTechnical(name)) return false;
  return [field.key, field.sourceColumn]
    .filter(Boolean)
    .some((candidate) => identity(candidate) === identity(name));
}

function issue(input: Omit<SchemaSemanticIssue, 'id' | 'ruleId'>): SchemaSemanticIssue {
  const ruleId = `schema.semantic.${input.code.toLocaleLowerCase('en-US').replaceAll('_', '-')}`;
  const digest = createHash('sha256').update(JSON.stringify({
    code: input.code,
    path: input.path,
    objectKey: input.objectKey,
    fieldKey: input.fieldKey ?? '',
  })).digest('hex').slice(0, 20);
  return { ...input, id: `ssi_${digest}`, ruleId };
}

export function analyzeSchemaSemantics(spec: PackageSpec, tables: DataDictTable[]): SchemaSemanticStatus {
  const physicalTables = schemaTables(tables);
  if (!physicalTables.length) {
    return {
      available: false,
      pass: true,
      validatorVersion: SCHEMA_SEMANTICS_VALIDATOR_VERSION,
      fingerprint: '',
      objectCount: 0,
      fieldCount: 0,
      errorCount: 0,
      warningCount: 0,
      issues: [],
    };
  }

  const expected = expectedSchemaObjects(physicalTables);
  const actualByKey = new Map(spec.objects.map((object) => [object.key, object]));
  const actualBySourceTable = new Map<string, SpecObject[]>();
  for (const object of spec.objects) {
    if (!object.sourceTable) continue;
    const matches = actualBySourceTable.get(object.sourceTable);
    if (matches) matches.push(object);
    else actualBySourceTable.set(object.sourceTable, [object]);
  }
  const resolved = expected.map(({ table, object: expectedObject }) => {
    const exact = actualByKey.get(expectedObject.key);
    const sourceMatches = actualBySourceTable.get(expectedObject.sourceTable ?? '') ?? [];
    return {
      table,
      expectedObject,
      object: exact ?? (sourceMatches.length === 1 ? sourceMatches[0] : undefined),
    };
  });
  const issues: SchemaSemanticIssue[] = [];
  const requireLocalizedName = !spec.language
    || spec.language.toLocaleLowerCase('en-US').startsWith('zh');
  let fieldCount = 0;
  for (const { table, expectedObject, object } of resolved) {
    if (!object) continue; // Object-source validation reports missing physical objects.
    const path = `objects/${object.key}.md`;
    if (isObjectTitlePlaceholder(object, table.name, requireLocalizedName)) {
      issues.push(issue({
        code: 'OBJECT_TITLE_PLACEHOLDER',
        level: 'error',
        path,
        objectKey: object.key,
        table: table.name,
        ...(table.documentId ? { documentId: table.documentId } : {}),
        message: `对象 ${object.key} 仍使用技术标识作为业务标题`,
        suggestion: '根据已确认资料填写业务人员能够识别的对象名称',
      }));
    }
    if (isSchemaFallbackDescription(object.description, table.name)) {
      issues.push(issue({
        code: 'OBJECT_DESCRIPTION_PLACEHOLDER',
        level: 'error',
        path,
        objectKey: object.key,
        table: table.name,
        ...(table.documentId ? { documentId: table.documentId } : {}),
        message: `对象 ${object.key} 仍使用 Schema 同步占位说明`,
        suggestion: '补充对象的业务用途、边界和使用场景',
      }));
    }

    const fieldsBySource = new Map(object.fields.map((field) => [
      normalize(field.sourceColumn || field.key),
      field,
    ]));
    for (const expectedField of expectedObject.fields) {
      const field = fieldsBySource.get(normalize(expectedField.sourceColumn || expectedField.key));
      if (!field) continue; // Object-source validation reports physical field drift.
      fieldCount += 1;
      if (isFieldNamePlaceholder(field, requireLocalizedName)) {
        issues.push(issue({
          code: 'FIELD_CN_NAME_PLACEHOLDER',
          level: 'error',
          path,
          objectKey: object.key,
          fieldKey: field.key,
          table: table.name,
          ...(table.documentId ? { documentId: table.documentId } : {}),
          sourceColumn: field.sourceColumn || expectedField.sourceColumn || expectedField.key,
          message: `对象 ${object.key} 的字段 ${field.key} 尚无明确业务名称`,
          suggestion: '依据数据字典或专家决定填写字段中文名',
        }));
      }
      if (typeof field.required !== 'boolean') {
        issues.push(issue({
          code: 'FIELD_REQUIRED_INVALID',
          level: 'error',
          path,
          objectKey: object.key,
          fieldKey: field.key,
          table: table.name,
          ...(table.documentId ? { documentId: table.documentId } : {}),
          sourceColumn: field.sourceColumn || expectedField.sourceColumn || expectedField.key,
          message: `对象 ${object.key} 的字段 ${field.key} 尚未完成必填二态归一化`,
          suggestion: '重新执行 Schema 对象同步；Core 会按显式约束或无约束来源时的推断主键回退规则生成二态值',
        }));
      }
    }
  }

  const errorCount = issues.filter((item) => item.level === 'error').length;
  const warningCount = issues.length - errorCount;
  const fingerprint = createHash('sha256').update(JSON.stringify({
    validatorVersion: SCHEMA_SEMANTICS_VALIDATOR_VERSION,
    objects: resolved.map(({ table, expectedObject, object }) => {
      return {
        table: table.name,
        documentId: table.documentId ?? '',
        key: object?.key ?? expectedObject.key,
        title: object?.title ?? '',
        description: object?.description ?? '',
        aliases: object?.aliases ?? [],
        fields: (object?.fields ?? []).map((field) => ({
          key: field.key,
          sourceColumn: field.sourceColumn ?? '',
          cnName: field.cnName ?? '',
          required: field.required === true,
          note: field.note ?? '',
          alias: field.alias ?? '',
        })),
      };
    }),
  })).digest('hex');

  return {
    available: true,
    pass: errorCount === 0,
    validatorVersion: SCHEMA_SEMANTICS_VALIDATOR_VERSION,
    fingerprint,
    objectCount: expected.length,
    fieldCount,
    errorCount,
    warningCount,
    issues,
  };
}
