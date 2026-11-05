// backend/app/evidence/spec.py 的 Node 版：规范 JSON、记录哈希、锚点编码。两边用 evidence_vectors.json 核对，改一边要同步另一边。
import { createHash } from 'node:crypto';

const ANCHOR_ESCAPE = new Set(['%', '/', '.', '#', '@', '|', '[', ']', ' ']);
export const SEPARATOR = '::';

// Python 的 sort_keys 按码点排序；JS 默认按 UTF-16 码元，增补平面字符会排错
function compareCodePoints(a, b) {
  const left = Array.from(a);
  const right = Array.from(b);
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const diff = left[index].codePointAt(0) - right[index].codePointAt(0);
    if (diff !== 0) return diff;
  }
  return left.length - right.length;
}

/** 按键排序、无空白、不转义非 ASCII；记录里不允许浮点数，遇到直接报错。 */
export function canonicalJson(value) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) throw new TypeError(`规范 JSON 只允许整数：${value}`);
    return String(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (typeof value === 'object') {
    const keys = Object.keys(value).filter((key) => value[key] !== undefined).sort(compareCodePoints);
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  throw new TypeError(`规范 JSON 不支持 ${typeof value}`);
}

export function sha256Text(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

export function scopeHash(value) {
  return sha256Text(canonicalJson(value));
}

export function jobScope(jobId) {
  return scopeHash({ job_id: jobId });
}

export function recordHash(record) {
  const { hash: _ignored, ...rest } = record;
  return scopeHash(rest);
}

export function encodeSegment(text) {
  let out = '';
  for (const char of String(text)) {
    out += ANCHOR_ESCAPE.has(char) ? `%${char.codePointAt(0).toString(16).toUpperCase().padStart(2, '0')}` : char;
  }
  return out;
}

export function qualified(...segments) {
  return segments.map(encodeSegment).join(SEPARATOR);
}

export function binAnchor(sourceId, kind, ...segments) {
  return `bin:${sourceId}/${kind}:${qualified(...segments)}`;
}

export function relationAnchor(sourceId, fromAnchor, name, to) {
  return `bin:${sourceId}/rel:${sha256Text(`${fromAnchor}|${name}|${to}`).slice(0, 16)}`;
}

export function encodeJobHeader(spec) {
  return Buffer.from(canonicalJson(spec), 'utf8').toString('base64url');
}

export function decodeJobHeader(value) {
  const data = JSON.parse(Buffer.from(value, 'base64url').toString('utf8'));
  if (data === null || typeof data !== 'object' || Array.isArray(data)) throw new TypeError('任务说明不是对象');
  return data;
}
