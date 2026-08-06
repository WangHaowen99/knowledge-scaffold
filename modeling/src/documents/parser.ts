import { basename, extname } from 'node:path';
import XLSX from 'xlsx';
import mammoth from 'mammoth';
import type {
  DataDictCell,
  DataDictField,
  DataDictTable,
  DocumentRecord,
} from '../types.js';

export type DocKind = DocumentRecord['kind'];

export function kindOf(filename: string): DocKind {
  const ext = extname(filename).toLowerCase();
  if (ext === '.md' || ext === '.markdown') return 'markdown';
  if (ext === '.txt') return 'text';
  if (ext === '.pdf') return 'pdf';
  if (ext === '.docx' || ext === '.doc') return 'word';
  if (ext === '.xlsx' || ext === '.xls') return 'excel';
  if (ext === '.csv') return 'csv';
  return 'unknown';
}

export interface ParseResult {
  textContent?: string;
  tables?: DataDictTable[];
  error?: string;
}

// 统一解析入口：Excel / CSV → 数据字典；其余 → 正文文本。
export async function parseDocument(kind: DocKind, buffer: Buffer, filename = ''): Promise<ParseResult> {
  try {
    switch (kind) {
      case 'markdown':
      case 'text':
        return { textContent: buffer.toString('utf8') };
      case 'excel':
        return { tables: parseExcel(buffer) };
      case 'csv':
        return {
          tables: parseExcel(buffer, {
            singleTableName: basename(filename, extname(filename)).trim() || 'data',
          }),
        };
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
export interface ParseExcelOptions {
  /** null 表示保留全部数据行；未传时沿用普通文档解析的 2000 行上限。 */
  maxRows?: number | null;
  rejectOverflow?: boolean;
  /** 图导入使用：额外保留单元格原始类型与 Excel 展示值。 */
  includeCellMetadata?: boolean;
  /** CSV 只有一张逻辑表，使用文件名替代工作簿默认 Sheet 名。 */
  singleTableName?: string;
}

interface ExcelDateTimeParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

interface ExcelSheetRow {
  raw: unknown[];
  formatted?: unknown[];
  sheetRow: number;
}

interface ExcelFormatSemantics {
  hasDate: boolean;
  hasTime: boolean;
}

function padDatePart(value: number, length = 2): string {
  return String(value).padStart(length, '0');
}

function excelDateTimeParts(
  cell: XLSX.CellObject | undefined,
  date1904: boolean,
): ExcelDateTimeParts | null {
  if (!cell || cell.t !== 'n' || typeof cell.v !== 'number' || cell.z == null
    || !XLSX.SSF.is_date(String(cell.z))) return null;
  const parsed = XLSX.SSF.parse_date_code(cell.v, { date1904 });
  if (!parsed) return null;
  return {
    year: parsed.y,
    month: parsed.m,
    day: parsed.d,
    hour: parsed.H,
    minute: parsed.M,
    second: parsed.S,
  };
}

function localDateTimeValue(parts: ExcelDateTimeParts): string {
  return `${padDatePart(parts.year, 4)}-${padDatePart(parts.month)}-${padDatePart(parts.day)}`
    + `T${padDatePart(parts.hour)}:${padDatePart(parts.minute)}:${padDatePart(parts.second)}`;
}

function dateValue(parts: ExcelDateTimeParts): string {
  return localDateTimeValue(parts).slice(0, 10);
}

function timeValue(parts: ExcelDateTimeParts): string {
  return `${padDatePart(parts.hour)}:${padDatePart(parts.minute)}:${padDatePart(parts.second)}`;
}

function excelFormatSemantics(format: unknown): ExcelFormatSemantics {
  const source = String(format ?? '');
  const hasBracketedTime = /\[(?:h+|m+|s+)\]/i.test(source);
  const normalized = source
    .replace(/"[^"]*"/g, '')
    .replace(/\\./g, '')
    .replace(/\[[^\]]+\]/g, '');
  const hasTime = hasBracketedTime || /(?:h|s|am\/pm|a\/p)/i.test(normalized);
  const hasDate = /(?:y|d)/i.test(normalized) || (!hasTime && /m/i.test(normalized));
  return { hasDate, hasTime };
}

function stableDateTimeValue(parts: ExcelDateTimeParts, format: unknown): string {
  const semantics = excelFormatSemantics(format);
  if (semantics.hasTime && !semantics.hasDate) return timeValue(parts);
  const containsTime = semantics.hasTime || parts.hour !== 0 || parts.minute !== 0 || parts.second !== 0;
  return containsTime ? localDateTimeValue(parts) : dateValue(parts);
}

function dateTimePartsFromDate(value: Date): ExcelDateTimeParts {
  return {
    year: value.getUTCFullYear(),
    month: value.getUTCMonth() + 1,
    day: value.getUTCDate(),
    hour: value.getUTCHours(),
    minute: value.getUTCMinutes(),
    second: value.getUTCSeconds(),
  };
}

function worksheetCell(sheet: XLSX.WorkSheet, row: number, column: number): XLSX.CellObject | undefined {
  return sheet[XLSX.utils.encode_cell({ r: row, c: column })] as XLSX.CellObject | undefined;
}

function documentCellValue(
  raw: unknown,
  cell: XLSX.CellObject | undefined,
  date1904: boolean,
): unknown {
  const dateTime = excelDateTimeParts(cell, date1904);
  if (dateTime) return stableDateTimeValue(dateTime, cell?.z);
  return raw instanceof Date
    ? stableDateTimeValue(dateTimePartsFromDate(raw), cell?.z)
    : raw;
}

export function parseExcel(buffer: Buffer, options: ParseExcelOptions = {}): DataDictTable[] {
  const includeCellMetadata = options.includeCellMetadata === true;
  // 始终保留 Excel 日期序列，避免 JavaScript Date 的时区转换导致日期偏移或时间截断。
  const wb = XLSX.read(buffer, {
    type: 'buffer',
    cellDates: false,
    cellNF: true,
  });
  const date1904 = Boolean(wb.Workbook?.WBProps?.date1904);
  const tables: DataDictTable[] = [];
  for (const sheetName of wb.SheetNames) {
    const sheet = wb.Sheets[sheetName];
    const reference = sheet['!ref'];
    if (!reference) continue;
    const range = XLSX.utils.decode_range(reference);
    const rawRows = XLSX.utils.sheet_to_json<unknown[]>(sheet, {
      header: 1,
      blankrows: true,
      raw: true,
    });
    const formattedRows = includeCellMetadata
      ? XLSX.utils.sheet_to_json<unknown[]>(sheet, {
        header: 1,
        blankrows: true,
        raw: false,
      })
      : undefined;
    const sheetRows: ExcelSheetRow[] = rawRows.map((raw, index) => ({
      raw,
      formatted: formattedRows?.[index],
      sheetRow: range.s.r + index,
    })).filter((row) => row.raw.some((value) => value != null));
    const rows = sheetRows.map((row) => row.raw.map((value, column) => documentCellValue(
      value,
      worksheetCell(sheet, row.sheetRow, range.s.c + column),
      date1904,
    )));
    if (!rows.length) continue;
    const header = (rows[0] as unknown[]).map((c) => String(c ?? '').trim());
    if (!header.some((h) => h)) continue;

    // 次行注释判定。仅"多数非数字+含中文"会误吃中文数据行（真实业务数据常含中文枚举值），
    // 增加与第三行的"形态相似度"判据：注释行与数据行形态迥异，数据行之间形态相近。
    // 只有 header+1 行时宁可当数据（吃掉唯一数据行的代价远大于少一行注释）。
    const cellClass = (v: string) => v === '' ? 'empty'
      : /^-?\d+(\.\d+)?$/.test(v) ? 'num'
      : /[一-龥]/.test(v) ? 'cjk' : 'code';
    let noteRow: string[] | null = null;
    let dataStart = 1;
    if (rows.length > 2) {
      const second = (rows[1] as unknown[]).map((c) => String(c ?? '').trim());
      const third = (rows[2] as unknown[]).map((c) => String(c ?? '').trim());
      const chineseHeavy = second.length === header.length
        && second.filter((v) => v && !/^-?\d+(\.\d+)?$/.test(v)).length >= header.length / 2
        && second.some((v) => /[一-龥：:（(]/.test(v));
      const similar = header.filter((_, c) => cellClass(second[c] ?? '') === cellClass(third[c] ?? '')).length
        / Math.max(header.length, 1);
      if (chineseHeavy && similar < 0.6) { noteRow = second; dataStart = 2; }
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

    const maxRows = options.maxRows === undefined ? 2000 : options.maxRows;
    if (maxRows !== null && options.rejectOverflow && dataRows.length > maxRows) {
      throw new Error(`Sheet「${sheetName}」数据行数 ${dataRows.length} 超过上限 ${maxRows}`);
    }
    const selectedRows = maxRows === null ? dataRows : dataRows.slice(0, maxRows);
    const rowMatrix = selectedRows.map((r) =>
      header.map((_, col) => String((r[col] ?? '')).trim()));
    const cellRows = includeCellMetadata
      ? sheetRows.slice(dataStart, dataStart + selectedRows.length).map((row) => (
        header.map((_, column) => cellMetadata(
          row.raw[column],
          row.formatted?.[column],
          worksheetCell(sheet, row.sheetRow, range.s.c + column),
          date1904,
        ))
      ))
      : undefined;
    tables.push({
      name: options.singleTableName && wb.SheetNames.length === 1
        ? options.singleTableName
        : sheetName,
      fields,
      rowCount: dataRows.length,
      rows: rowMatrix,
      ...(cellRows ? { cellRows } : {}),
    });
  }
  return tables;
}

function cellMetadata(
  raw: unknown,
  formatted: unknown,
  cell: XLSX.CellObject | undefined,
  date1904: boolean,
): DataDictCell {
  const formattedText = String(formatted ?? cell?.w ?? '').trim();
  if (raw == null || raw === '') return { kind: 'blank', value: '', formatted: formattedText };
  const dateTime = excelDateTimeParts(cell, date1904);
  if (dateTime) {
    return { kind: 'date', value: localDateTimeValue(dateTime), formatted: formattedText };
  }
  if (raw instanceof Date) {
    return {
      kind: 'date',
      value: localDateTimeValue(dateTimePartsFromDate(raw)),
      formatted: formattedText,
    };
  }
  if (typeof raw === 'number') {
    return { kind: 'number', value: String(raw), formatted: formattedText };
  }
  if (typeof raw === 'boolean') {
    return { kind: 'boolean', value: String(raw), formatted: formattedText };
  }
  return { kind: 'string', value: String(raw).trim(), formatted: formattedText };
}

function inferType(values: string[]): DataDictField['inferredType'] {
  if (!values.length) return 'unknown';
  // 前导零编码（0001 之类）是字符串不是数字——按 number 落库会丢前导零（0002→2）
  if (values.some((v) => /^-?0\d/.test(v))) return 'string';
  if (values.every((v) => /^-?\d+(\.\d+)?$/.test(v))) {
    const hasUnsafeInteger = values.some((value) => /^-?\d+$/.test(value)
      && !Number.isSafeInteger(Number(value)));
    // MySQL 使用 DECIMAL(65,30)：超出 35 位整数或 30 位小数时按文本保存，
    // 避免全量导入在后段数据上失败，同时保留原始精度。
    const exceedsPortableDecimal = values.some((value) => {
      const [integer, fraction = ''] = value.replace(/^-/, '').split('.');
      return integer.length > 35 || fraction.length > 30 || integer.length + fraction.length > 65;
    });
    return hasUnsafeInteger || exceedsPortableDecimal ? 'string' : 'number';
  }
  if (values.every((v) => /^(true|false|是|否|yes|no|0|1)$/i.test(v))) return 'boolean';
  if (values.every((v) => /^\d{4}[-/]\d{1,2}([-/]\d{1,2})?/.test(v))) return 'date';
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
