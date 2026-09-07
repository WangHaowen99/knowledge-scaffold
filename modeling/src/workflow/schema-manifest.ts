import { createHash } from 'node:crypto';
import type { DataDictField, DataDictTable, SpecField, SpecObject } from '../types.js';
import { isSchemaFallbackDescription, schemaFallbackDescription } from './schema-semantic-shared.js';

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
  /** Internal merge hint; never rendered into the package. */
  authoritativeRequiredFieldKeys: ReadonlySet<string>;
}

export interface SchemaBusinessMetadata {
  tableName: string;
  columnName?: string;
  businessName?: string;
  description?: string;
  aliases?: string[];
  required?: boolean;
  primaryKey?: boolean;
  sourceTable: string;
}

const normalize = (value: unknown) => String(value ?? '').trim().normalize('NFKC');
const normalized = (value: unknown) => normalize(value).toLocaleLowerCase('zh-CN').replace(/[\s_\-]/gu, '');

function fieldIndex(fields: DataDictField[], patterns: RegExp[]): number {
  return fields.findIndex((field) => patterns.some((pattern) => pattern.test(normalized(field.name))));
}

function exactFieldIndex(fields: DataDictField[], names: string[]): number {
  const accepted = new Set(names.map(normalized));
  return fields.findIndex((field) => accepted.has(normalized(field.name)));
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

function isNamedPrimaryKeyCandidate(field: DataDictField): boolean {
  return (
    /(?:^|_)(?:id|code|key|no|number)$/iu.test(field.name)
    || /(编号|编码|标识|主键|代码)/u.test(field.name)
  );
}

function isUniquePrimaryKeyCandidate(field: DataDictField): boolean {
  return field.distinctRatio === 1 && field.sampleValues.length > 0;
}

function inferPrimaryKey(fields: DataDictField[]): string[] {
  const likely = fields.filter(isNamedPrimaryKeyCandidate);
  const selected = likely.find(isUniquePrimaryKeyCandidate)
    ?? fields.find(isUniquePrimaryKeyCandidate)
    ?? likely[0]
    ?? fields[0];
  return selected ? [selected.name] : [];
}

function fieldType(field: DataDictField): string {
  return field.inferredType === 'unknown' ? 'string' : field.inferredType;
}

function toSpecFields(
  table: DataDictTable,
  metadata: ReadonlyMap<string, SchemaBusinessMetadata>,
  hasDictionaryMetadata: boolean,
): { fields: SpecField[]; primaryKey: string[]; authoritativeRequiredFieldKeys: ReadonlySet<string> } {
  const used = new Set<string>();
  const fieldKeys = new Map<string, string>();
  const authoritativeRequiredFieldKeys = new Set<string>();
  const keyedFields = table.fields.map((field, index) => {
    const key = fieldKey(field, index, used);
    fieldKeys.set(field.name, key);
    return { field, key, semantic: metadata.get(field.name) };
  });
  const declaredTablePrimaryNames = (table.primaryKey ?? [])
    .filter((name) => fieldKeys.has(name));
  const explicitPrimaryNames = [...new Set([
    ...declaredTablePrimaryNames,
    ...keyedFields
    .filter(({ field, semantic }) => semantic?.primaryKey === true || field.primaryKey === true)
    .map(({ field }) => field.name),
  ])];
  const explicitPrimaryNameSet = new Set(explicitPrimaryNames);
  const inferredPrimaryNames = inferPrimaryKey(table.fields);
  const primaryNames = explicitPrimaryNames.length ? explicitPrimaryNames : inferredPrimaryNames;
  const primaryNameSet = new Set(primaryNames);
  const hasConstraintSource = hasDictionaryMetadata
    || Boolean(table.constraintSource)
    || Array.isArray(table.primaryKey)
    || table.fields.some((field) => (
      typeof field.primaryKey === 'boolean'
      || typeof field.required === 'boolean'
      || typeof field.nullable === 'boolean'
    ));
  const inferredPrimaryRequired = !hasConstraintSource && explicitPrimaryNames.length === 0;
  const fields = keyedFields.map(({ field, key, semantic }) => {
    const explicitPrimaryDecision = Array.isArray(table.primaryKey)
      || typeof semantic?.primaryKey === 'boolean'
      || typeof field.primaryKey === 'boolean';
    const explicitPrimary = explicitPrimaryNameSet.has(field.name);
    const explicitRequired = typeof semantic?.required === 'boolean'
      ? semantic.required
      : typeof field.required === 'boolean'
        ? field.required
        : typeof field.nullable === 'boolean'
          ? !field.nullable
          : undefined;
    const reliableInferredPrimary = inferredPrimaryRequired
      && primaryNameSet.has(field.name)
      && (isNamedPrimaryKeyCandidate(field) || isUniquePrimaryKeyCandidate(field));
    if (explicitPrimaryDecision || typeof explicitRequired === 'boolean' || reliableInferredPrimary) {
      authoritativeRequiredFieldKeys.add(key);
    }
    return {
      key,
      cnName: semantic?.businessName || field.name,
      type: fieldType(field),
      sourceColumn: field.name,
      required: explicitPrimary
        || explicitRequired === true
        || reliableInferredPrimary,
      ...((semantic?.description || field.note) ? { note: semantic?.description || field.note } : {}),
      ...(semantic?.aliases?.[0] ? { alias: semantic.aliases[0] } : {}),
    } satisfies SpecField;
  });
  const primaryKey = primaryNames
    .map((name) => fieldKeys.get(name))
    .filter((key): key is string => Boolean(key));
  return {
    fields,
    primaryKey,
    authoritativeRequiredFieldKeys,
  };
}

function splitAliases(value: unknown): string[] | undefined {
  const aliases = normalize(value).split(/[,，;；|/]/u).map((item) => item.trim()).filter(Boolean);
  return aliases.length ? [...new Set(aliases)] : undefined;
}

function parseRequiredMetadata(value: unknown): boolean {
  const text = normalize(value).toLocaleLowerCase('zh-CN');
  if (['true', 'yes', 'y', '1', '是', '必填', 'required'].includes(text)) return true;
  return false;
}

function parsePrimaryKeyMetadata(value: unknown): boolean {
  const text = normalize(value).toLocaleLowerCase('zh-CN');
  if (/^[1-9]\d*$/u.test(text)) return true;
  return ['true', 'yes', 'y', '是', '主键', 'primary', 'primary key', 'pk', 'pri'].includes(text);
}

/** Detect the explicit data-dictionary contract without inspecting sample values. */
export function detectSchemaBusinessMetadata(
  physicalTables: readonly DataDictTable[],
  sourceTables: readonly DataDictTable[],
): Map<string, SchemaBusinessMetadata[]> {
  const result = new Map<string, SchemaBusinessMetadata[]>();
  const byDocumentAndName = new Map(physicalTables.map((table) => [
    `${table.documentId ?? '__unbound__'}:${table.name}`,
    table,
  ]));
  const byName = new Map<string, DataDictTable[]>();
  for (const table of physicalTables) {
    const matches = byName.get(table.name);
    if (matches) matches.push(table);
    else byName.set(table.name, [table]);
  }
  for (const source of sourceTables) {
    const sheetIndex = exactFieldIndex(source.fields, ['sheet_name', 'sheetname', 'table_name', 'tablename', '表名', '工作表']);
    const columnIndex = exactFieldIndex(source.fields, ['column_name', 'columnname', 'field_name', 'fieldname', '列名', '字段名']);
    const businessIndex = exactFieldIndex(source.fields, ['business_name', 'businessname', 'cn_name', 'cnname', '业务名称', '中文名']);
    const descriptionIndex = exactFieldIndex(source.fields, ['description', '业务说明', '说明', '描述']);
    const aliasesIndex = exactFieldIndex(source.fields, ['aliases', 'alias', '别名']);
    const requiredIndex = exactFieldIndex(source.fields, ['required', 'is_required', 'isrequired', '必填', '是否必填']);
    const primaryKeyIndex = exactFieldIndex(source.fields, [
      'primary_key', 'primarykey', 'is_primary_key', 'isprimarykey', 'is_pk', 'ispk',
      'primary_key_order', 'primarykeyorder', 'pk_order', 'pkorder', 'column_key', 'columnkey',
      'pk', '主键', '是否主键', '主键序号',
    ]);
    if (sheetIndex < 0 || columnIndex < 0
      || [businessIndex, descriptionIndex, aliasesIndex, requiredIndex, primaryKeyIndex]
        .every((index) => index < 0)) {
      continue;
    }
    for (const row of source.rows ?? []) {
      const sheetName = normalize(row[sheetIndex]);
      const exact = byDocumentAndName.get(`${source.documentId ?? '__unbound__'}:${sheetName}`);
      const global = byName.get(sheetName) ?? [];
      const target = exact ?? (global.length === 1 ? global[0] : undefined);
      if (!target) continue;
      const columnName = normalize(row[columnIndex]);
      if (columnName && !target.fields.some((field) => field.name === columnName)) continue;
      const aliases = aliasesIndex >= 0 ? splitAliases(row[aliasesIndex]) : undefined;
      const required = requiredIndex >= 0 ? parseRequiredMetadata(row[requiredIndex]) : undefined;
      const primaryKey = primaryKeyIndex >= 0 ? parsePrimaryKeyMetadata(row[primaryKeyIndex]) : undefined;
      const item: SchemaBusinessMetadata = {
        tableName: target.name,
        ...(columnName ? { columnName } : {}),
        ...(businessIndex >= 0 && normalize(row[businessIndex])
          ? { businessName: normalize(row[businessIndex]) }
          : {}),
        ...(descriptionIndex >= 0 && normalize(row[descriptionIndex])
          ? { description: normalize(row[descriptionIndex]) }
          : {}),
        ...(aliases ? { aliases } : {}),
        ...(typeof required === 'boolean' ? { required } : {}),
        ...(typeof primaryKey === 'boolean' ? { primaryKey } : {}),
        sourceTable: source.name,
      };
      const targetKey = `${target.documentId ?? '__unbound__'}:${target.name}`;
      result.set(targetKey, [...(result.get(targetKey) ?? []), item]);
    }
  }
  return result;
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

export function buildPhysicalSchemaObjects(
  tables: DataDictTable[],
  metadataSources: DataDictTable[] = tables,
): PhysicalSchemaObject[] {
  const manifests = detectSchemaManifests(metadataSources);
  const businessMetadata = detectSchemaBusinessMetadata(tables, metadataSources);
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
    const entry = entryBySheet.get(manifestKey);
    const metadata = businessMetadata.get(manifestKey) ?? [];
    const objectMetadata = metadata.filter((item) => !item.columnName);
    const fieldMetadata = new Map(metadata
      .filter((item): item is SchemaBusinessMetadata & { columnName: string } => Boolean(item.columnName))
      .map((item) => [item.columnName, item]));
    const { fields, primaryKey, authoritativeRequiredFieldKeys } = toSpecFields(
      table,
      fieldMetadata,
      metadata.length > 0,
    );
    const title = objectMetadata.find((item) => item.businessName)?.businessName || entry?.title || table.name;
    const description = objectMetadata.find((item) => item.description)?.description
      || entry?.description
      || schemaFallbackDescription(table.name);
    const aliases = objectMetadata.flatMap((item) => item.aliases ?? []).length
      ? [...new Set(objectMetadata.flatMap((item) => item.aliases ?? []))]
      : entry?.title && entry.title !== table.name ? [entry.title] : undefined;
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
      authoritativeRequiredFieldKeys,
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

function matchingPreviousObject(
  generated: SpecObject,
  table: DataDictTable,
  previousByKey: ReadonlyMap<string, SpecObject>,
  previousBySource: ReadonlyMap<string, SpecObject[]>,
): SpecObject | undefined {
  const exact = previousByKey.get(generated.key);
  if (exact) return exact;
  const sameSource = previousBySource.get(table.name) ?? [];
  const sameRevision = sameSource.find((object) => object.sourceRefs?.some((source) => (
    source.kind === 'table' && source.table === table.name && source.documentId === table.documentId
  )));
  return sameRevision ?? (sameSource.length === 1 ? sameSource[0] : undefined);
}

function mergeFieldSemantics(
  generated: SpecField,
  previous: SpecField | undefined,
  requiredIsExplicit: boolean,
): SpecField {
  if (!previous) return generated;
  const previousName = normalize(previous.cnName);
  const previousTechnical = [previous.key, previous.sourceColumn]
    .filter(Boolean)
    .some((value) => normalized(value) === normalized(previousName));
  const generatedName = normalize(generated.cnName);
  const generatedTechnical = [generated.key, generated.sourceColumn]
    .filter(Boolean)
    .some((value) => normalized(value) === normalized(generatedName));
  return {
    ...generated,
    ...((!generatedName || generatedTechnical) && previousName && !previousTechnical
      ? { cnName: previous.cnName }
      : {}),
    ...(!normalize(generated.note) && normalize(previous.note) ? { note: previous.note } : {}),
    ...(!normalize(generated.alias) && normalize(previous.alias) ? { alias: previous.alias } : {}),
    ...(!requiredIsExplicit && typeof previous.required === 'boolean'
      ? { required: previous.required }
      : {}),
    ...(normalize(previous.valueBindingRef) ? { valueBindingRef: previous.valueBindingRef } : {}),
  };
}

/** Preserve confirmed business semantics while rebuilding physical Schema identity and mappings. */
export function preservePhysicalSchemaSemantics(
  physical: readonly PhysicalSchemaObject[],
  previousObjects: readonly SpecObject[],
): PhysicalSchemaObject[] {
  const previousByKey = new Map(previousObjects.map((object) => [object.key, object]));
  const previousBySource = new Map<string, SpecObject[]>();
  for (const object of previousObjects) {
    if (!object.sourceTable) continue;
    const matches = previousBySource.get(object.sourceTable);
    if (matches) matches.push(object);
    else previousBySource.set(object.sourceTable, [object]);
  }
  return physical.map((item) => {
    const { table, object, authoritativeRequiredFieldKeys } = item;
    const previous = matchingPreviousObject(object, table, previousByKey, previousBySource);
    if (!previous) return item;
    const previousFields = new Map(previous.fields.map((field) => [
      normalize(field.sourceColumn || field.key),
      field,
    ]));
    const previousTitle = normalize(previous.title);
    const previousTitleIsFallback = [previous.key, previous.sourceTable]
      .filter(Boolean)
      .some((value) => normalized(value) === normalized(previousTitle));
    const generatedTitle = normalize(object.title);
    const generatedTitleIsFallback = [object.key, table.name]
      .some((value) => normalized(value) === normalized(generatedTitle));
    return {
      table,
      authoritativeRequiredFieldKeys,
      object: {
        ...object,
        ...(generatedTitleIsFallback && previousTitle && !previousTitleIsFallback
          ? { title: previous.title }
          : {}),
        ...(isSchemaFallbackDescription(object.description, table.name)
          && !isSchemaFallbackDescription(previous.description, table.name)
          ? { description: previous.description }
          : {}),
        ...(!object.aliases?.length && previous.aliases?.length ? { aliases: [...previous.aliases] } : {}),
        ...(normalize(previous.queryBoundary) ? { queryBoundary: previous.queryBoundary } : {}),
        ...(previous.relations?.length ? { relations: structuredClone(previous.relations) } : {}),
        fields: object.fields.map((field) => mergeFieldSemantics(
          field,
          previousFields.get(normalize(field.sourceColumn || field.key)),
          authoritativeRequiredFieldKeys.has(field.key),
        )),
      },
    };
  });
}
