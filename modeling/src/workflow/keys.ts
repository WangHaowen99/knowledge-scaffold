import type { PackageSpec } from '../types.js';

// 统一 key 校验与清洗（规范 §5.2-5.4）：
// - 清洗：NFC Unicode 规范化 + 去首尾空白（规范 L181「导入时应做 Unicode 规范化」）；
// - 校验：长度 ≤ 80；禁止 / \ .. # ? & | 与换行、制表符及控制字符
//   （路径穿越 / URL 歧义 / markdown 表格破坏）；
// - 唯一：包内同类资源 key 必须唯一——重复会导致 objects/<key>.md 等详情文件互相覆盖，
//   索引声明两行而详情只剩一份（破坏规范 §2.2「索引声明的详情文件必须存在」），必须报错。
// 三道闸门共用：LLM 工具 schema（tools.ts，pattern/描述引导）→ 渲染入包前强制校验
// （package-render.ts renderPackage 入口）→ zip 路径二次断言（export.ts）。

export const KEY_MAX_LENGTH = 80;

// JSON Schema pattern（工具 schema 层引导；`..` 禁则由 validateKey 强制）
export const KEY_SCHEMA_PATTERN = '^[^/\\\\#?&|\\r\\n\\t]{1,80}$';
export const KEY_SCHEMA_DESCRIPTION =
  '资源 key：包内稳定引用标识，推荐中文/字母/数字/下划线/短横线；不超过 80 字符；禁止 / \\ .. # ? & | 与换行、制表符；同类资源内必须唯一';

const FORBIDDEN_CHARS = /[/\\#?&|]/;
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

export function normalizeKey(raw: string): string {
  return String(raw ?? '').normalize('NFC').trim();
}

// 校验并返回清洗后的 key；不合法时抛带中文说明的错误（进入既有错误处理流）。
export function validateKey(raw: string, what = '资源'): string {
  const key = normalizeKey(raw);
  const shown = (key || String(raw ?? '')).slice(0, 60);
  if (!key) throw new Error(`${what}的 key 不能为空（或仅含空白字符）`);
  if (key.length > KEY_MAX_LENGTH) throw new Error(`${what}的 key「${shown}…」超过 ${KEY_MAX_LENGTH} 字符上限`);
  if (key.includes('..')) throw new Error(`${what}的 key「${shown}」包含 ..（路径穿越风险，禁止）`);
  if (FORBIDDEN_CHARS.test(key)) throw new Error(`${what}的 key「${shown}」包含非法字符（禁止 / \\ # ? & |）`);
  if (CONTROL_CHARS.test(key)) throw new Error(`${what}的 key「${shown}」包含换行/制表符/控制字符（禁止）`);
  return key;
}

function assertUnique(kind: string, keys: string[]): void {
  const seen = new Set<string>();
  for (const k of keys) {
    if (seen.has(k)) throw new Error(`知识包内${kind} key 重复：「${k}」——同类资源 key 必须唯一，重复会导致详情文件被静默覆盖，请改名后重试`);
    seen.add(k);
  }
}

// 渲染入包前的强制校验：清洗全部资源 key（NFC + trim）、校验合法性、断言同类唯一。
// 返回清洗后的规格副本；任何不合法直接抛错（不静默修正语义、不静默覆盖）。
export function validateSpecKeys(spec: PackageSpec): PackageSpec {
  const objects = (spec.objects ?? []).map((o) => ({
    ...o,
    key: validateKey(o.key, '对象'),
    fields: (o.fields ?? []).map((f) => ({ ...f, key: validateKey(f.key, `对象「${normalizeKey(o.key)}」的字段`) })),
    primaryKey: o.primaryKey?.map(normalizeKey),
    relations: o.relations?.map((r) => ({ ...r, target: normalizeKey(r.target) })),
  }));
  const operations = (spec.operations ?? []).map((op) => ({
    ...op,
    key: validateKey(op.key, '操作'),
    relatedObjects: (op.relatedObjects ?? []).map(normalizeKey),
    usesRules: op.usesRules?.map(normalizeKey),
    usesMetrics: op.usesMetrics?.map(normalizeKey),
    script: op.script ? { ...op.script, key: normalizeKey(op.script.key) } : undefined,
  }));
  const rules = spec.rules?.map((r) => ({
    ...r,
    key: validateKey(r.key, '规则'),
    appliesToObjects: r.appliesToObjects?.map(normalizeKey),
    appliesToOperations: r.appliesToOperations?.map(normalizeKey),
  }));
  const metrics = spec.metrics?.map((m) => ({
    ...m,
    key: validateKey(m.key, '指标'),
    usesObjects: m.usesObjects?.map(normalizeKey),
  }));
  const scripts = spec.scripts?.map((script) => ({
    ...script,
    key: validateKey(script.key, '脚本'),
    boundOperation: normalizeKey(script.boundOperation),
  }));
  assertUnique('对象', objects.map((o) => o.key));
  assertUnique('操作', operations.map((o) => o.key));
  if (rules) assertUnique('规则', rules.map((r) => r.key));
  if (metrics) assertUnique('指标', metrics.map((m) => m.key));
  if (scripts) assertUnique('脚本', scripts.map((script) => script.key));
  for (const o of objects) assertUnique(`对象「${o.key}」的字段`, o.fields.map((f) => f.key));
  return { ...spec, objects, operations, rules, metrics, scripts };
}

// zip 打包前的二次断言：条目路径不得越界（renderPackage 已保证，此处防御未来调用方）。
export function assertSafeZipPath(path: string): void {
  if (!path) throw new Error('zip 条目路径不能为空');
  if (path.startsWith('/') || path.includes('\\') || CONTROL_CHARS.test(path)) {
    throw new Error(`zip 条目路径非法：「${path.slice(0, 80)}」（禁止绝对路径、反斜杠与控制字符）`);
  }
  for (const seg of path.split('/')) {
    if (!seg || seg === '.' || seg === '..') {
      throw new Error(`zip 条目路径非法：「${path.slice(0, 80)}」（禁止空路径段、. 与 ..，防路径穿越）`);
    }
  }
}
