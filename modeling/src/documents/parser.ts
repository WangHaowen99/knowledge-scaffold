import { extname } from 'node:path';
import * as XLSX from 'xlsx';
import mammoth from 'mammoth';
import type { DataDictField, DataDictTable, DocumentRecord } from '../types.js';

export type DocKind = DocumentRecord['kind'];

export function kindOf(filename: string): DocKind {
  const ext = extname(filename).toLowerCase();
  if (ext === '.md' || ext === '.markdown') return 'markdown';
  if (ext === '.txt') return 'text';
  if (ext === '.pdf') return 'pdf';
  if (ext === '.docx' || ext === '.doc') return 'word';
  if (ext === '.xlsx' || ext === '.xls') return 'excel';
  return 'unknown';
}

export interface ParseResult {
  textContent?: string;
  tables?: DataDictTable[];
  error?: string;
}

// 统一解析入口：excel → 数据字典；其余 → 正文文本。
export async function parseDocument(kind: DocKind, buffer: Buffer): Promise<ParseResult> {
  try {
    switch (kind) {
      case 'markdown':
      case 'text':
        return { textContent: buffer.toString('utf8') };
      case 'excel':
        return { tables: parseExcel(buffer) };
      case 'word':
        return { textContent: (await mammoth.extractRawText({ buffer })).value };
      case 'pdf':
        return await parsePdf(buffer);
      default:
        return { error: '不支持的文件类型' };
    }
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

// Excel 语义（design.md 决策 5）：sheet=表，首行=字段名，次行可选注释，余行=样例数据。
function parseExcel(buffer: Buffer): DataDictTable[] {
  const wb = XLSX.read(buffer, { type: 'buffer' });
  const tables: DataDictTable[] = [];
  for (const sheetName of wb.SheetNames) {
    const sheet = wb.Sheets[sheetName];
    const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1, blankrows: false });
    if (!rows.length) continue;
    const header = (rows[0] as unknown[]).map((c) => String(c ?? '').trim());
    if (!header.some((h) => h)) continue;

    // 次行：若整行都不像数据（短、含中文/单位）视作注释行——简单启发式：
    // 若第二行单元格数量与表头一致且多数非纯数字，认定为注释行。
    let noteRow: string[] | null = null;
    let dataStart = 1;
    if (rows.length > 1) {
      const second = (rows[1] as unknown[]).map((c) => String(c ?? '').trim());
      const looksLikeNote = second.length === header.length
        && second.filter((v) => v && !/^-?\d+(\.\d+)?$/.test(v)).length >= header.length / 2
        && second.some((v) => /[一-龥：:（(]/.test(v));
      if (looksLikeNote) { noteRow = second; dataStart = 2; }
    }

    const dataRows = rows.slice(dataStart) as unknown[][];
    const fields: DataDictField[] = header.map((name, col) => {
      const values = dataRows.map((r) => String((r[col] ?? '')).trim()).filter((v) => v !== '');
      const distinct = new Set(values);
      return {
        name,
        note: noteRow?.[col] || undefined,
        inferredType: inferType(values),
        sampleValues: [...distinct].slice(0, 5),
        distinctRatio: values.length ? distinct.size / values.length : 0,
      };
    });

    tables.push({ name: sheetName, fields, rowCount: dataRows.length });
  }
  return tables;
}

function inferType(values: string[]): DataDictField['inferredType'] {
  if (!values.length) return 'unknown';
  const sample = values.slice(0, 20);
  if (sample.every((v) => /^-?\d+(\.\d+)?$/.test(v))) return 'number';
  if (sample.every((v) => /^(true|false|是|否|yes|no|0|1)$/i.test(v))) return 'boolean';
  if (sample.every((v) => /^\d{4}[-/]\d{1,2}([-/]\d{1,2})?/.test(v))) return 'date';
  return 'string';
}

// pdf-parse 是 CJS，动态 import 避免 ESM 顶层加载其内置测试副作用。
async function parsePdf(buffer: Buffer): Promise<ParseResult> {
  const mod = await import('pdf-parse');
  const pdf = (mod as { default?: (b: Buffer) => Promise<{ text: string }> }).default ?? (mod as any);
  const out = await pdf(buffer);
  const text = (out.text || '').trim();
  if (!text) return { error: '未能从 PDF 提取文本（可能是扫描版，暂不支持）' };
  return { textContent: text };
}
