import type { DataDictCell, DataDictTable } from '../types.js';
import type { GraphImportPropertyType } from './types.js';

const MIN_NEO4J_INTEGER = -9_223_372_036_854_775_808n;
const MAX_NEO4J_INTEGER = 9_223_372_036_854_775_807n;
const MAX_FLOAT_SIGNIFICANT_DIGITS = 15;

const INTEGER_PATTERN = /^[+-]?\d+$/;
const NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const BOOLEAN_VALUES: Record<string, boolean> = {
  true: true,
  false: false,
  yes: true,
  no: false,
  y: true,
  n: false,
  '是': true,
  '否': false,
};

export type GraphImportInferenceIssue =
  | 'mixed_types'
  | 'integer_out_of_range'
  | 'unsafe_excel_integer'
  | 'float_precision';

export interface GraphImportColumnProfile {
  type: GraphImportPropertyType;
  issues: GraphImportInferenceIssue[];
}

interface CellClassification {
  type: GraphImportPropertyType;
  issue?: GraphImportInferenceIssue;
}

interface DateParts {
  year: number;
  month: number;
  day: number;
}

interface LocalDateTimeParts extends DateParts {
  hour: number;
  minute: number;
  second: number;
}

function cellAt(table: DataDictTable, rowIndex: number, column: number): DataDictCell {
  const metadata = table.cellRows?.[rowIndex]?.[column];
  if (metadata) return metadata;
  const value = String(table.rows?.[rowIndex]?.[column] ?? '').trim();
  return {
    kind: value ? 'string' : 'blank',
    value,
    formatted: value,
  };
}

function hasLeadingZeroInteger(value: string): boolean {
  return /^0\d+$/.test(value.replace(/^[+-]/, ''));
}

function isFormattedIntegerCode(cell: DataDictCell): boolean {
  return cell.kind === 'number'
    && INTEGER_PATTERN.test(cell.value)
    && hasLeadingZeroInteger(cell.formatted)
    && cell.formatted !== cell.value;
}

function integerInRange(value: string): boolean {
  try {
    const integer = BigInt(value);
    return integer >= MIN_NEO4J_INTEGER && integer <= MAX_NEO4J_INTEGER;
  } catch {
    return false;
  }
}

function significantDigits(value: string): number {
  const mantissa = value.replace(/^[+-]/, '').split(/[eE]/, 1)[0].replace('.', '');
  return mantissa.replace(/^0+/, '').length || 1;
}

function dateParts(value: string): DateParts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const parts = { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
  const leap = parts.year % 4 === 0 && (parts.year % 100 !== 0 || parts.year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (parts.month < 1 || parts.month > 12 || parts.day < 1 || parts.day > days[parts.month - 1]) {
    return null;
  }
  return parts;
}

function localDateTimeParts(value: string): LocalDateTimeParts | null {
  const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?$/.exec(value);
  if (!match) return null;
  const date = dateParts(match[1]);
  if (!date) return null;
  const hour = Number(match[2]);
  const minute = Number(match[3]);
  const second = Number(match[4] ?? '0');
  if (hour > 23 || minute > 59 || second > 59) return null;
  return { ...date, hour, minute, second };
}

function dateCellType(cell: DataDictCell): GraphImportPropertyType {
  const dateTime = localDateTimeParts(cell.value);
  if (!dateTime) return dateParts(cell.value) ? 'date' : 'string';
  return /\d{1,2}:\d{2}/.test(cell.formatted)
    || dateTime.hour || dateTime.minute || dateTime.second
    ? 'local_datetime'
    : 'date';
}

function classifyText(value: string): CellClassification {
  if (hasLeadingZeroInteger(value)) return { type: 'string' };
  if (BOOLEAN_VALUES[value.toLocaleLowerCase()] !== undefined) return { type: 'boolean' };
  if (localDateTimeParts(value)) return { type: 'local_datetime' };
  if (dateParts(value)) return { type: 'date' };
  if (INTEGER_PATTERN.test(value)) {
    return integerInRange(value)
      ? { type: 'integer' }
      : { type: 'string', issue: 'integer_out_of_range' };
  }
  if (NUMBER_PATTERN.test(value) && Number.isFinite(Number(value))) {
    return significantDigits(value) <= MAX_FLOAT_SIGNIFICANT_DIGITS
      ? { type: 'float' }
      : { type: 'string', issue: 'float_precision' };
  }
  return { type: 'string' };
}

function classifyCell(cell: DataDictCell): CellClassification {
  if (isFormattedIntegerCode(cell)) return { type: 'string' };
  if (cell.kind === 'boolean') return { type: 'boolean' };
  if (cell.kind === 'date') return { type: dateCellType(cell) };
  if (cell.kind === 'number') {
    const number = Number(cell.value);
    if (!Number.isFinite(number)) return { type: 'string' };
    if (!Number.isInteger(number)) return { type: 'float' };
    return Number.isSafeInteger(number)
      ? { type: 'integer' }
      : { type: 'string', issue: 'unsafe_excel_integer' };
  }
  return classifyText(cell.value);
}

function resolvedType(types: Set<GraphImportPropertyType>): GraphImportPropertyType {
  if (!types.size) return 'string';
  if ([...types].every((type) => type === 'integer' || type === 'float')) {
    return types.has('float') ? 'float' : 'integer';
  }
  if ([...types].every((type) => type === 'date' || type === 'local_datetime')) {
    return types.has('local_datetime') ? 'local_datetime' : 'date';
  }
  if (types.size === 1) return [...types][0];
  return 'string';
}

export function inferGraphImportColumns(table: DataDictTable): GraphImportColumnProfile[] {
  return table.fields.map((_field, column) => {
    const classifications = (table.rows ?? []).flatMap((_row, rowIndex) => {
      const cell = cellAt(table, rowIndex, column);
      return cell.kind === 'blank' || !cell.value ? [] : [classifyCell(cell)];
    });
    const types = new Set(classifications.map((item) => item.type));
    const type = resolvedType(types);
    const issues = new Set(classifications.flatMap((item) => item.issue ? [item.issue] : []));
    const compatibleNumbers = [...types].every((item) => item === 'integer' || item === 'float');
    const compatibleDates = [...types].every((item) => item === 'date' || item === 'local_datetime');
    if (types.size > 1 && !compatibleNumbers && !compatibleDates) issues.add('mixed_types');
    return { type, issues: [...issues] };
  });
}

function pad(value: number, length = 2): string {
  return String(value).padStart(length, '0');
}

function normalizeDateCell(value: string): string {
  const dateTime = localDateTimeParts(value);
  if (dateTime) {
    return `${pad(dateTime.year, 4)}-${pad(dateTime.month)}-${pad(dateTime.day)}`;
  }
  return value;
}

function normalizeLocalDateTime(value: string): string {
  const parsed = localDateTimeParts(value);
  if (parsed) {
    return `${pad(parsed.year, 4)}-${pad(parsed.month)}-${pad(parsed.day)}`
      + `T${pad(parsed.hour)}:${pad(parsed.minute)}:${pad(parsed.second)}`;
  }
  const date = dateParts(value);
  if (date) return `${value}T00:00:00`;
  return value;
}

export function graphImportCellValue(
  table: DataDictTable,
  rowIndex: number,
  column: number,
  type: GraphImportPropertyType,
): string {
  const cell = cellAt(table, rowIndex, column);
  if (cell.kind === 'blank' || !cell.value) return '';
  if (type === 'string') return isFormattedIntegerCode(cell) ? cell.formatted : cell.value;
  if (type === 'integer') return BigInt(cell.value).toString();
  if (type === 'float') return String(Number(cell.value));
  if (type === 'boolean') return String(BOOLEAN_VALUES[cell.value.toLocaleLowerCase()] ?? cell.value === 'true');
  if (type === 'date') return cell.kind === 'date' ? normalizeDateCell(cell.value) : cell.value;
  return normalizeLocalDateTime(cell.value);
}
