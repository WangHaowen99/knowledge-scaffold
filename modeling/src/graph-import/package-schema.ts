import { assertGraphIdentifier } from '../evaluation/data-source.js';
import type { PackageSpec } from '../types.js';
import { mappingFieldName, mappingObjectName } from '../workflow/mapping-contract.js';
import {
  markdownTableCells,
  parseObjectIndex,
  type ObjectIndexEntry,
} from '../workflow/object-index.js';
import type {
  GraphImportObjectSchema,
  GraphImportPackageSchema,
  GraphImportRelationSchema,
  GraphImportValueBindingSchema,
} from './types.js';
import { assertGraphLabel } from './graph-label.js';

type PackageFiles = Record<string, string>;

function relationRows(content: string): {
  present: boolean;
  rows: Array<{ relation: string; target: string; note: string }>;
} {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const start = lines.findIndex((line) => /^#{1,6}\s+关系\s*$/.test(line.trim()));
  if (start < 0) return { present: false, rows: [] };
  const section: string[] = [];
  for (let index = start + 1; index < lines.length; index++) {
    if (/^#{1,6}\s+/.test(lines[index].trim())) break;
    section.push(lines[index]);
  }
  const rows = section
    .filter((line) => /^\s*\|/.test(line))
    .map(markdownTableCells)
    .filter((cells) => cells.length >= 2 && cells[0] !== '关系' && !/^[-:]+$/.test(cells[0]))
    .map((cells) => ({ relation: cells[0], target: cells[1], note: cells[2] ?? '' }))
    .filter((row) => row.relation && row.target);
  return { present: true, rows };
}

function asRecord(value: unknown, label: string): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label}格式非法`);
  return value as Record<string, any>;
}

function objectSchema(
  spec: PackageSpec,
  mappings: Record<string, any>,
  indexEntries: Map<string, ObjectIndexEntry>,
): GraphImportObjectSchema[] {
  return spec.objects.filter((object) => mappings[mappingObjectName(object)] != null).map((object) => {
    const objectName = mappingObjectName(object);
    const mapping = asRecord(mappings[objectName], `对象 ${object.key} 的 mapping`);
    if (mapping.resource !== object.key) {
      throw new Error(`对象 ${object.key} 的 mapping resource 应为 ${object.key}（当前：${String(mapping.resource ?? '缺失')}）`);
    }
    const indexEntry = indexEntries.get(object.key);
    if (!indexEntry) throw new Error(`objects/index.md 缺少 object key ${object.key}`);
    assertGraphLabel(indexEntry.key, `对象 ${object.key} 的 Neo4j Label`);
    const fields = asRecord(mapping.fields || {}, `对象 ${object.key} 的 fields`);
    const expectedFieldNames = new Set<string>();
    const propertyMappings = object.fields.map((field) => {
      const fieldName = mappingFieldName(field);
      expectedFieldNames.add(fieldName);
      const raw = fields[fieldName];
      if (!raw || typeof raw !== 'object') {
        throw new Error(`对象 ${object.key} 的 mapping 缺少字段 key ${fieldName}`);
      }
      const semanticSource = String((raw as any).source ?? '').trim();
      if (semanticSource !== field.key) {
        throw new Error(`对象 ${object.key} 字段 ${fieldName} 的 mapping source 应为 ${field.key}（当前：${semanticSource || '缺失'}）`);
      }
      const property = String(field.key).trim();
      const sourceColumn = String(field.sourceColumn || field.key).trim();
      assertGraphIdentifier(property, `对象 ${object.key} 的 Property`);
      return { property, sourceColumn };
    });
    for (const fieldName of Object.keys(fields)) {
      if (!expectedFieldNames.has(fieldName)) {
        throw new Error(`对象 ${object.key} 的 mapping 包含对象规格未声明的字段 key ${fieldName}`);
      }
    }
    return {
      key: object.key,
      title: object.title,
      label: indexEntry.key,
      defaultSheet: indexEntry.sourceSheet,
      propertyMappings,
    };
  });
}

function relations(spec: PackageSpec, files: PackageFiles): GraphImportRelationSchema[] {
  const objectKeys = new Set(spec.objects.map((object) => object.key));
  const result: GraphImportRelationSchema[] = [];
  for (const object of spec.objects) {
    const content = files[`objects/${object.key}.md`];
    if (typeof content !== 'string') throw new Error(`最终知识包缺少 objects/${object.key}.md`);
    const parsed = relationRows(content);
    const declared = parsed.present ? parsed.rows : (object.relations ?? []);
    for (const [ordinal, relation] of declared.entries()) {
      if (!objectKeys.has(relation.target)) {
        throw new Error(`对象 ${object.key} 的关系目标不存在：${relation.target}`);
      }
      assertGraphIdentifier(relation.relation, `对象 ${object.key} 的关系类型`);
      result.push({
        id: `${object.key}|${relation.relation}|${relation.target}|${ordinal}`,
        sourceObject: object.key,
        relationName: relation.relation,
        targetObject: relation.target,
        note: relation.note,
      });
    }
  }
  return result;
}

function valueBindings(rawMapping: any): GraphImportValueBindingSchema[] {
  const bindings = rawMapping?.value_bindings;
  if (!bindings || typeof bindings !== 'object' || Array.isArray(bindings)) return [];
  return Object.entries<any>(bindings).map(([key, binding]) => {
    const label = String(binding?.resource ?? '').trim();
    const valueField = String(binding?.value_field ?? '').trim();
    const semanticField = String(binding?.semantic_field ?? '').trim();
    const aliasFields = Array.isArray(binding?.alias_fields)
      ? binding.alias_fields.map((item: unknown) => String(item ?? '').trim()).filter(Boolean)
      : [];
    if (!key || !label || !valueField || !semanticField) {
      throw new Error(`value_bindings.${key || '未命名'} 缺少 resource、value_field 或 semantic_field`);
    }
    assertGraphLabel(label, `术语绑定 ${key} 的 Neo4j Label`);
    const propertyMappings = [valueField, semanticField, ...aliasFields].map((field) => {
      assertGraphIdentifier(field, `术语绑定 ${key} 的 Property`);
      return { property: field, sourceColumn: field };
    });
    return {
      key,
      label,
      defaultSheet: label,
      valueField,
      semanticField,
      aliasFields,
      propertyMappings,
    };
  });
}

export function extractGraphImportSchema(spec: PackageSpec, files: PackageFiles): GraphImportPackageSchema {
  const rawObjectIndex = files['objects/index.md'];
  if (!rawObjectIndex) throw new Error('最终知识包缺少 objects/index.md');
  const rawMappings = files['mappings/object-data-mappings.json'];
  if (!rawMappings) throw new Error('最终知识包缺少 mappings/object-data-mappings.json');
  let parsed: any;
  try { parsed = JSON.parse(rawMappings); } catch { throw new Error('object-data-mappings.json 不是合法 JSON'); }
  const mappings = asRecord(parsed.objects, 'object-data-mappings.json objects');
  const objects = objectSchema(spec, mappings, parseObjectIndex(rawObjectIndex));
  const mappedObjectKeys = new Set(objects.map((object) => object.key));
  return {
    packageVersion: spec.version,
    objects,
    valueBindings: valueBindings(parsed),
    relations: relations(spec, files).filter((relation) => (
      mappedObjectKeys.has(relation.sourceObject) && mappedObjectKeys.has(relation.targetObject)
    )),
  };
}
