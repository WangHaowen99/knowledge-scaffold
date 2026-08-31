import { unzipSync } from 'fflate';
import { parse as parseYaml } from 'yaml';

import type {
  CoverageResult,
  DocumentSourceRef,
  PackageSpec,
  SourceRef,
  SpecField,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRule,
  SpecScript,
} from '../types.js';
import {
  PACKAGE_STATUSES,
  packageManifestIssues,
  parsePackageManifest,
} from './package-contract.js';
import { normalizeFieldAlias } from './field-alias.js';
import { parseObjectIndex, type ObjectIndexEntry } from './object-index.js';
import { canonicalizePackageSpec } from './package-spec.js';

const MAX_ZIP_BYTES = 50 * 1024 * 1024;
const MAX_ARCHIVE_BYTES = 64 * 1024 * 1024;
const MAX_ARCHIVE_FILES = 2_000;
const MODEL_SPEC_PATH = 'model/package-spec.json';
const MODEL_COVERAGE_PATH = 'model/coverage.json';

export interface ImportedPackageArchive {
  spec: PackageSpec;
  coverage: CoverageResult;
  /** 可供检查和编辑的 UTF-8 文本文件。 */
  files: Record<string, string>;
  /** 原始包文件；二进制资产必须按字节保留。 */
  rawFiles: Record<string, Uint8Array>;
  warnings: string[];
}

type Frontmatter = Record<string, any>;

const CJK_TEXT = /[\u3400-\u9fff]/u;

export function decodeLegacyZipEntryName(value: string): string {
  const name = String(value ?? '');
  if (!/[^\u0000-\u007f]/u.test(name)) return name;
  const bytes: number[] = [];
  for (const character of name) {
    const code = character.codePointAt(0) ?? 0;
    if (code > 0xff) return name;
    bytes.push(code);
  }
  try {
    const decoded = new TextDecoder('gb18030', { fatal: true }).decode(Uint8Array.from(bytes));
    return CJK_TEXT.test(decoded) ? decoded : name;
  } catch {
    return name;
  }
}

function safeArchivePath(value: string): string {
  const normalized = String(value || '').replaceAll('\\', '/').replace(/^\.\//, '');
  const parts = normalized.split('/');
  if (
    !normalized
    || normalized.startsWith('/')
    || /^[A-Za-z]:\//u.test(normalized)
    || normalized.includes('\0')
    || parts.some((part) => !part || part === '.' || part === '..')
  ) {
    throw new Error(`知识包 ZIP 路径不安全：${value || '空路径'}`);
  }
  return parts.join('/');
}

function text(bytes: Uint8Array, path: string): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw new Error(`知识包文件不是 UTF-8 文本：${path}`);
  }
}

const BINARY_EXTENSIONS = new Set([
  '.bmp', '.doc', '.docx', '.gif', '.gz', '.ico', '.jpeg', '.jpg', '.pdf', '.png',
  '.tar', '.webp', '.xls', '.xlsx', '.zip',
]);

function optionalText(bytes: Uint8Array, path: string): string | null {
  const dot = path.lastIndexOf('.');
  const suffix = dot >= 0 ? path.slice(dot).toLowerCase() : '';
  if (BINARY_EXTENSIONS.has(suffix)) return null;
  try {
    const decoded = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(decoded) ? null : decoded;
  } catch {
    return null;
  }
}

function archiveFiles(bytes: Uint8Array): {
  files: Record<string, string>;
  rawFiles: Record<string, Uint8Array>;
} {
  if (!(bytes instanceof Uint8Array) || !bytes.length) throw new Error('知识包 ZIP 为空');
  if (bytes.byteLength > MAX_ZIP_BYTES) throw new Error('知识包 ZIP 超过 50 MB');

  let entries: Record<string, Uint8Array>;
  let declaredFileCount = 0;
  let declaredTotalBytes = 0;
  const declaredPaths = new Set<string>();
  try {
    entries = unzipSync(bytes, {
      filter(file) {
        const rawName = decodeLegacyZipEntryName(String(file.name ?? '')).replaceAll('\\', '/');
        const directory = rawName.endsWith('/');
        const path = safeArchivePath(directory ? rawName.slice(0, -1) : rawName);
        if (directory) return false;

        declaredFileCount += 1;
        if (declaredFileCount > MAX_ARCHIVE_FILES) throw new Error('知识包 ZIP 文件数量过多');
        if (!Number.isSafeInteger(file.originalSize) || file.originalSize < 0) {
          throw new Error(`知识包 ZIP 文件大小不正确：${path}`);
        }
        declaredTotalBytes += file.originalSize;
        if (declaredTotalBytes > MAX_ARCHIVE_BYTES) throw new Error('知识包 ZIP 解压后超过 64 MB');
        if (declaredPaths.has(path)) throw new Error(`知识包 ZIP 包含重复文件：${path}`);
        declaredPaths.add(path);
        return !(path.startsWith('__MACOSX/') || path.endsWith('/.DS_Store') || path === '.DS_Store');
      },
    });
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('知识包 ZIP')) throw error;
    throw new Error('知识包 ZIP 无法解压');
  }
  const rawFiles = Object.entries(entries).filter(([path]) => !path.endsWith('/'));
  if (!rawFiles.length) throw new Error('知识包 ZIP 中没有文件');
  if (rawFiles.length > MAX_ARCHIVE_FILES) throw new Error('知识包 ZIP 文件数量过多');

  let total = 0;
  const normalized = new Map<string, Uint8Array>();
  for (const [rawPath, content] of rawFiles) {
    const path = safeArchivePath(decodeLegacyZipEntryName(rawPath));
    if (path.startsWith('__MACOSX/') || path.endsWith('/.DS_Store')) continue;
    total += content.byteLength;
    if (total > MAX_ARCHIVE_BYTES) throw new Error('知识包 ZIP 解压后超过 64 MB');
    if (normalized.has(path)) throw new Error(`知识包 ZIP 包含重复文件：${path}`);
    normalized.set(path, content);
  }

  const manifests = [...normalized.keys()]
    .filter((path) => path === 'manifest.yaml' || path.endsWith('/manifest.yaml'))
    .sort((left, right) => left.split('/').length - right.split('/').length || left.localeCompare(right));
  if (!manifests.length) throw new Error('知识包 ZIP 缺少 manifest.yaml');
  const depth = manifests[0].split('/').length;
  const shallowest = manifests.filter((path) => path.split('/').length === depth);
  if (shallowest.length > 1) throw new Error('知识包 ZIP 中包含多个知识包根目录');
  const root = manifests[0] === 'manifest.yaml' ? '' : manifests[0].slice(0, -'manifest.yaml'.length);

  const files: Record<string, string> = {};
  const packageBytes: Record<string, Uint8Array> = {};
  for (const [path, content] of normalized) {
    if (root && !path.startsWith(root)) continue;
    const relative = root ? path.slice(root.length) : path;
    if (!relative) continue;
    packageBytes[relative] = new Uint8Array(content);
    const decoded = optionalText(content, relative);
    if (decoded !== null) files[relative] = decoded;
  }
  if (!files['manifest.yaml']) text(packageBytes['manifest.yaml'], 'manifest.yaml');
  return { files, rawFiles: packageBytes };
}

function parseJson<T>(value: string | undefined, label: string): T | undefined {
  if (value === undefined) return undefined;
  try {
    return JSON.parse(value) as T;
  } catch {
    throw new Error(`${label} 不是合法 JSON`);
  }
}

function frontmatter(markdown: string, path: string): { data: Frontmatter; body: string } {
  const normalized = markdown.replaceAll('\r\n', '\n');
  const match = normalized.match(/^---\n([\s\S]*?)\n---\s*\n?/);
  if (!match) throw new Error(`${path} 缺少 frontmatter`);
  try {
    const data = parseYaml(match[1]) as Frontmatter;
    return { data: data && typeof data === 'object' ? data : {}, body: normalized.slice(match[0].length) };
  } catch {
    throw new Error(`${path} 的 frontmatter 不是合法 YAML`);
  }
}

function section(markdown: string, heading: string): string {
  const lines = markdown.replaceAll('\r\n', '\n').split('\n');
  const start = lines.findIndex((line) => line.trim() === `# ${heading}`);
  if (start < 0) return '';
  const result: string[] = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    if (/^#\s+/.test(lines[index])) break;
    result.push(lines[index]);
  }
  return result.join('\n').trim();
}

function paragraph(value: string): string {
  return value.split(/\n\s*\n/).map((item) => item.trim()).find(Boolean) ?? '';
}

function listItems(value: string): string[] {
  return value.split('\n')
    .map((line) => line.trim().match(/^[-*]\s+(.+)$/)?.[1]?.trim())
    .filter((item): item is string => Boolean(item));
}

function numberedItems(value: string): string[] {
  return value.split('\n')
    .map((line) => line.trim().match(/^\d+[.)]\s+(.+)$/)?.[1]?.trim())
    .filter((item): item is string => Boolean(item));
}

function splitTableRow(line: string): string[] {
  const source = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells: string[] = [];
  let current = '';
  let escaped = false;
  for (const char of source) {
    if (escaped) {
      current += char;
      escaped = false;
    } else if (char === '\\') {
      escaped = true;
    } else if (char === '|') {
      cells.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current.trim());
  return cells;
}

function tableRows(value: string): string[][] {
  return markdownTable(value).rows;
}

function markdownTable(value: string): { headers: string[]; rows: string[][] } {
  const lines = value.split('\n').map((line) => line.trim()).filter(Boolean);
  const header = lines.findIndex((line) => line.startsWith('|'));
  if (header < 0 || !/^\|?\s*:?-{3}/.test(lines[header + 1] ?? '')) {
    return { headers: [], rows: [] };
  }
  return {
    headers: splitTableRow(lines[header]),
    rows: lines.slice(header + 2).filter((line) => line.startsWith('|')).map(splitTableRow),
  };
}

function tableCell(headers: string[], row: string[], ...names: string[]): string {
  const normalized = headers.map((header) => header.replaceAll(/\s+/g, '').toLowerCase());
  for (const name of names) {
    const index = normalized.indexOf(name.replaceAll(/\s+/g, '').toLowerCase());
    if (index >= 0) return row[index] ?? '';
  }
  return '';
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? '').trim()).filter(Boolean);
}

function parseSource(value: string): SourceRef | null {
  if (value.startsWith('表格：')) return { kind: 'table', table: value.slice('表格：'.length).trim() };
  if (!value.startsWith('文档：')) return null;
  const [filename, heading] = value.slice('文档：'.length).split(' / ', 2).map((item) => item.trim());
  const source: DocumentSourceRef = { kind: 'document', filename };
  if (heading) source.anchors = [{ heading }];
  return source;
}

function required(value: string): boolean {
  return ['是', 'true', 'yes', 'required'].includes(value.trim().toLowerCase());
}

function optionalString(value: unknown): string | undefined {
  const text = String(value ?? '').trim();
  return text || undefined;
}

function parseObject(
  path: string,
  markdown: string,
  mapping: any,
  objectIndex: Map<string, ObjectIndexEntry> = new Map(),
): SpecObject {
  const { data, body } = frontmatter(markdown, path);
  const key = String(data.key ?? path.split('/').at(-1)?.replace(/\.md$/, '') ?? '').trim();
  const objectMapping = mapping?.objects?.[key];
  const fields: SpecField[] = tableRows(section(body, '字段')).map((row) => ({
    key: row[0] ?? '', cnName: row[1] || undefined, type: row[2] || undefined,
    required: required(row[3] ?? ''), note: row[4] || undefined, alias: normalizeFieldAlias(row[5]),
    ...(optionalString(objectMapping?.fields?.[row[0] ?? '']?.source_column)
      ? { sourceColumn: optionalString(objectMapping?.fields?.[row[0] ?? '']?.source_column) }
      : {}),
    valueBindingRef: optionalString(objectMapping?.fields?.[row[0] ?? '']?.value_binding_ref),
  })).filter((field) => field.key && field.key !== '—');
  const relations = tableRows(section(body, '关系')).map((row) => ({
    relation: row[0] ?? '', target: row[1] ?? '', note: row[2] || undefined,
    ...(row[3] ? { evidence: row[3].split('；').map((item) => item.trim()).filter(Boolean) } : {}),
  })).filter((item) => item.relation && item.target);
  const sourceRefs = strings(data.sources).map(parseSource).filter((item): item is SourceRef => Boolean(item));
  const indexedSourceSheet = objectIndex.get(key)?.sourceSheet;
  const fallbackSourceRefs = sourceRefs.length || !indexedSourceSheet || indexedSourceSheet === '—'
    ? []
    : [{ kind: 'table' as const, table: indexedSourceSheet }];
  return {
    key,
    title: String(data.title ?? key),
    description: String(data.description ?? paragraph(section(body, '业务定义')) ?? ''),
    fields,
    ...(strings(data.aliases).length ? { aliases: strings(data.aliases) } : {}),
    ...(section(body, '查询边界') ? { queryBoundary: paragraph(section(body, '查询边界')) } : {}),
    ...(relations.length ? { relations } : {}),
    ...(sourceRefs.length || fallbackSourceRefs.length ? { sourceRefs: sourceRefs.length ? sourceRefs : fallbackSourceRefs } : {}),
    ...(Array.isArray(objectMapping?.primary_key) ? { primaryKey: strings(objectMapping.primary_key) } : {}),
  };
}

function parseValueBindings(mapping: any): PackageSpec['valueBindings'] {
  if (!mapping?.value_bindings || typeof mapping.value_bindings !== 'object' || Array.isArray(mapping.value_bindings)) {
    return undefined;
  }
  const bindings = Object.entries<any>(mapping.value_bindings).map(([key, value]) => ({
    key,
    resource: String(value?.resource ?? ''),
    valueField: String(value?.value_field ?? ''),
    semanticField: String(value?.semantic_field ?? ''),
    ...(value?.provider_alias ? { providerAlias: String(value.provider_alias) } : {}),
    ...(Array.isArray(value?.alias_fields)
      ? { aliasFields: value.alias_fields.map((item: unknown) => String(item ?? '')).filter(Boolean) }
      : {}),
  })).filter((binding) => binding.key && binding.resource && binding.valueField && binding.semanticField);
  return bindings.length ? bindings : undefined;
}

function parseOperation(path: string, markdown: string): SpecOperation {
  const { data, body } = frontmatter(markdown, path);
  const inputs = tableRows(section(body, '输入')).map((row) => ({
    param: row[0] ?? '', cnName: row[1] || undefined, type: row[2] || 'string',
    required: required(row[3] ?? ''), note: row[4] || undefined,
  })).filter((item) => item.param && item.param !== '—');
  const outputs = tableRows(section(body, '输出')).map((row) => ({
    field: row[0] ?? '', cnName: row[1] || undefined, type: row[2] || 'string', note: row[3] || undefined,
  })).filter((item) => item.field && item.field !== '—');
  const notes = listItems(section(body, '规则、指标和注意事项'))
    .filter((item) => !item.startsWith('相关规则：') && !item.startsWith('相关指标：') && item !== '暂无');
  const intent = strings(data.intent_examples)[0];
  const evidence = strings(data.evidence).length ? strings(data.evidence) : listItems(section(body, '知识证据'));
  return {
    key: String(data.key ?? path.split('/').at(-1)?.replace(/\.md$/, '') ?? ''),
    title: String(data.title ?? ''),
    description: String(data.description ?? paragraph(section(body, '业务目标')) ?? ''),
    inputs,
    outputs,
    flow: numberedItems(section(body, '流程')),
    relatedObjects: strings(data.uses?.objects),
    ...(strings(data.uses?.rules).length ? { usesRules: strings(data.uses.rules) } : {}),
    ...(strings(data.uses?.metrics).length ? { usesMetrics: strings(data.uses.metrics) } : {}),
    ...(notes.length ? { notes } : {}),
    ...(data.script?.key ? { script: { key: String(data.script.key), required: Boolean(data.script.required) } } : {}),
    ...(intent ? { competencyQuestion: intent } : {}),
    ...(evidence.length ? { evidence } : {}),
  };
}

function parseRule(path: string, markdown: string): SpecRule {
  const { data, body } = frontmatter(markdown, path);
  const conditions = tableRows(section(body, '判断条件')).map((row) => ({
    condition: row[0] ?? '', conclusion: row[1] ?? '',
  })).filter((item) => item.condition && item.conclusion);
  const exceptions = listItems(section(body, '例外'));
  const evidence = strings(data.evidence).length ? strings(data.evidence) : listItems(section(body, '知识证据'));
  return {
    key: String(data.key ?? path.split('/').at(-1)?.replace(/\.md$/, '') ?? ''),
    title: String(data.title ?? ''),
    description: data.description == null ? undefined : String(data.description),
    content: section(body, '规则内容'),
    ...(strings(data.applies_to?.objects).length ? { appliesToObjects: strings(data.applies_to.objects) } : {}),
    ...(strings(data.applies_to?.operations).length
      ? { appliesToOperations: strings(data.applies_to.operations) }
      : {}),
    ...(conditions.length ? { conditions } : {}),
    ...(exceptions.length ? { exceptions } : {}),
    ...(evidence.length ? { evidence } : {}),
  };
}

function parseMetric(path: string, markdown: string): SpecMetric {
  const { data, body } = frontmatter(markdown, path);
  const nullPolicy = tableRows(section(body, '空值策略')).map((row) => ({ field: row[0] ?? '', handling: row[1] ?? '' }))
    .filter((item) => item.field && item.handling);
  const dimensions = tableRows(section(body, '维度')).map((row) => ({ key: row[0] ?? '', note: row[1] ?? '' }))
    .filter((item) => item.key);
  const evidence = strings(data.evidence).length ? strings(data.evidence) : listItems(section(body, '知识证据'));
  return {
    key: String(data.key ?? path.split('/').at(-1)?.replace(/\.md$/, '') ?? ''),
    title: String(data.title ?? ''),
    description: data.description == null ? undefined : String(data.description),
    unit: data.unit == null ? undefined : String(data.unit),
    grain: data.grain == null ? undefined : String(data.grain),
    caliber: section(body, '业务口径'),
    boundaries: listItems(section(body, '计算边界')),
    ...(strings(data.uses?.objects).length ? { usesObjects: strings(data.uses.objects) } : {}),
    ...(nullPolicy.length ? { nullPolicy } : {}),
    ...(dimensions.length ? { dimensions } : {}),
    ...(evidence.length ? { evidence } : {}),
  };
}

export function parseRenderedScriptFile(
  path: string,
  markdown: string,
  files: Record<string, string>,
): SpecScript {
  const { data, body } = frontmatter(markdown, path);
  const inputSection = section(body, '输入参数');
  const inputTable = markdownTable(inputSection);
  const parameters = inputTable.rows.map((row, index) => {
    const name = tableCell(inputTable.headers, row, '参数 key', '参数key', '参数名称', 'name');
    const rawAction = tableCell(inputTable.headers, row, 'Action', 'action').trim().toLowerCase();
    if (rawAction && rawAction !== '—' && !['store_true', 'store_false'].includes(rawAction)) {
      throw new Error(`${path} 的第 ${index + 1} 个脚本参数 Action 不正确：${rawAction}`);
    }
    const action: SpecScript['parameters'][number]['action'] =
      rawAction === 'store_true' || rawAction === 'store_false' ? rawAction : undefined;
    const type = tableCell(inputTable.headers, row, '类型', 'type') || 'string';
    const isRequired = required(tableCell(inputTable.headers, row, '必填', 'required'));
    const multiple = required(tableCell(inputTable.headers, row, '是否多值', 'multiple'));
    const defaultText = tableCell(inputTable.headers, row, '默认值', '默认', 'default');
    const parsedDefault = defaultText && defaultText !== '—'
      ? parseParameterDefault(defaultText)
      : undefined;
    if (action && !['bool', 'boolean'].includes(type.toLowerCase())) {
      throw new Error(`${path} 的第 ${index + 1} 个脚本参数 Action 只适用于 boolean`);
    }
    if (action && isRequired) {
      throw new Error(`${path} 的第 ${index + 1} 个脚本参数使用 ${action} 时不能设为必填`);
    }
    if (action && multiple) {
      throw new Error(`${path} 的第 ${index + 1} 个脚本参数使用 ${action} 时不能设为多值`);
    }
    const actionDefault = action === 'store_true' ? false : action === 'store_false' ? true : undefined;
    if (actionDefault !== undefined && parsedDefault !== undefined && parsedDefault !== actionDefault) {
      throw new Error(`${path} 的第 ${index + 1} 个脚本参数默认值与 ${action} 不一致`);
    }
    const defaultValue = parsedDefault ?? actionDefault;
    return {
      name,
      flag: tableCell(inputTable.headers, row, 'CLI 参数', 'CLI参数', 'flag'),
      type,
      required: isRequired,
      ...(defaultValue !== undefined ? { defaultValue } : {}),
      ...(tableCell(inputTable.headers, row, '说明', '描述', 'description')
        ? { description: tableCell(inputTable.headers, row, '说明', '描述', 'description') }
        : {}),
      ...(multiple ? { multiple: true } : {}),
      ...(action ? { action } : {}),
    };
  }).filter((item) => item.name && item.name !== '—');
  const outputs = tableRows(section(body, '输出')).map((row) => ({
    key: row[0] ?? '', type: row[1] || 'string', description: row[2] || undefined,
  })).filter((item) => item.key && item.key !== '—');
  const entrypoint = String(data.entrypoint ?? '');
  const evidence = strings(data.evidence).length ? strings(data.evidence) : listItems(section(body, '知识证据'));
  return {
    key: String(data.key ?? path.split('/').at(-1)?.replace(/\.md$/, '') ?? ''),
    title: String(data.title ?? ''),
    description: String(data.description ?? ''),
    boundOperation: String(data.bound_operation ?? ''),
    runtime: 'python',
    entrypoint,
    timeoutSeconds: Number(data.timeout_seconds ?? 30),
    parameters,
    outputs,
    boundaries: listItems(section(body, '执行边界')),
    ...(evidence.length ? { evidence } : {}),
    sourceCode: files[`scripts/${entrypoint}`] ?? '',
  };
}

function parseParameterDefault(value: string): string | number | boolean {
  const text = String(value ?? '').trim();
  if (text === 'true') return true;
  if (text === 'false') return false;
  if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(text)) return Number(text);
  return text;
}

function parseRenderedSpec(
  files: Record<string, string>,
  options: { allowMissingManifestStatus?: boolean } = {},
): PackageSpec {
  const manifest = parsePackageManifest(files['manifest.yaml']);
  const manifestIssues = packageManifestIssues(manifest).filter((issue) => (
    !options.allowMissingManifestStatus || issue.ruleId !== 'manifest.required.status'
  ));
  if (manifestIssues.length) throw new Error(manifestIssues.map((issue) => issue.message).join('；'));
  const mapping = parseJson<any>(files['mappings/object-data-mappings.json'], '数据映射') ?? {};
  const objectIndex = files['objects/index.md'] ? parseObjectIndex(files['objects/index.md']) : new Map();
  const detailPaths = (folder: string) => Object.keys(files)
    .filter((path) => path.startsWith(`${folder}/`) && path.endsWith('.md') && path !== `${folder}/index.md`)
    .sort();
  const objects = detailPaths('objects').map((path) => parseObject(path, files[path], mapping, objectIndex));
  const operations = detailPaths('operations').map((path) => parseOperation(path, files[path]));
  const rules = detailPaths('rules').map((path) => parseRule(path, files[path]));
  const metrics = detailPaths('metrics').map((path) => parseMetric(path, files[path]));
  const scripts = detailPaths('scripts').map((path) => parseRenderedScriptFile(path, files[path], files));
  const valueBindings = parseValueBindings(mapping);
  return {
    id: String(manifest.id ?? '').trim(),
    name: String(manifest.name ?? manifest.id ?? '').trim(),
    description: String(manifest.description ?? '').trim(),
    version: String(manifest.version ?? '').trim(),
    language: String(manifest.language ?? 'zh-CN').trim(),
    status: PACKAGE_STATUSES.includes(String(manifest.status) as typeof PACKAGE_STATUSES[number])
      ? manifest.status as PackageSpec['status']
      : 'draft',
    defaultProviderAlias: String(mapping.default_provider_alias ?? 'default'),
    objects,
    operations,
    ...(rules.length ? { rules } : {}),
    ...(metrics.length ? { metrics } : {}),
    ...(scripts.length ? { scripts } : {}),
    ...(valueBindings ? { valueBindings } : {}),
  };
}

/**
 * 从标准知识包目录重建结构化规格。Studio 与 ZIP 导入共用这一解析入口，
 * 避免两套 Markdown 解释规则逐渐分叉。
 */
export function parseRenderedPackageFiles(files: Record<string, string>): PackageSpec {
  return canonicalizePackageSpec(parseRenderedSpec(files));
}

function inferredCoverage(spec: PackageSpec): CoverageResult {
  return {
    covered: spec.operations
      .filter((operation) => operation.competencyQuestion)
      .map((operation) => ({ question: operation.competencyQuestion!, operation: operation.key })),
    uncovered: [],
  };
}

export function parsePackageArchive(bytes: Uint8Array): ImportedPackageArchive {
  const archive = archiveFiles(bytes);
  const allFiles = archive.files;
  const embeddedSpec = parseJson<PackageSpec>(allFiles[MODEL_SPEC_PATH], '结构化知识包规格');
  const embeddedCoverage = parseJson<CoverageResult>(allFiles[MODEL_COVERAGE_PATH], '知识包覆盖结果');
  const files = Object.fromEntries(Object.entries(allFiles).filter(([path]) => !path.startsWith('model/')));
  const rawFiles = Object.fromEntries(Object.entries(archive.rawFiles).filter(([path]) => !path.startsWith('model/')));
  const manifest = parsePackageManifest(files['manifest.yaml']);
  const missingManifestStatus = manifest.status === undefined
    || manifest.status === null
    || String(manifest.status).trim() === '';
  const manifestIssues = packageManifestIssues(manifest).filter((issue) => (
    !missingManifestStatus || issue.ruleId !== 'manifest.required.status'
  ));
  if (manifestIssues.length) throw new Error(manifestIssues.map((issue) => issue.message).join('；'));
  const spec = canonicalizePackageSpec(embeddedSpec ?? parseRenderedSpec(files, {
    allowMissingManifestStatus: missingManifestStatus,
  }));
  if (missingManifestStatus) spec.status = 'draft';
  if (!spec.id || !spec.name || !spec.version || !spec.objects || !spec.operations) {
    throw new Error('知识包缺少 id、name、version、objects 或 operations');
  }
  return {
    spec,
    coverage: embeddedCoverage ?? inferredCoverage(spec),
    files,
    rawFiles,
    warnings: [
      ...(missingManifestStatus ? ['来源包 manifest 未声明 status，已按 draft 兼容导入。'] : []),
      ...(embeddedSpec ? [] : ['该知识包未携带结构化规格，已从标准目录和 Markdown 内容重建。']),
      ...(Object.keys(rawFiles).length > Object.keys(files).length
        ? ['包内二进制附件已原样保留，可随知识包导出，但不支持在线编辑。'] : []),
    ],
  };
}
