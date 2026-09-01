import type { SpecScript } from '../types.js';

export type ScriptParameter = SpecScript['parameters'][number];

export interface ScriptCliArguments {
  args: string[];
  missing: string[];
  invalid: string[];
}

export function buildScriptCliArguments(
  parameters: readonly ScriptParameter[],
  input: Readonly<Record<string, unknown>>,
): ScriptCliArguments {
  const args: string[] = [];
  const missing: string[] = [];
  const invalid: string[] = [];
  const consumed = new Set<string>();

  for (const parameter of parameters) {
    const name = String(parameter.name ?? '').trim();
    if (!name) continue;
    const flag = String(parameter.flag ?? '').trim();
    const matched = parameterArgumentKeys(parameter).find((key) => Object.hasOwn(input, key));
    let value = matched === undefined ? undefined : input[matched];
    if (matched !== undefined) consumed.add(matched);

    if (value === undefined || value === null) {
      if (parameter.defaultValue !== undefined && parameter.defaultValue !== null) {
        value = parameter.defaultValue;
      } else if (parameter.required) {
        missing.push(parameter.description?.trim() || name);
        continue;
      } else {
        continue;
      }
    }

    if (parameter.multiple) {
      const values = Array.isArray(value) ? value.filter((item) => item != null) : [value];
      if (!values.length) {
        if (parameter.required) missing.push(parameter.description?.trim() || name);
        continue;
      }
      for (const item of values) {
        appendConverted(args, invalid, parameter, flag, name, item);
      }
      continue;
    }

    if (isStructured(value) && !acceptsStructuredValue(parameter)) {
      invalid.push(`${name} does not accept multiple values`);
      continue;
    }
    appendConverted(args, invalid, parameter, flag, name, value);
  }

  for (const [key, value] of Object.entries(input)) {
    if (value === undefined || value === null || consumed.has(key)) continue;
    invalid.push(`Unknown argument '${key}'`);
  }

  return { args, missing, invalid };
}

function appendConverted(
  args: string[],
  invalid: string[],
  parameter: ScriptParameter,
  flag: string,
  name: string,
  value: unknown,
): void {
  const converted = convertParameterValue(value, parameter.type);
  if (converted.error) {
    invalid.push(`${name}: ${converted.error}`);
    return;
  }
  const action = normalizeBooleanAction(parameter.action);
  if (isBooleanType(parameter.type) && action === 'store_true') {
    if (converted.value === 'true' && flag) args.push(flag);
    return;
  }
  if (isBooleanType(parameter.type) && action === 'store_false') {
    if (converted.value === 'false' && flag) args.push(flag);
    return;
  }
  if (flag) args.push(flag);
  args.push(converted.value!);
}

function convertParameterValue(
  value: unknown,
  declaredType: string,
): { value?: string; error?: string } {
  const type = String(declaredType || 'string').trim().toLowerCase();
  if (type === 'string' && isStructured(value)) {
    return jsonValue(value, 'expected string or JSON-serializable structured value');
  }
  if (type === 'array' || type === 'list' || type.endsWith('[]')) {
    const parsed = parseJsonString(value);
    return Array.isArray(parsed) ? jsonValue(parsed, 'expected array') : { error: 'expected array' };
  }
  if (type === 'object' || type === 'dict') {
    const parsed = parseJsonString(value);
    return isPlainObject(parsed) ? jsonValue(parsed, 'expected object') : { error: 'expected object' };
  }
  if (type === 'json') {
    const parsed = parseJsonString(value);
    return parsed === JSON_PARSE_ERROR ? { error: 'expected JSON' } : jsonValue(parsed, 'expected JSON');
  }
  if (type === 'int' || type === 'integer') {
    if (typeof value === 'boolean') return { value: value ? '1' : '0' };
    if (typeof value === 'number') {
      return Number.isFinite(value) ? { value: String(Math.trunc(value)) } : { error: 'expected integer' };
    }
    const source = String(value).trim();
    if (!/^[+-]?\d+$/u.test(source)) return { error: 'expected integer' };
    try {
      return { value: BigInt(source).toString() };
    } catch {
      return { error: 'expected integer' };
    }
  }
  if (type === 'float' || type === 'number') {
    const numeric = typeof value === 'boolean' ? (value ? 1 : 0) : Number(value);
    return Number.isFinite(numeric) ? { value: pythonFloatString(numeric) } : { error: 'expected number' };
  }
  if (isBooleanType(type)) {
    if (typeof value === 'boolean') return { value: value ? 'true' : 'false' };
    const normalized = String(value).trim().toLowerCase();
    if (['true', '1', 'yes'].includes(normalized)) return { value: 'true' };
    if (['false', '0', 'no'].includes(normalized)) return { value: 'false' };
    return { error: 'expected boolean' };
  }
  return { value: pythonString(value) };
}

const JSON_PARSE_ERROR = Symbol('json-parse-error');

function parseJsonString(value: unknown): unknown | typeof JSON_PARSE_ERROR {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return JSON_PARSE_ERROR;
  }
}

function jsonValue(value: unknown, error: string): { value?: string; error?: string } {
  if (value === JSON_PARSE_ERROR) return { error };
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined ? { error } : { value: serialized };
  } catch {
    return { error };
  }
}

function parameterArgumentKeys(parameter: ScriptParameter): string[] {
  const keys: string[] = [];
  const add = (raw: unknown, stripFlag = false) => {
    let value = String(raw ?? '').trim();
    if (stripFlag) value = value.replace(/^-+/u, '');
    if (!value) return;
    for (const candidate of [value, value.replaceAll('-', '_'), value.replaceAll('_', '-')]) {
      if (candidate && !keys.includes(candidate)) keys.push(candidate);
    }
  };
  add(parameter.name);
  add(parameter.flag, true);
  add(parameter.description);
  return keys;
}

function normalizeBooleanAction(value: unknown): '' | 'store_true' | 'store_false' {
  const action = String(value ?? '').trim().toLowerCase();
  return action === 'store_true' || action === 'store_false' ? action : '';
}

function isBooleanType(value: string): boolean {
  return ['bool', 'boolean'].includes(String(value ?? '').trim().toLowerCase());
}

function acceptsStructuredValue(parameter: ScriptParameter): boolean {
  const type = String(parameter.type || 'string').trim().toLowerCase();
  return type === 'string' || ['array', 'list', 'object', 'dict', 'json'].includes(type) || type.endsWith('[]');
}

function isStructured(value: unknown): boolean {
  return Array.isArray(value) || isPlainObject(value);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pythonString(value: unknown): string {
  if (value === true) return 'True';
  if (value === false) return 'False';
  return String(value);
}

function pythonFloatString(value: number): string {
  if (Object.is(value, -0)) return '-0.0';
  const text = String(value);
  if (!/[.eE]/u.test(text)) return `${text}.0`;
  return text.replace(/e([+-])(\d)$/u, 'e$10$2');
}
