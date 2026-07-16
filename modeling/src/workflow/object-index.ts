export interface ObjectIndexEntry {
  key: string;
  sourceSheet: string;
}

export function markdownTableCells(line: string): string[] {
  const value = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells: string[] = [];
  let current = '';
  let escaped = false;
  for (const char of value) {
    if (escaped) { current += char; escaped = false; continue; }
    if (char === '\\') { escaped = true; continue; }
    if (char === '|') { cells.push(current.trim()); current = ''; continue; }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

export function parseObjectIndex(content: string): Map<string, ObjectIndexEntry> {
  const rows = content.replace(/\r\n/g, '\n').split('\n')
    .filter((line) => /^\s*\|/.test(line))
    .map(markdownTableCells);
  const headerIndex = rows.findIndex((cells) => cells.some((cell) => cell.toLowerCase() === 'object key'));
  if (headerIndex < 0) throw new Error('objects/index.md 缺少 object key 列');
  const header = rows[headerIndex];
  const keyIndex = header.findIndex((cell) => cell.toLowerCase() === 'object key');
  const sourceSheetIndex = header.findIndex((cell) => cell.toLowerCase() === '来源sheet');
  if (sourceSheetIndex < 0) throw new Error('objects/index.md 缺少 来源Sheet 列');

  const entries = new Map<string, ObjectIndexEntry>();
  for (const cells of rows.slice(headerIndex + 1)) {
    const key = String(cells[keyIndex] ?? '').trim();
    if (!key || /^[-:]+$/.test(key)) continue;
    if (entries.has(key)) throw new Error(`objects/index.md 中对象 ${key} 重复`);
    const sourceSheet = String(cells[sourceSheetIndex] ?? '').trim();
    if (!sourceSheet || /^[-:]+$/.test(sourceSheet)) {
      throw new Error(`objects/index.md 中对象 ${key} 缺少 来源Sheet`);
    }
    entries.set(key, { key, sourceSheet });
  }
  return entries;
}
