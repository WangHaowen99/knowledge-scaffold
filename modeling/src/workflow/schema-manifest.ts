import { createHash } from 'node:crypto';
import type { DataDictField, DataDictTable, SpecField, SpecObject } from '../types.js';

export interface SchemaManifestEntry {
  sheetName: string;
  sheetType?: string;
  identifier?: string;
  title?: string;
  description?: string;
}

export interface SchemaManifest {
  table: DataDictTable;
  entries: SchemaManifestEntry[];
  confidence: number;
}

export interface PhysicalSchemaObject {
  table: DataDictTable;
  object: SpecObject;
}

const normalize = (value: unknown) => String(value ?? '').trim().normalize('NFKC');
const normalized = (value: unknown) => normalize(value).toLocaleLowerCase('zh-CN').replace(/[\s_\-]/gu, '');

function fieldIndex(fields: DataDictField[], patterns: RegExp[]): number {
  return fields.findIndex((field) => patterns.some((pattern) => pattern.test(normalized(field.name))));
}

function safeKey(value: string, fallback: string): string {
  const key = normalize(value)
    .replace(/[\\/#?&|\u0000-\u001f\u007f]/gu, '_')
    .replace(/\s+/gu, '_')
    .replace(/^\.+|\.+$/gu, '')
    .slice(0, 64);
  return key || fallback;
}

function stableObjectKey(table: DataDictTable, index: number, identifier?: string): string {
  return safeKey(identifier || table.name, `sheet_${index + 1}`)
    .replace(/[^A-Za-z0-9_\u4e00-\u9fff]/gu, '_')
    .replace(/^([^A-Za-z_\u4e00-\u9fff])/u, '_$1')
    .slice(0, 80);
}

function fieldKey(field: DataDictField, index: number, used: Set<string>): string {
  const base = safeKey(field.name, `field_${index + 1}`)
    .replace(/[^A-Za-z0-9_\u4e00-\u9fff]/gu, '_')
    .replace(/^([^A-Za-z_\u4e00-\u9fff])/u, '_$1');
  let key = base;
  let suffix = 2;
  while (used.has(key)) key = `${base}_${suffix++}`;
  used.add(key);
  return key;
}

function inferPrimaryKey(fields: DataDictField[]): string[] {
  const likely = fields.filter((field) => (
    /(?:^|_)(?:id|code|key|no|number)$/iu.test(field.name)
    || /(编号|编码|标识|主键|代码)/u.test(field.name)
  ));
  const unique = (field: DataDictField) => field.distinctRatio === 1 && field.sampleValues.length > 0;
  const selected = likely.find(unique) ?? fields.find(unique) ?? likely[0] ?? fields[0];
  return selected ? [selected.name] : [];
}

function fieldType(field: DataDictField): string {
  return field.inferredType === 'unknown' ? 'string' : field.inferredType;
}

function toSpecFields(table: DataDictTable): { fields: SpecField[]; primaryKey: string[] } {
  const used = new Set<string>();
  const fieldKeys = new Map<string, string>();
  const fields = table.fields.map((field, index) => {
    const key = fieldKey(field, index, used);
    fieldKeys.set(field.name, key);
    return {
      key,
      cnName: field.name,
      type: fieldType(field),
      sourceColumn: field.name,
      ...(field.note ? { note: field.note } : {}),
    } satisfies SpecField;
  });
  const primaryKey = inferPrimaryKey(table.fields)
    .map((name) => fieldKeys.get(name))
    .filter((key): key is string => Boolean(key));
  const primaryKeys = new Set(primaryKey);
  return {
    fields: fields.map((field) => primaryKeys.has(field.key) ? { ...field, required: true } : field),
    primaryKey,
  };
}

function manifestCandidate(table: DataDictTable, workbookNames: Set<string>): SchemaManifest | undefined {
  const sheetIndex = fieldIndex(table.fields, [
    /sheetname/u, /tablename/u, /objectname/u, /sheet.*name/u, /表名/u, /工作表/u,
  ]);
  const typeIndex = fieldIndex(table.fields, [/(?:sheet|table|object|device)?type$/u, /类型/u]);
  const idIndex = fieldIndex(table.fields, [
    /(?:sheet|table|object|device)?id$/u, /identifier/u, /标识/u, /编号/u, /编码/u,
  ]);
  const titleIndex = fieldIndex(table.fields, [/(?:device|table|object)?name$/u, /名称/u]);
  const descriptionIndex = fieldIndex(table.fields, [/description/u, /desc/u, /说明/u, /描述/u]);
  if (sheetIndex < 0 || typeIndex < 0 || idIndex < 0 || descriptionIndex < 0) return undefined;
  const values = (table.rows ?? [])
    .map((row) => normalize(row[sheetIndex]))
    .filter(Boolean);
  const references = values.filter((value) => workbookNames.has(value));
  if (references.length < 3 || references.length / Math.max(values.length, 1) < 0.5) return undefined;
  const entries = (table.rows ?? []).flatMap((row) => {
    const sheetName = normalize(row[sheetIndex]);
    if (!sheetName || !workbookNames.has(sheetName)) return [];
    return [{
      sheetName,
      sheetType: normalize(row[typeIndex]) || undefined,
      identifier: idIndex >= 0 ? normalize(row[idIndex]) || undefined : undefined,
      title: titleIndex >= 0 ? normalize(row[titleIndex]) || undefined : undefined,
      description: descriptionIndex >= 0 ? normalize(row[descriptionIndex]) || undefined : undefined,
    } satisfies SchemaManifestEntry];
  });
  return { table, entries, confidence: references.length / Math.max(values.length, 1) };
}

export function detectSchemaManifests(tables: DataDictTable[]): SchemaManifest[] {
  const byDocument = new Map<string, DataDictTable[]>();
  for (const table of tables) {
    const key = table.documentId ?? '__unbound__';
    byDocument.set(key, [...(byDocument.get(key) ?? []), table]);
  }
  const manifests: SchemaManifest[] = [];
  for (const workbookTables of byDocument.values()) {
    const workbookNames = new Set(workbookTables.map((table) => table.name));
    for (const table of workbookTables) {
      const candidate = manifestCandidate(table, workbookNames);
      if (candidate) manifests.push(candidate);
    }
  }
  return manifests;
}

export function buildPhysicalSchemaObjects(tables: DataDictTable[]): PhysicalSchemaObject[] {
  const manifests = detectSchemaManifests(tables);
  const manifestNames = new Set(manifests.map((manifest) => manifest.table.name));
  const entryBySheet = new Map<string, SchemaManifestEntry>();
  for (const manifest of manifests) {
    for (const entry of manifest.entries) {
      const key = `${manifest.table.documentId ?? '__unbound__'}:${entry.sheetName}`;
      entryBySheet.set(key, entry);
    }
  }
  const usedKeys = new Set<string>();
  return tables.flatMap((table, index) => {
    const manifestKey = `${table.documentId ?? '__unbound__'}:${table.name}`;
    if (manifestNames.has(table.name) && manifests.some((item) => item.table === table)) return [];
    const entry = entryBySheet.get(manifestKey);
    const { fields, primaryKey } = toSpecFields(table);
    const title = entry?.title || table.name;
    const description = entry?.description || `来自 Schema Sheet「${table.name}」的物理数据对象`;
    const aliases = entry?.title && entry.title !== table.name ? [entry.title] : undefined;
    const baseKey = stableObjectKey(table, index, entry?.identifier);
    let objectKey = baseKey;
    if (usedKeys.has(objectKey)) {
      const digest = createHash('sha1')
        .update(`${table.documentId ?? ''}:${table.name}`)
        .digest('hex')
        .slice(0, 8);
      objectKey = `${baseKey.slice(0, 71)}_${digest}`;
    }
    let suffix = 2;
    while (usedKeys.has(objectKey)) objectKey = `${baseKey.slice(0, 74)}_${suffix++}`;
    usedKeys.add(objectKey);
    return [{
      table,
      object: {
        key: objectKey,
        title,
        description,
        ...(aliases ? { aliases } : {}),
        fields,
        primaryKey,
        sourceTable: table.name,
        sourceRefs: [{ kind: 'table' as const, ...(table.documentId ? { documentId: table.documentId } : {}), table: table.name }],
      },
    }];
  });
}
