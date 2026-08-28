import type { DataDictTable } from '../types.js';
import type { SelectablePackageDataSourceType } from './package-data-source-selection.js';
import { assertGraphIdentifier } from '../evaluation/data-source.js';

export interface TermTableValidationOptions {
  dataSourceType?: SelectablePackageDataSourceType;
}

function normalized(value: string): string {
  return value.trim().normalize('NFC').toLowerCase();
}

function cell(row: string[], index: number): string {
  return String(row[index] ?? '').trim();
}

function validateNeo4jTermFieldName(tableName: string, fieldName: string, columnIndex: number): void {
  try {
    assertGraphIdentifier(fieldName, `术语表 sheet「${tableName}」第 ${columnIndex} 列表头「${fieldName}」`);
  } catch {
    throw new Error(`术语表 sheet「${tableName}」第 ${columnIndex} 列表头「${fieldName}」不能作为 Neo4j 属性名：只能以中文、英文字母或下划线开头，后续只能包含中文、英文字母、数字或下划线`);
  }
}

export function validateTermTables(
  tables: DataDictTable[],
  options: TermTableValidationOptions = {},
): void {
  if (!tables.length) throw new Error('术语表文件至少需要包含一个 sheet');

  for (const table of tables) {
    if (table.fields.length < 2) {
      throw new Error(`术语表 sheet「${table.name}」至少需要两列：第 1 列 value，第 2 列 semantic`);
    }
    const valueField = table.fields[0]?.name.trim();
    const semanticField = table.fields[1]?.name.trim();
    if (!valueField || !semanticField) {
      throw new Error(`术语表 sheet「${table.name}」第 1 列和第 2 列表头不能为空`);
    }
    if (options.dataSourceType === 'neo4j') {
      table.fields.forEach((field, index) => {
        const fieldName = field.name.trim();
        if (fieldName) validateNeo4jTermFieldName(table.name, fieldName, index + 1);
      });
    }

    const semanticsByValue = new Map<string, string>();
    for (const row of table.rows ?? []) {
      const value = cell(row, 0);
      const semantic = cell(row, 1);
      if (!value || !semantic) continue;
      const valueKey = normalized(value);
      const semanticKey = normalized(semantic);
      const existing = semanticsByValue.get(valueKey);
      if (existing && existing !== semanticKey) {
        throw new Error(`术语表 sheet「${table.name}」中 value「${value}」对应多个 semantic，请先整理为唯一关系`);
      }
      semanticsByValue.set(valueKey, semanticKey);
    }
  }
}

export function isTermTable(table: DataDictTable): boolean {
  return table.materialPurpose === 'term';
}
