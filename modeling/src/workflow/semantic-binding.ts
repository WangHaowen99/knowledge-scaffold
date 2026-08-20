import type { DataDictField, DataDictTable, PackageSpec, SpecObject } from '../types.js';
import { tableSourcesFor } from './source-references.js';

const CODE_NAME = /(?:^|[_\-\s])(cd|code|id|no)(?:$|[_\-\s])|编号|代码|代号/iu;
const SEMANTIC_NAME = /名称|中文|描述|名|name|desc|description/iu;
const EXCLUDED_SEMANTIC = /状态|日期|时间|数量|数值|金额|类型|type|status|date|time|amount|qty|count/iu;
const TERMINOLOGY_RESOURCE_NAME = /名称|中文|英文|中英文|对照|映射|别名|术语|字典|码表|lookup|dict|term|alias|name|cn[-_]|[-_]cn|en[-_]|[-_]en/iu;

const FIELD_SEMANTIC_GROUPS: Array<{
  id: string;
  code: RegExp;
  semantic: RegExp;
}> = [
  { id: 'airport', code: /(^|[_\-\s])(arp|airport)([_\-\s]|$)|机场|机场代码|机场代号/iu, semantic: /机场|airport/iu },
  { id: 'country', code: /(^|[_\-\s])(country|nation|region)([_\-\s]|$)|国家|地区/iu, semantic: /国家|国别|地区|country|nation|region/iu },
  { id: 'aircraft', code: /(^|[_\-\s])(ac|aircraft|tail)([_\-\s]|$)|飞机|机尾|尾号/iu, semantic: /飞机|机型|机尾|尾号|aircraft|tail/iu },
  { id: 'flight', code: /(^|[_\-\s])(flt|flight)([_\-\s]|$)|航班/iu, semantic: /航班|flight/iu },
  { id: 'staff', code: /(^|[_\-\s])(staff|crew|employee|emp)([_\-\s]|$)|员工|人员|机组|乘务|staff|crew/iu, semantic: /员工|人员|机组|乘务|staff|crew/iu },
];

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

function coverage(
  source: DataDictField,
  target: DataDictField,
  sourceTable?: DataDictTable,
  targetTable?: DataDictTable,
): number {
  const sourceValues = valueSet(sourceTable, source);
  if (!sourceValues.size) return 0;
  const targetValues = valueSet(targetTable, target);
  let matched = 0;
  for (const value of sourceValues) {
    if (targetValues.has(value)) matched++;
  }
  return matched / sourceValues.size;
}

function isCodeField(field: DataDictField): boolean {
  const name = field.name.trim();
  if (CODE_NAME.test(name)) return true;
  if (SEMANTIC_NAME.test(name) || semanticGroupsForName(field).length) return false;
  const samples = fieldSamples(field);
  return samples.length > 0 && samples.every((value) => value.length <= 16);
}

function isSemanticField(field: DataDictField): boolean {
  const name = field.name.trim();
  if (!SEMANTIC_NAME.test(name) && !semanticGroupsForName(field).length) return false;
  if (EXCLUDED_SEMANTIC.test(name)) return false;
  return fieldSamples(field).some((value) => /[\p{Script=Han}A-Za-z]/u.test(value));
}

function isTerminologyResourceTable(table: DataDictTable): boolean {
  if (TERMINOLOGY_RESOURCE_NAME.test(table.name)) return true;
  const codeFields = table.fields.filter(isCodeField);
  const semanticFields = table.fields.filter(isSemanticField);
  if (!codeFields.length || !semanticFields.length) return false;
  const semanticColumnNames = semanticFields.map((field) => field.name).join(' ');
  return TERMINOLOGY_RESOURCE_NAME.test(semanticColumnNames);
}

function fieldText(field: DataDictField): string {
  return [field.name, field.note ?? ''].filter(Boolean).join(' ');
}

function semanticGroupsForCode(field: DataDictField): string[] {
  const text = fieldText(field);
  return FIELD_SEMANTIC_GROUPS
    .filter((group) => group.code.test(text))
    .map((group) => group.id);
}

function semanticGroupsForName(field: DataDictField): string[] {
  const text = fieldText(field);
  return FIELD_SEMANTIC_GROUPS
    .filter((group) => group.semantic.test(text))
    .map((group) => group.id);
}

function pairScore(valueField: DataDictField, semanticField: DataDictField): number {
  if (semanticField.name === valueField.name) return -1;
  const codeGroups = semanticGroupsForCode(valueField);
  const semanticGroups = semanticGroupsForName(semanticField);
  if (codeGroups.length && semanticGroups.length) {
    if (codeGroups.some((group) => semanticGroups.includes(group))) return 100;
    return -1;
  }
  if (codeGroups.length && !semanticGroups.length) return -1;
  if (!codeGroups.length && semanticGroups.length) return 20;
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

function uniqueKey(base: string, used: Set<string>): string {
  let key = base;
  let suffix = 2;
  while (used.has(normalized(key))) {
    key = `${base}-${suffix++}`;
  }
  used.add(normalized(key));
  return key;
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
        suggestions.push({
          key,
          resource: table.name,
          valueField: valueField.name,
          semanticField: semanticField.name,
          providerAlias: spec.defaultProviderAlias,
          aliasFields,
          candidateFieldRefs: [],
          confidence: Number(Math.min(0.98, 0.7 + (semanticMatched ? 0.15 : 0) + (aliasFields.length ? 0.05 : 0)).toFixed(3)),
          reasons: [
            `资源「${table.name}」包含可配对的代号字段「${valueField.name}」和名称字段「${semanticField.name}」`,
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
        const score = coverage(sourceField, valueField, sourceTable, resource);
        if (score < 0.5) continue;
        suggestions.push({
          objectKey: object.key,
          fieldKey: field.key,
          sourceColumn,
          sourceSheet: sourceTable.name,
          bindingKey: binding.key,
          coverage: Number(score.toFixed(3)),
        });
      }
    }
  }

  return suggestions.sort((a, b) =>
    b.coverage - a.coverage
    || a.bindingKey.localeCompare(b.bindingKey)
    || `${a.objectKey}.${a.fieldKey}`.localeCompare(`${b.objectKey}.${b.fieldKey}`));
}
