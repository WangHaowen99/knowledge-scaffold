import type { DataDictField, DataDictTable, PackageSpec, SpecObject } from '../types.js';
import { tableSourcesFor } from './source-references.js';
import { isTermTable } from './term-table.js';

const CODE_NAME = /(?:^|[_\-\s])(cd|code|id|no)(?:$|[_\-\s])|编号|代码|代号/iu;
const SEMANTIC_NAME = /名称|中文|描述|名|name|desc|description/iu;
const EXCLUDED_SEMANTIC = /状态|日期|时间|数量|数值|金额|类型|type|status|date|time|amount|qty|count/iu;
const TERMINOLOGY_RESOURCE_NAME = /名称|中文|英文|中英文|对照|映射|别名|术语|字典|码表|lookup|dict|term|alias|name|cn[-_]|[-_]cn|en[-_]|[-_]en/iu;
const MIN_AUTO_BINDING_CONFIDENCE = 0.85;
const CODE_SUFFIX = /(?:^|[_\-\s])(cd|code|id|no)(?:$|[_\-\s])|编号|代码|代号/giu;
const SEMANTIC_SUFFIX = /名称|中文|描述|名|name|desc|description/giu;

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

function isCodeField(field: DataDictField): boolean {
  const name = field.name.trim();
  if (CODE_NAME.test(name)) return true;
  if (SEMANTIC_NAME.test(name)) return false;
  const samples = fieldSamples(field);
  return samples.length > 0 && samples.every((value) => value.length <= 16);
}

function isSemanticField(field: DataDictField): boolean {
  const name = field.name.trim();
  if (!SEMANTIC_NAME.test(name)) return false;
  if (EXCLUDED_SEMANTIC.test(name)) return false;
  return fieldSamples(field).some((value) => /[\p{Script=Han}A-Za-z]/u.test(value));
}

function isTerminologyResourceTable(table: DataDictTable): boolean {
  return TERMINOLOGY_RESOURCE_NAME.test(table.name);
}

function comparableFieldStem(name: string, role: 'code' | 'semantic'): string {
  const rolePattern = role === 'code' ? CODE_SUFFIX : SEMANTIC_SUFFIX;
  return normalized(name)
    .replace(rolePattern, ' ')
    .replace(/[^\p{Letter}\p{Number}]+/gu, ' ')
    .trim()
    .replace(/\s+/g, ' ');
}

function pairScore(valueField: DataDictField, semanticField: DataDictField): number {
  if (semanticField.name === valueField.name) return -1;
  const codeStem = comparableFieldStem(valueField.name, 'code');
  const semanticStem = comparableFieldStem(semanticField.name, 'semantic');
  if (codeStem && semanticStem && codeStem === semanticStem) return 100;
  return 10;
}

function rankedSemanticFields(valueField: DataDictField, semanticFields: DataDictField[]): DataDictField[] {
  const ranked = semanticFields
    .map((field, index) => ({ field, index, score: pairScore(valueField, field) }))
    .filter((item) => item.score >= 0)
    .sort((left, right) => right.score - left.score || left.index - right.index);
  if (!ranked.length) return [];
  const best = ranked[0].score;
  return ranked.filter((item) => item.score === best).map((item) => item.field);
}

function aliasFieldsFor(_valueField: DataDictField, _semanticField: DataDictField, _semanticFields: DataDictField[]): string[] {
  return [];
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

function bestBusinessFieldCoverage(
  spec: PackageSpec,
  tables: DataDictTable[],
  resource: DataDictTable,
  valueField: DataDictField,
): number {
  const tablesByName = new Map(tables.map((table) => [table.name, table]));
  let best = 0;
  for (const object of spec.objects ?? []) {
    for (const field of object.fields ?? []) {
      const source = sourceFieldByObject(object, tablesByName, field.key);
      if (!source.table || !source.field || source.table.name === resource.name) continue;
      best = Math.max(
        best,
        coverageStats(source.field, valueField, source.table, resource).coverage,
      );
    }
  }
  return best;
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
  const termTables = tables.filter(isTermTable);
  if (termTables.length) {
    for (const table of termTables) {
      const suggestion = termTableBindingSuggestion(spec, table, usedKeys, existingBindingSignatures);
      if (suggestion) suggestions.push(suggestion);
    }
    return suggestions.sort((a, b) => b.confidence - a.confidence || a.key.localeCompare(b.key));
  }

  for (const table of tables) {
    if (!isTerminologyResourceTable(table)) continue;
    const codeFields = table.fields.filter(isCodeField);
    const semanticFields = table.fields.filter(isSemanticField);
    if (!codeFields.length || !semanticFields.length) continue;

    for (const valueField of codeFields) {
      const pairedSemanticFields = rankedSemanticFields(valueField, semanticFields);
      if (!pairedSemanticFields.length) continue;

      for (const semanticField of pairedSemanticFields) {
        if (existingBindingSignatures.has(bindingSignature({
          resource: table.name,
          valueField: valueField.name,
          semanticField: semanticField.name,
        }))) continue;
        const aliasFields = aliasFieldsFor(valueField, semanticField, semanticFields);
        const key = uniqueKey(keyFrom(table.name, valueField.name), usedKeys);
        const semanticMatched = pairScore(valueField, semanticField) >= 100;
        const businessCoverage = bestBusinessFieldCoverage(spec, tables, table, valueField);
        const valuesMatched = businessCoverage >= 0.5;
        const confidence = Number(Math.min(
          0.98,
          0.7
            + (semanticMatched ? 0.15 : 0)
            + (valuesMatched ? 0.15 : 0)
            + (aliasFields.length ? 0.05 : 0),
        ).toFixed(3));
        if (confidence < MIN_AUTO_BINDING_CONFIDENCE) continue;
        suggestions.push({
          key,
          resource: table.name,
          valueField: valueField.name,
          semanticField: semanticField.name,
          providerAlias: spec.defaultProviderAlias,
          aliasFields,
          candidateFieldRefs: [],
          confidence,
          reasons: [
            `资源「${table.name}」包含可配对的代号字段「${valueField.name}」和名称字段「${semanticField.name}」`,
            ...(valuesMatched ? [`该代号字段与现有业务字段的样例值覆盖率为 ${Math.round(businessCoverage * 100)}%`] : []),
            '先确认术语绑定本身；业务字段关联将在确认后单独推荐',
          ],
        });
      }
    }
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
