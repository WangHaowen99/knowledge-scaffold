import type { DataDictField, DataDictTable, PackageSpec, SpecObject } from '../types.js';
import { tableSourcesFor } from './source-references.js';
import { isTermTable } from './term-table.js';

export interface SemanticBindingSuggestion {
  key: string;
  resource: string;
  valueField: string;
  semanticField: string;
  providerAlias?: string;
  aliasFields: string[];
  candidateFieldRefs: Array<{
    objectKey: string;
    fieldKey: string;
    sourceColumn: string;
    sourceSheet?: string;
    coverage: number;
    matchedValueCount?: number;
    sourceValueCount?: number;
  }>;
  confidence: number;
  reasons: string[];
}

export interface SemanticFieldRefSuggestion {
  objectKey: string;
  fieldKey: string;
  sourceColumn: string;
  sourceSheet?: string;
  bindingKey: string;
  coverage: number;
  matchedValueCount?: number;
  sourceValueCount?: number;
}

function normalized(value: string): string {
  return value.trim().normalize('NFC').toLowerCase();
}

function bindingSignature(input: { resource: string; valueField: string; semanticField: string }): string {
  return [
    input.resource,
    input.valueField,
    input.semanticField,
  ].map(normalized).join('\u0000');
}

function fieldSamples(field: DataDictField): string[] {
  return (field.sampleValues ?? [])
    .map((value) => String(value ?? '').trim())
    .filter(Boolean);
}

function valueSet(table: DataDictTable | undefined, field: DataDictField): Set<string> {
  const values: string[] = [];
  if (table?.rows?.length) {
    const index = table.fields.findIndex((item) => item.name === field.name);
    if (index >= 0) {
      for (const row of table.rows) {
        const value = String(row[index] ?? '').trim();
        if (value) values.push(value);
      }
    }
  }
  if (!values.length) values.push(...fieldSamples(field));
  return new Set(values.map(normalized));
}

function coverageStats(
  source: DataDictField,
  target: DataDictField,
  sourceTable?: DataDictTable,
  targetTable?: DataDictTable,
): { coverage: number; matchedValueCount: number; sourceValueCount: number } {
  const sourceValues = valueSet(sourceTable, source);
  if (!sourceValues.size) return { coverage: 0, matchedValueCount: 0, sourceValueCount: 0 };
  const targetValues = valueSet(targetTable, target);
  let matched = 0;
  for (const value of sourceValues) {
    if (targetValues.has(value)) matched++;
  }
  return {
    coverage: matched / sourceValues.size,
    matchedValueCount: matched,
    sourceValueCount: sourceValues.size,
  };
}

function keyFrom(resource: string, valueField: string): string {
  const base = `${resource}-${valueField}`
    .normalize('NFC')
    .replace(/[^\p{Letter}\p{Number}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase();
  return base || 'value-binding';
}

function objectSourceSheet(object: SpecObject): string | undefined {
  return tableSourcesFor(object)[0]?.table ?? object.sourceTable;
}

function sourceFieldByObject(
  object: SpecObject,
  tablesByName: Map<string, DataDictTable>,
  fieldKey: string,
): { table?: DataDictTable; field?: DataDictField; sourceColumn: string } {
  const field = object.fields.find((item) => item.key === fieldKey);
  const sourceColumn = field?.sourceColumn || field?.key || fieldKey;
  const tableName = objectSourceSheet(object);
  const table = tableName ? tablesByName.get(tableName) : undefined;
  return { table, field: table?.fields.find((item) => item.name === sourceColumn), sourceColumn };
}

function uniqueKey(base: string, used: Set<string>): string {
  let key = base;
  let suffix = 2;
  while (used.has(normalized(key))) {
    key = `${base}-${suffix++}`;
  }
  used.add(normalized(key));
  return key;
}

function termTableBindingSuggestion(
  spec: PackageSpec,
  table: DataDictTable,
  usedKeys: Set<string>,
  existingBindingSignatures: Set<string>,
): SemanticBindingSuggestion | null {
  const valueField = table.fields[0];
  const semanticField = table.fields[1];
  if (!valueField?.name.trim() || !semanticField?.name.trim()) return null;
  if (existingBindingSignatures.has(bindingSignature({
    resource: table.name,
    valueField: valueField.name,
    semanticField: semanticField.name,
  }))) return null;
  return {
    key: uniqueKey(keyFrom(table.name, valueField.name), usedKeys),
    resource: table.name,
    valueField: valueField.name,
    semanticField: semanticField.name,
    providerAlias: spec.defaultProviderAlias,
    aliasFields: table.fields.slice(2).map((field) => field.name.trim()).filter(Boolean),
    candidateFieldRefs: [],
    confidence: 0.99,
    reasons: [
      `已上传术语表「${table.name}」，按固定列顺序识别第 1 列「${valueField.name}」为 value，第 2 列「${semanticField.name}」为 semantic`,
      '第 3 列及后续列将作为 alias 字段；业务字段关联将在确认后单独推荐',
    ],
  };
}

export function suggestSemanticBindings(
  spec: PackageSpec,
  tables: DataDictTable[],
): SemanticBindingSuggestion[] {
  const usedKeys = new Set<string>([
    ...(spec.objects ?? []).flatMap((object) => [
      object.key,
      object.title,
      ...(object.aliases ?? []),
    ]),
    ...((spec.valueBindings ?? []).map((binding) => binding.key)),
  ].filter(Boolean).map((value) => normalized(String(value))));
  const existingBindingSignatures = new Set((spec.valueBindings ?? []).map(bindingSignature));

  const suggestions: SemanticBindingSuggestion[] = [];
  for (const table of tables.filter(isTermTable)) {
    const suggestion = termTableBindingSuggestion(spec, table, usedKeys, existingBindingSignatures);
    if (suggestion) suggestions.push(suggestion);
  }

  return suggestions.sort((a, b) => b.confidence - a.confidence || a.key.localeCompare(b.key));
}

export function suggestSemanticFieldRefs(
  spec: PackageSpec,
  tables: DataDictTable[],
): SemanticFieldRefSuggestion[] {
  const tablesByName = new Map(tables.map((table) => [table.name, table]));
  const seenBindings = new Set<string>();
  const bindings = (spec.valueBindings ?? []).filter((binding) => {
    const signature = bindingSignature(binding);
    if (seenBindings.has(signature)) return false;
    seenBindings.add(signature);
    return true;
  });
  const suggestions: SemanticFieldRefSuggestion[] = [];

  for (const binding of bindings) {
    const resource = tablesByName.get(binding.resource);
    const valueField = resource?.fields.find((field) => field.name === binding.valueField);
    if (!resource || !valueField) continue;
    for (const object of spec.objects ?? []) {
      for (const field of object.fields ?? []) {
        if (field.valueBindingRef === binding.key) continue;
        const { table: sourceTable, field: sourceField, sourceColumn } = sourceFieldByObject(object, tablesByName, field.key);
        if (!sourceField || !sourceTable) continue;
        if (sourceTable.name === resource.name && sourceColumn === valueField.name) continue;
        const stats = coverageStats(sourceField, valueField, sourceTable, resource);
        if (stats.coverage < 0.5) continue;
        suggestions.push({
          objectKey: object.key,
          fieldKey: field.key,
          sourceColumn,
          sourceSheet: sourceTable.name,
          bindingKey: binding.key,
          coverage: Number(stats.coverage.toFixed(3)),
          matchedValueCount: stats.matchedValueCount,
          sourceValueCount: stats.sourceValueCount,
        });
      }
    }
  }

  return suggestions.sort((a, b) =>
    b.coverage - a.coverage
    || a.bindingKey.localeCompare(b.bindingKey)
    || `${a.objectKey}.${a.fieldKey}`.localeCompare(`${b.objectKey}.${b.fieldKey}`));
}
