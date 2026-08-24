import { basename, extname } from 'node:path';
import XLSX from 'xlsx';

import type { DataDictTable, DocumentRecord } from '../types.js';

export interface DocumentExport {
  buffer: Buffer;
  filename: string;
  contentType: string;
  mode: 'original' | 'normalized';
}

function safeFilename(value: string): string {
  return basename(value).replace(/[\r\n"\\/]/g, '_').trim() || 'material';
}

function contentType(document: DocumentRecord): string {
  if (document.kind === 'excel') {
    return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  }
  if (document.kind === 'csv') return 'text/csv; charset=utf-8';
  if (document.kind === 'pdf') return 'application/pdf';
  if (document.kind === 'word') {
    return extname(document.filename).toLowerCase() === '.doc'
      ? 'application/msword'
      : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  }
  return 'text/plain; charset=utf-8';
}

function tableRows(table: DataDictTable): string[][] {
  const header = table.fields.map((field) => field.name);
  const notes = table.fields.map((field) => field.note ?? '');
  return [
    header,
    ...(notes.some(Boolean) ? [notes] : []),
    ...(table.rows ?? []),
  ];
}

function uniqueSheetName(value: string, used: Set<string>): string {
  const base = value.replace(/[\\/?*\[\]:]/g, '_').slice(0, 31) || 'Sheet';
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate)) {
    const tail = `_${suffix++}`;
    candidate = `${base.slice(0, 31 - tail.length)}${tail}`;
  }
  used.add(candidate);
  return candidate;
}

function normalizedTableExport(document: DocumentRecord): DocumentExport | null {
  const tables = document.tables ?? [];
  if (!tables.length) return null;
  if (document.kind === 'csv' && tables.length === 1) {
    const sheet = XLSX.utils.aoa_to_sheet(tableRows(tables[0]));
    return {
      buffer: Buffer.from(`\ufeff${XLSX.utils.sheet_to_csv(sheet)}`, 'utf8'),
      filename: safeFilename(document.filename).replace(/\.[^.]+$/, '') + '.csv',
      contentType: 'text/csv; charset=utf-8',
      mode: 'normalized',
    };
  }
  const workbook = XLSX.utils.book_new();
  const used = new Set<string>();
  for (const table of tables) {
    XLSX.utils.book_append_sheet(
      workbook,
      XLSX.utils.aoa_to_sheet(tableRows(table)),
      uniqueSheetName(table.name, used),
    );
  }
  return {
    buffer: Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' })),
    filename: safeFilename(document.filename).replace(/\.[^.]+$/, '') + '.xlsx',
    contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    mode: 'normalized',
  };
}

export function buildDocumentExport(
  document: DocumentRecord,
  rawContent: Buffer | null,
): DocumentExport | null {
  if (rawContent) {
    return {
      buffer: rawContent,
      filename: safeFilename(document.filename),
      contentType: contentType(document),
      mode: 'original',
    };
  }
  const tableExport = normalizedTableExport(document);
  if (tableExport) return tableExport;
  if (document.textContent != null) {
    const sourceName = safeFilename(document.filename);
    const textKind = ['markdown', 'text'].includes(document.kind);
    return {
      buffer: Buffer.from(document.textContent, 'utf8'),
      filename: textKind ? sourceName : `${sourceName}.extracted.txt`,
      contentType: 'text/plain; charset=utf-8',
      mode: 'normalized',
    };
  }
  return null;
}
