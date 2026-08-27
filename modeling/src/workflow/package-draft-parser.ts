import type {
  PackageSpec,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRule,
} from '../types.js';
import { renderPackage } from './package-render.js';
import { normalizeFieldAlias } from './field-alias.js';

type FrontmatterScalar = string;
type FrontmatterValue = FrontmatterScalar | FrontmatterValue[] | FrontmatterMap;
interface FrontmatterMap { [key: string]: FrontmatterValue }

interface ParsedMarkdown {
  frontmatter: FrontmatterMap;
  sections: Map<string, string[]>;
}

export class PackageDraftParseError extends Error {
  constructor(
    readonly path: string,
    message: string,
  ) {
    super(`${path}：${message}`);
    this.name = 'PackageDraftParseError';
  }
}

function fail(path: string, message: string): never {
  throw new PackageDraftParseError(path, message);
}

function parseScalar(path: string, raw: string): FrontmatterScalar {
  const value = raw.trim();
  if (value.startsWith('"')) {
    try {
      const parsed = JSON.parse(value);
      if (typeof parsed !== 'string') fail(path, `frontmatter 标量 ${value} 必须是字符串`);
      return parsed;
    } catch (error) {
      if (error instanceof PackageDraftParseError) throw error;
      fail(path, `frontmatter 字符串 ${value} 无法解析`);
    }
  }
  return value;
}

function indentation(path: string, line: string): number {
  const spaces = line.length - line.trimStart().length;
  if (spaces % 2 !== 0) fail(path, 'frontmatter 缩进必须使用两个空格');
  return spaces;
}

function parseFrontmatterMap(
  path: string,
  lines: string[],
  start: number,
  expectedIndent: number,
): { value: FrontmatterMap; next: number } {
  const value: FrontmatterMap = {};
  let index = start;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const currentIndent = indentation(path, line);
    if (currentIndent < expectedIndent) break;
    if (currentIndent > expectedIndent) fail(path, `frontmatter 第 ${index + 2} 行缩进层级不正确`);
    const trimmed = line.trim();
    if (trimmed.startsWith('- ')) fail(path, `frontmatter 第 ${index + 2} 行缺少字段名`);
    const match = trimmed.match(/^([A-Za-z_][\w-]*):(?:\s+(.*))?$/);
    if (!match) fail(path, `frontmatter 第 ${index + 2} 行格式无效`);
    const key = match[1];
    if (Object.hasOwn(value, key)) fail(path, `frontmatter 字段 ${key} 重复`);
    if (match[2] !== undefined) {
      value[key] = parseScalar(path, match[2]);
      index += 1;
      continue;
    }

    index += 1;
    while (index < lines.length && !lines[index].trim()) index += 1;
    if (index >= lines.length || indentation(path, lines[index]) <= expectedIndent) {
      fail(path, `frontmatter 字段 ${key} 缺少值`);
    }
    if (lines[index].trim().startsWith('- ')) {
      const items: FrontmatterValue[] = [];
      while (index < lines.length) {
        const itemLine = lines[index];
        if (!itemLine.trim()) {
          index += 1;
          continue;
        }
        const itemIndent = indentation(path, itemLine);
        if (itemIndent < expectedIndent + 2) break;
        if (itemIndent !== expectedIndent + 2 || !itemLine.trim().startsWith('- ')) {
          fail(path, `frontmatter 字段 ${key} 只支持标量列表`);
        }
        items.push(parseScalar(path, itemLine.trim().slice(2)));
        index += 1;
      }
      value[key] = items;
    } else {
      const nested = parseFrontmatterMap(path, lines, index, expectedIndent + 2);
      value[key] = nested.value;
      index = nested.next;
    }
  }
  return { value, next: index };
}

function parseMarkdown(path: string, content: string): ParsedMarkdown {
  const lines = content.replace(/\r\n?/g, '\n').split('\n');
  if (lines[0] !== '---') fail(path, '缺少 frontmatter 起始分隔符 ---');
  const end = lines.indexOf('---', 1);
  if (end < 0) fail(path, '缺少 frontmatter 结束分隔符 ---');
  const frontmatterLines = lines.slice(1, end);
  const parsed = parseFrontmatterMap(path, frontmatterLines, 0, 0);
  if (parsed.next !== frontmatterLines.length) fail(path, 'frontmatter 存在无法解析的内容');

  const sections = new Map<string, string[]>();
  let current: string | null = null;
  for (const line of lines.slice(end + 1)) {
    const heading = line.match(/^# ([^#].*)$/);
    if (heading) {
      current = heading[1].trim();
      if (sections.has(current)) fail(path, `章节「${current}」重复`);
      sections.set(current, []);
      continue;
    }
    if (!current) {
      if (line.trim()) fail(path, 'frontmatter 后、首个一级章节前存在无法识别的正文');
      continue;
    }
    sections.get(current)!.push(line);
  }
  return { frontmatter: parsed.value, sections };
}

function assertKeys(path: string, value: FrontmatterMap, allowed: string[], label: string): void {
  const allowedSet = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!allowedSet.has(key)) fail(path, `${label}包含不支持的字段 ${key}`);
  }
}

function asMap(path: string, value: FrontmatterValue | undefined, label: string): FrontmatterMap | undefined {
  if (value === undefined) return undefined;
  if (!value || Array.isArray(value) || typeof value !== 'object') fail(path, `${label} 必须是对象`);
  return value;
}

function asString(path: string, value: FrontmatterValue | undefined, label: string): string {
  if (typeof value !== 'string') fail(path, `${label} 必须是字符串`);
  return value;
}

function optionalString(path: string, value: FrontmatterValue | undefined, label: string): string | undefined {
  if (value === undefined) return undefined;
  return asString(path, value, label);
}

function asBoolean(path: string, value: FrontmatterValue | undefined, label: string): boolean {
  if (value === 'true') return true;
  if (value === 'false') return false;
  fail(path, `${label} 必须是 true 或 false`);
}

function stringList(path: string, value: FrontmatterValue | undefined, label: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    fail(path, `${label} 必须是字符串列表`);
  }
  return value as string[];
}

function assertResource(path: string, parsed: ParsedMarkdown, resource: string, key: string): void {
  const actualResource = asString(path, parsed.frontmatter.resource, 'frontmatter.resource');
  const actualKey = asString(path, parsed.frontmatter.key, 'frontmatter.key');
  if (actualResource !== resource) fail(path, `frontmatter.resource 必须保持为 ${resource}`);
  if (actualKey !== key) fail(path, `frontmatter.key 必须保持为 ${key}`);
}

function assertSections(path: string, sections: Map<string, string[]>, allowed: string[]): void {
  const allowedSet = new Set(allowed);
  for (const section of sections.keys()) {
    if (!allowedSet.has(section)) fail(path, `不支持章节「${section}」`);
  }
}

function assertReadonlyFrontmatter(
  path: string,
  parsed: ParsedMarkdown,
  baseParsed: ParsedMarkdown,
  key: string,
  label: string,
): void {
  if (JSON.stringify(parsed.frontmatter[key]) !== JSON.stringify(baseParsed.frontmatter[key])) {
    fail(path, `${label}是生成链路记录的只读字段，不能在详情文件中修改`);
  }
}

function assertReadonlySection(
  path: string,
  parsed: ParsedMarkdown,
  baseParsed: ParsedMarkdown,
  name: string,
): void {
  const content = parsed.sections.get(name)?.join('\n').trim() ?? '';
  const baseContent = baseParsed.sections.get(name)?.join('\n').trim() ?? '';
  if (content !== baseContent) fail(path, `章节「${name}」记录生成依据，不能在详情文件中修改`);
}

function sectionLines(path: string, parsed: ParsedMarkdown, name: string, required = true): string[] {
  const lines = parsed.sections.get(name);
  if (!lines) {
    if (required) fail(path, `缺少章节「${name}」`);
    return [];
  }
  return lines;
}

function sectionText(path: string, parsed: ParsedMarkdown, name: string, required = true): string {
  const text = sectionLines(path, parsed, name, required).join('\n').trim();
  if (required && !text) fail(path, `章节「${name}」不能为空`);
  return text;
}

function reconcileText(path: string, label: string, base: string, first: string, second: string): string {
  if (first === second) return first;
  if (first === base) return second;
  if (second === base) return first;
  fail(path, `${label}在 frontmatter 与正文中的修改不一致`);
}

function sameStrings(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function reconcileList(path: string, label: string, base: string[], first: string[], second: string[]): string[] {
  if (sameStrings(first, second)) return first;
  if (sameStrings(first, base)) return second;
  if (sameStrings(second, base)) return first;
  fail(path, `${label}在 frontmatter 与正文中的修改不一致`);
}

function markdownCells(path: string, line: string): string[] {
  const trimmed = line.trim();
  if (!trimmed.startsWith('|') || !trimmed.endsWith('|')) fail(path, `表格行格式无效：${trimmed}`);
  const cells: string[] = [];
  let current = '';
  const body = trimmed.slice(1, -1);
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index];
    if (char === '\\' && body[index + 1] === '|') {
      current += '|';
      index += 1;
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

function tableRows(path: string, lines: string[], headers: string[]): string[][] {
  const nonBlank = lines.filter((line) => line.trim());
  if (nonBlank.length < 2) fail(path, `表格缺少表头：${headers.join(' / ')}`);
  const actualHeaders = markdownCells(path, nonBlank[0]);
  if (!sameStrings(actualHeaders, headers)) fail(path, `表头必须为：${headers.join(' / ')}`);
  const separators = markdownCells(path, nonBlank[1]);
  if (separators.length !== headers.length || separators.some((cell) => !/^:?-{3,}:?$/.test(cell))) {
    fail(path, '表格分隔行格式无效');
  }
  return nonBlank.slice(2).map((line) => {
    const cells = markdownCells(path, line);
    if (cells.length !== headers.length) fail(path, `表格列数必须为 ${headers.length}`);
    return cells;
  });
}

function optionalCell(value: string): string | undefined {
  return value && value !== '未声明' && value !== '—' ? value : undefined;
}

function requiredFlag(path: string, value: string, allowUnspecified: boolean): boolean | undefined {
  if (value === '是') return true;
  if (value === '否') return false;
  if (allowUnspecified && value === '未声明') return undefined;
  fail(path, `必填列只允许「是」「否」${allowUnspecified ? '或「未声明」' : ''}`);
}

function bulletList(path: string, lines: string[], label: string): string[] {
  const nonBlank = lines.filter((line) => line.trim());
  return nonBlank.map((line) => {
    const match = line.trim().match(/^-\s+(.+)$/);
    if (!match) fail(path, `${label}必须使用 Markdown 无序列表`);
    return match[1].trim();
  });
}

function numberedList(path: string, lines: string[], label: string): string[] {
  const nonBlank = lines.filter((line) => line.trim());
  return nonBlank.map((line) => {
    const match = line.trim().match(/^\d+\.\s+(.+)$/);
    if (!match) fail(path, `${label}必须使用 Markdown 有序列表`);
    return match[1].trim();
  });
}

function parseObject(path: string, content: string, base: SpecObject, baseContent: string): SpecObject {
  const parsed = parseMarkdown(path, content);
  const baseParsed = parseMarkdown(path, baseContent);
  assertKeys(path, parsed.frontmatter, ['resource', 'key', 'title', 'description', 'aliases', 'mapping', 'sources'], '对象 frontmatter ');
  assertResource(path, parsed, 'object', base.key);
  assertSections(path, parsed.sections, ['业务定义', '字段', '关系', '查询边界']);
  if (JSON.stringify(parsed.frontmatter.mapping) !== JSON.stringify(baseParsed.frontmatter.mapping)) {
    fail(path, 'mapping 是由对象数据来源派生的只读字段，不能在详情文件中修改');
  }
  assertReadonlyFrontmatter(path, parsed, baseParsed, 'sources', 'frontmatter.sources');

  const frontmatterDescription = asString(path, parsed.frontmatter.description, 'frontmatter.description');
  const bodyDescription = sectionText(path, parsed, '业务定义');
  const fields = tableRows(path, sectionLines(path, parsed, '字段'), [
    '字段 key', '中文名', '类型', '必填', '业务说明', '别名',
  ]).map((row) => {
    if (!row[0] || row[0] === '—') fail(path, '字段 key 不能为空');
    const original = base.fields.find((field) => field.key === row[0]);
    return {
      key: row[0],
      cnName: optionalCell(row[1]),
      type: optionalCell(row[2]),
      required: requiredFlag(path, row[3], true),
      note: optionalCell(row[4]),
      alias: normalizeFieldAlias(row[5]),
      sourceColumn: original?.sourceColumn,
      valueBindingRef: original?.valueBindingRef,
    };
  });
  if (new Set(fields.map((field) => field.key)).size !== fields.length) fail(path, '字段 key 不能重复');

  const relationLines = sectionLines(path, parsed, '关系', false);
  const relations = relationLines.length ? tableRows(path, relationLines, ['关系', '目标对象', '说明', '证据']).map((row) => {
    if (!row[0] || !row[1]) fail(path, '关系和目标对象不能为空');
    const evidence = optionalCell(row[3])?.split('；').map((item) => item.trim()).filter(Boolean);
    return { relation: row[0], target: row[1], note: optionalCell(row[2]), evidence };
  }) : undefined;
  const queryBoundaryText = sectionText(path, parsed, '查询边界');
  const baseBoundaryText = sectionText(path, baseParsed, '查询边界');

  return {
    ...base,
    title: asString(path, parsed.frontmatter.title, 'frontmatter.title'),
    description: reconcileText(path, '对象描述', base.description, frontmatterDescription, bodyDescription),
    aliases: stringList(path, parsed.frontmatter.aliases, 'frontmatter.aliases'),
    fields,
    relations,
    queryBoundary: !base.queryBoundary && queryBoundaryText === baseBoundaryText ? undefined : queryBoundaryText,
  };
}

function parseOperation(path: string, content: string, base: SpecOperation, baseContent: string): SpecOperation {
  const parsed = parseMarkdown(path, content);
  const baseParsed = parseMarkdown(path, baseContent);
  assertKeys(path, parsed.frontmatter, ['resource', 'key', 'title', 'description', 'intent_examples', 'uses', 'script', 'evidence'], '操作 frontmatter ');
  assertResource(path, parsed, 'operation', base.key);
  assertSections(path, parsed.sections, ['业务目标', '输入', '输出', '流程', '规则、指标和注意事项', '知识证据']);
  assertReadonlyFrontmatter(path, parsed, baseParsed, 'evidence', 'frontmatter.evidence');
  assertReadonlySection(path, parsed, baseParsed, '知识证据');

  const uses = asMap(path, parsed.frontmatter.uses, 'frontmatter.uses');
  if (uses) assertKeys(path, uses, ['objects', 'rules', 'metrics'], 'frontmatter.uses ');
  const scriptMap = asMap(path, parsed.frontmatter.script, 'frontmatter.script');
  if (scriptMap) assertKeys(path, scriptMap, ['key', 'required'], 'frontmatter.script ');
  const examples = stringList(path, parsed.frontmatter.intent_examples, 'frontmatter.intent_examples');
  if (examples.length > 1) fail(path, 'frontmatter.intent_examples 最多保留一个胜任力问题');

  const inputs = tableRows(path, sectionLines(path, parsed, '输入'), [
    '参数 key', '中文名', '类型', '必填', '说明',
  ]).filter((row) => !row.every((cell) => cell === '—')).map((row) => {
    if (!row[0] || !row[2]) fail(path, '输入参数 key 和类型不能为空');
    return {
      param: row[0], cnName: optionalCell(row[1]), type: row[2],
      required: requiredFlag(path, row[3], false)!, note: optionalCell(row[4]),
    };
  });
  const outputs = tableRows(path, sectionLines(path, parsed, '输出'), [
    '输出 key', '中文名', '类型', '说明',
  ]).filter((row) => !row.every((cell) => cell === '—')).map((row) => {
    if (!row[0] || !row[2]) fail(path, '输出 key 和类型不能为空');
    return { field: row[0], cnName: optionalCell(row[1]), type: row[2], note: optionalCell(row[3]) };
  });
  const bodyItems = bulletList(path, sectionLines(path, parsed, '规则、指标和注意事项'), '规则、指标和注意事项');
  const effectiveItems = bodyItems.length === 1 && bodyItems[0] === '暂无' ? [] : bodyItems;
  const bodyRules: string[] = [];
  const bodyMetrics: string[] = [];
  const notes: string[] = [];
  for (const item of effectiveItems) {
    if (item.startsWith('相关规则：rule:')) bodyRules.push(item.slice('相关规则：rule:'.length));
    else if (item.startsWith('相关指标：metric:')) bodyMetrics.push(item.slice('相关指标：metric:'.length));
    else notes.push(item);
  }
  const frontmatterRules = stringList(path, uses?.rules, 'frontmatter.uses.rules');
  const frontmatterMetrics = stringList(path, uses?.metrics, 'frontmatter.uses.metrics');

  return {
    ...base,
    title: asString(path, parsed.frontmatter.title, 'frontmatter.title'),
    description: reconcileText(
      path,
      '操作描述',
      base.description,
      asString(path, parsed.frontmatter.description, 'frontmatter.description'),
      sectionText(path, parsed, '业务目标'),
    ),
    competencyQuestion: examples[0],
    relatedObjects: stringList(path, uses?.objects, 'frontmatter.uses.objects'),
    usesRules: reconcileList(path, '相关规则', base.usesRules ?? [], frontmatterRules, bodyRules),
    usesMetrics: reconcileList(path, '相关指标', base.usesMetrics ?? [], frontmatterMetrics, bodyMetrics),
    script: scriptMap ? {
      key: asString(path, scriptMap.key, 'frontmatter.script.key'),
      required: scriptMap.required === undefined ? false : asBoolean(path, scriptMap.required, 'frontmatter.script.required'),
    } : undefined,
    inputs,
    outputs,
    flow: numberedList(path, sectionLines(path, parsed, '流程'), '流程'),
    notes,
  };
}

function parseRule(path: string, content: string, base: SpecRule, baseContent: string): SpecRule {
  const parsed = parseMarkdown(path, content);
  const baseParsed = parseMarkdown(path, baseContent);
  assertKeys(path, parsed.frontmatter, ['resource', 'key', 'title', 'description', 'applies_to', 'evidence'], '规则 frontmatter ');
  assertResource(path, parsed, 'rule', base.key);
  assertSections(path, parsed.sections, ['规则内容', '判断条件', '例外', '知识证据']);
  assertReadonlyFrontmatter(path, parsed, baseParsed, 'evidence', 'frontmatter.evidence');
  assertReadonlySection(path, parsed, baseParsed, '知识证据');
  const appliesTo = asMap(path, parsed.frontmatter.applies_to, 'frontmatter.applies_to');
  if (appliesTo) assertKeys(path, appliesTo, ['objects', 'operations'], 'frontmatter.applies_to ');
  const conditionLines = sectionLines(path, parsed, '判断条件', false);
  const exceptionLines = sectionLines(path, parsed, '例外', false);
  return {
    ...base,
    title: asString(path, parsed.frontmatter.title, 'frontmatter.title'),
    description: optionalString(path, parsed.frontmatter.description, 'frontmatter.description'),
    appliesToObjects: stringList(path, appliesTo?.objects, 'frontmatter.applies_to.objects'),
    appliesToOperations: stringList(path, appliesTo?.operations, 'frontmatter.applies_to.operations'),
    content: sectionText(path, parsed, '规则内容'),
    conditions: conditionLines.length ? tableRows(path, conditionLines, ['条件', '结论']).map((row) => {
      if (!row[0] || !row[1]) fail(path, '判断条件和结论不能为空');
      return { condition: row[0], conclusion: row[1] };
    }) : undefined,
    exceptions: exceptionLines.length ? bulletList(path, exceptionLines, '例外') : undefined,
  };
}

function parseMetric(path: string, content: string, base: SpecMetric, baseContent: string): SpecMetric {
  const parsed = parseMarkdown(path, content);
  const baseParsed = parseMarkdown(path, baseContent);
  assertKeys(path, parsed.frontmatter, ['resource', 'key', 'title', 'description', 'unit', 'grain', 'uses', 'evidence'], '指标 frontmatter ');
  assertResource(path, parsed, 'metric', base.key);
  assertSections(path, parsed.sections, ['业务口径', '计算边界', '空值策略', '维度', '知识证据']);
  assertReadonlyFrontmatter(path, parsed, baseParsed, 'evidence', 'frontmatter.evidence');
  assertReadonlySection(path, parsed, baseParsed, '知识证据');
  const uses = asMap(path, parsed.frontmatter.uses, 'frontmatter.uses');
  if (uses) assertKeys(path, uses, ['objects'], 'frontmatter.uses ');
  const boundaryItems = bulletList(path, sectionLines(path, parsed, '计算边界'), '计算边界');
  const baseBoundaryItems = bulletList(path, sectionLines(path, baseParsed, '计算边界'), '计算边界');
  const boundaries = !base.boundaries.length && sameStrings(boundaryItems, baseBoundaryItems) ? [] : boundaryItems;
  const nullPolicyLines = sectionLines(path, parsed, '空值策略', false);
  const dimensionLines = sectionLines(path, parsed, '维度', false);
  return {
    ...base,
    title: asString(path, parsed.frontmatter.title, 'frontmatter.title'),
    description: optionalString(path, parsed.frontmatter.description, 'frontmatter.description'),
    unit: optionalString(path, parsed.frontmatter.unit, 'frontmatter.unit'),
    grain: optionalString(path, parsed.frontmatter.grain, 'frontmatter.grain'),
    usesObjects: stringList(path, uses?.objects, 'frontmatter.uses.objects'),
    caliber: sectionText(path, parsed, '业务口径'),
    boundaries,
    nullPolicy: nullPolicyLines.length ? tableRows(path, nullPolicyLines, ['字段', '处理']).map((row) => {
      if (!row[0] || !row[1]) fail(path, '空值策略的字段和处理不能为空');
      return { field: row[0], handling: row[1] };
    }) : undefined,
    dimensions: dimensionLines.length ? tableRows(path, dimensionLines, ['维度 key', '说明']).map((row) => {
      if (!row[0] || !row[1]) fail(path, '维度 key 和说明不能为空');
      return { key: row[0], note: row[1] };
    }) : undefined,
  };
}

export function applyPackageDraftFiles(
  baseSpec: PackageSpec,
  changedFiles: Array<{ path: string; content: string }>,
): PackageSpec {
  const baseFiles = renderPackage(baseSpec);
  let objects = [...baseSpec.objects];
  let operations = [...baseSpec.operations];
  let rules = [...(baseSpec.rules ?? [])];
  let metrics = [...(baseSpec.metrics ?? [])];

  for (const file of changedFiles) {
    const match = file.path.match(/^(objects|operations|rules|metrics)\/([^/]+)\.md$/);
    if (!match || match[2] === 'index') {
      fail(file.path, '该文件是派生文件，不能作为人工草稿发布；请修改对象、操作、规则或指标详情文件');
    }
    const [, kind, key] = match;
    const baseContent = baseFiles[file.path];
    if (baseContent === undefined) fail(file.path, '文件不存在于草稿基线版本');
    if (kind === 'objects') {
      const index = objects.findIndex((item) => item.key === key);
      if (index < 0) fail(file.path, `对象 ${key} 不存在于草稿基线版本`);
      objects[index] = parseObject(file.path, file.content, objects[index], baseContent);
    } else if (kind === 'operations') {
      const index = operations.findIndex((item) => item.key === key);
      if (index < 0) fail(file.path, `操作 ${key} 不存在于草稿基线版本`);
      operations[index] = parseOperation(file.path, file.content, operations[index], baseContent);
    } else if (kind === 'rules') {
      const index = rules.findIndex((item) => item.key === key);
      if (index < 0) fail(file.path, `规则 ${key} 不存在于草稿基线版本`);
      rules[index] = parseRule(file.path, file.content, rules[index], baseContent);
    } else {
      const index = metrics.findIndex((item) => item.key === key);
      if (index < 0) fail(file.path, `指标 ${key} 不存在于草稿基线版本`);
      metrics[index] = parseMetric(file.path, file.content, metrics[index], baseContent);
    }
  }

  return {
    ...baseSpec,
    objects,
    operations,
    rules,
    metrics,
  };
}
