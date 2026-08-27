import type { DataDictTable } from '../types.js';

function normalized(value: string): string {
  return value.trim().normalize('NFC').toLowerCase();
}

function cell(row: string[], index: number): string {
  return String(row[index] ?? '').trim();
}

export function validateTermTables(tables: DataDictTable[]): void {
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
