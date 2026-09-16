import type { DataDictTable } from '../types.js';
import type { PhysicalSchemaObject } from './schema-manifest.js';

export const RELATIONSHIP_TEMPLATE_SHEET_NAME = '主外键映射';
export const RELATIONSHIP_TEMPLATE_HEADERS = [
  'table_name',
  'column_name',
  'constraint_type',
  'referenced_table',
  'referenced_column',
] as const;

export interface RelationshipTemplateStatus {
  matched: boolean;
  status: 'matched' | 'not_template' | 'invalid';
  message: string;
  primaryKeyCount: number;
  foreignKeyCount: number;
}

interface ForeignKeyRow {
  rowNumber: number;
  tableName: string;
  columnName: string;
  referencedTable: string;
  referencedColumn: string;
}

function normalize(value: unknown): string {
  return String(value ?? '').normalize('NFKC').trim();
}

function tableDocumentKey(table: Pick<DataDictTable, 'documentId'>): string {
  return table.documentId ?? '__unbound__';
}

function rowValue(row: string[] | undefined, index: number): string {
  return normalize(row?.[index]);
}

export function relationshipTemplateStatus(
  table: Pick<DataDictTable, 'name' | 'fields' | 'rows'>,
  tableNames: Iterable<string> = [],
): RelationshipTemplateStatus {
  if (normalize(table.name) !== RELATIONSHIP_TEMPLATE_SHEET_NAME) {
    return {
      matched: false,
      status: 'not_template',
      message: '不是主外键映射模板',
      primaryKeyCount: 0,
      foreignKeyCount: 0,
    };
  }

  const headers = table.fields.map((field) => normalize(field.name));
  if (headers.length !== RELATIONSHIP_TEMPLATE_HEADERS.length
    || !RELATIONSHIP_TEMPLATE_HEADERS.every((header, index) => headers[index] === header)) {
    return {
      matched: false,
      status: 'invalid',
      message: '模板未匹配',
      primaryKeyCount: 0,
      foreignKeyCount: 0,
    };
  }

  const knownTables = new Set([...tableNames].map(normalize).filter(Boolean));
  let primaryKeyCount = 0;
  let foreignKeyCount = 0;
  for (const row of table.rows ?? []) {
    const tableName = rowValue(row, 0);
    const columnName = rowValue(row, 1);
    const constraintType = rowValue(row, 2);
    const referencedTable = rowValue(row, 3);
    const referencedColumn = rowValue(row, 4);
    if (!tableName && !columnName && !constraintType && !referencedTable && !referencedColumn) continue;
    if (!tableName || !columnName) {
      return { matched: false, status: 'invalid', message: '模板未匹配', primaryKeyCount, foreignKeyCount };
    }
    if (knownTables.size && !knownTables.has(tableName)) {
      return { matched: false, status: 'invalid', message: '模板未匹配', primaryKeyCount, foreignKeyCount };
    }
    if (constraintType === 'PRIMARY_KEY') {
      if (referencedTable || referencedColumn) {
        return { matched: false, status: 'invalid', message: '模板未匹配', primaryKeyCount, foreignKeyCount };
      }
      primaryKeyCount += 1;
      continue;
    }
    if (constraintType === 'FOREIGN_KEY') {
      if (!referencedTable || !referencedColumn) {
        return { matched: false, status: 'invalid', message: '模板未匹配', primaryKeyCount, foreignKeyCount };
      }
      if (knownTables.size && !knownTables.has(referencedTable)) {
        return { matched: false, status: 'invalid', message: '模板未匹配', primaryKeyCount, foreignKeyCount };
      }
      foreignKeyCount += 1;
      continue;
    }
    return { matched: false, status: 'invalid', message: '模板未匹配', primaryKeyCount, foreignKeyCount };
  }

  return {
    matched: true,
    status: 'matched',
    message: foreignKeyCount ? `已匹配，${foreignKeyCount} 条外键关系` : '已匹配，暂无外键关系',
    primaryKeyCount,
    foreignKeyCount,
  };
}

export function isRelationshipTemplateTable(
  table: Pick<DataDictTable, 'name' | 'fields' | 'rows'>,
  tableNames: Iterable<string> = [],
): boolean {
  return relationshipTemplateStatus(table, tableNames).matched;
}

function relationshipTablesByDocument(tables: readonly DataDictTable[]): Map<string, DataDictTable[]> {
  const byDocument = new Map<string, DataDictTable[]>();
  for (const table of tables) {
    const values = byDocument.get(tableDocumentKey(table)) ?? [];
    values.push(table);
    byDocument.set(tableDocumentKey(table), values);
  }
  return byDocument;
}

function relationshipTemplateTables(tables: readonly DataDictTable[]): DataDictTable[] {
  const byDocument = relationshipTablesByDocument(tables);
  return tables.filter((table) => {
    if (table.materialImportDisposition !== 'relationship') return false;
    const siblings = byDocument.get(tableDocumentKey(table)) ?? tables;
    return relationshipTemplateStatus(table, siblings.map((item) => item.name)).matched;
  });
}

export function hasAuthoritativeObjectRelationSource(tables: readonly DataDictTable[]): boolean {
  return relationshipTemplateTables(tables).length > 0;
}

function foreignKeyRows(table: DataDictTable): ForeignKeyRow[] {
  return (table.rows ?? []).flatMap((row, index): ForeignKeyRow[] => {
    if (rowValue(row, 2) !== 'FOREIGN_KEY') return [];
    return [{
      rowNumber: index + 2,
      tableName: rowValue(row, 0),
      columnName: rowValue(row, 1),
      referencedTable: rowValue(row, 3),
      referencedColumn: rowValue(row, 4),
    }];
  });
}

export function applyAuthoritativeObjectRelations(
  physical: readonly PhysicalSchemaObject[],
  tables: readonly DataDictTable[],
): PhysicalSchemaObject[] {
  const sources = relationshipTemplateTables(tables);
  if (!sources.length) return physical.map((item) => ({ ...item, object: { ...item.object } }));

  const objectBySource = new Map<string, PhysicalSchemaObject>();
  for (const item of physical) {
    objectBySource.set(`${tableDocumentKey(item.table)}\0${item.table.name}`, item);
  }

  const relationsByObject = new Map<string, Map<string, {
    relation: 'references';
    target: string;
    note: string;
    evidence: string[];
  }>>();

  const relationBucket = (objectKey: string) => {
    const existing = relationsByObject.get(objectKey);
    if (existing) return existing;
    const next = new Map<string, {
      relation: 'references';
      target: string;
      note: string;
      evidence: string[];
    }>();
    relationsByObject.set(objectKey, next);
    return next;
  };

  for (const sourceTable of sources) {
    const documentKey = tableDocumentKey(sourceTable);
    for (const row of foreignKeyRows(sourceTable)) {
      const source = objectBySource.get(`${documentKey}\0${row.tableName}`);
      const target = objectBySource.get(`${documentKey}\0${row.referencedTable}`);
      if (!source || !target || source.object.key === target.object.key) continue;
      const bucket = relationBucket(source.object.key);
      const relationKey = `${target.object.key}\0references`;
      const note = `主外键映射：${row.tableName}.${row.columnName} → ${row.referencedTable}.${row.referencedColumn}`;
      const evidence = sourceTable.documentId
        ? `material:${sourceTable.documentId}#${sourceTable.name}!${row.rowNumber}`
        : `${sourceTable.name}!${row.rowNumber}`;
      const existing = bucket.get(relationKey);
      if (existing) {
        if (!existing.note.includes(note)) existing.note = `${existing.note}；${note}`;
        if (!existing.evidence.includes(evidence)) existing.evidence.push(evidence);
      } else {
        bucket.set(relationKey, {
          relation: 'references',
          target: target.object.key,
          note,
          evidence: [evidence],
        });
      }
    }
  }

  return physical.map((item) => ({
    ...item,
    object: {
      ...item.object,
      relations: [...(relationsByObject.get(item.object.key)?.values() ?? [])],
    },
  }));
}
