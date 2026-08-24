import { parse as parseYaml } from 'yaml';

type KnowledgeKind = 'object' | 'operation' | 'rule' | 'metric';

interface ParsedMarkdown {
  frontmatter: Record<string, unknown>;
  sections: Map<string, string>;
}

const DETAIL_CONFIG: Record<KnowledgeKind, {
  directory: string;
  allowedKeys: string[];
  requiredKeys: string[];
  allowedSections: string[];
  requiredSections: string[];
}> = {
  object: {
    directory: 'objects',
    allowedKeys: ['resource', 'key', 'title', 'description', 'aliases', 'mapping', 'sources'],
    requiredKeys: ['resource', 'key', 'title', 'description'],
    allowedSections: ['业务定义', '字段', '关系', '查询边界'],
    requiredSections: ['业务定义', '字段', '查询边界'],
  },
  operation: {
    directory: 'operations',
    allowedKeys: ['resource', 'key', 'title', 'description', 'intent_examples', 'uses', 'script', 'evidence'],
    requiredKeys: ['resource', 'key', 'title', 'description', 'evidence'],
    allowedSections: ['业务目标', '输入', '输出', '流程', '规则、指标和注意事项', '知识证据'],
    requiredSections: ['业务目标', '输入', '输出', '流程', '规则、指标和注意事项', '知识证据'],
  },
  rule: {
    directory: 'rules',
    allowedKeys: ['resource', 'key', 'title', 'description', 'applies_to', 'evidence'],
    requiredKeys: ['resource', 'key', 'title', 'evidence'],
    allowedSections: ['规则内容', '判断条件', '例外', '知识证据'],
    requiredSections: ['规则内容', '知识证据'],
  },
  metric: {
    directory: 'metrics',
    allowedKeys: ['resource', 'key', 'title', 'description', 'unit', 'grain', 'uses', 'evidence'],
    requiredKeys: ['resource', 'key', 'title', 'evidence'],
    allowedSections: ['业务口径', '计算边界', '空值策略', '维度', '知识证据'],
    requiredSections: ['业务口径', '计算边界', '知识证据'],
  },
};

const INDEX_CONFIG = {
  object: { path: 'objects/index.md', key: 'objects', title: '对象目录', header: ['object key', '标题', '说明', '来源Sheet'] },
  operation: { path: 'operations/index.md', key: 'operations', title: '操作目录', header: ['operation key', '标题', '输入', '输出'] },
  rule: { path: 'rules/index.md', key: 'rules', title: '规则目录', header: ['rule key', '标题', '说明'] },
  metric: { path: 'metrics/index.md', key: 'metrics', title: '指标目录', header: ['metric key', '标题', '说明'] },
} satisfies Record<KnowledgeKind, { path: string; key: string; title: string; header: string[] }>;

export function validateKnowledgeMarkdownContract(files: Record<string, string>): string[] {
  const issues: string[] = [];
  const parsedByPath = new Map<string, ParsedMarkdown>();
  const keys = new Map<KnowledgeKind, Set<string>>(
    (Object.keys(DETAIL_CONFIG) as KnowledgeKind[]).map((kind) => [kind, new Set()]),
  );

  for (const kind of Object.keys(DETAIL_CONFIG) as KnowledgeKind[]) {
    const config = DETAIL_CONFIG[kind];
    const paths = Object.keys(files).filter((path) => (
      path.startsWith(`${config.directory}/`) && path.endsWith('.md') && path !== `${config.directory}/index.md`
    )).sort();
    for (const path of paths) {
      const parsed = parseMarkdown(path, files[path], issues);
      if (!parsed) continue;
      parsedByPath.set(path, parsed);
      const expectedKey = path.slice(config.directory.length + 1, -3);
      keys.get(kind)!.add(expectedKey);
      validateDetail(path, kind, expectedKey, parsed, issues);
    }
  }

  for (const kind of Object.keys(INDEX_CONFIG) as KnowledgeKind[]) {
    validateIndex(kind, files, keys.get(kind)!, issues);
  }
  validateReferences(parsedByPath, keys, issues);
  return issues;
}

function parseMarkdown(path: string, content: string, issues: string[]): ParsedMarkdown | null {
  const normalized = String(content ?? '').replaceAll('\r\n', '\n');
  const match = normalized.match(/^---\n([\s\S]*?)\n---\s*\n?/);
  if (!match) {
    issues.push(`${path} 缺少 YAML frontmatter`);
    return null;
  }
  let frontmatter: unknown;
  try {
    frontmatter = parseYaml(match[1]);
  } catch {
    issues.push(`${path} 的 frontmatter 不是合法 YAML`);
    return null;
  }
  if (!isRecord(frontmatter)) {
    issues.push(`${path} 的 frontmatter 必须是对象`);
    return null;
  }
  const body = normalized.slice(match[0].length);
  const headings = [...body.matchAll(/^# (.+)$/gm)];
  const sections = new Map<string, string>();
  for (const [index, heading] of headings.entries()) {
    const name = heading[1].trim();
    if (sections.has(name)) issues.push(`${path} 重复声明章节 # ${name}`);
    const start = (heading.index ?? 0) + heading[0].length;
    const end = headings[index + 1]?.index ?? body.length;
    sections.set(name, body.slice(start, end).trim());
  }
  return { frontmatter, sections };
}

function validateDetail(
  path: string,
  kind: KnowledgeKind,
  expectedKey: string,
  parsed: ParsedMarkdown,
  issues: string[],
) {
  const config = DETAIL_CONFIG[kind];
  validateKeys(path, parsed.frontmatter, config.allowedKeys, config.requiredKeys, issues);
  if (parsed.frontmatter.resource !== kind) issues.push(`${path} 的 resource 必须是 ${kind}`);
  if (parsed.frontmatter.key !== expectedKey) issues.push(`${path} 的 key 必须与文件名 ${expectedKey} 一致`);
  validateSections(path, parsed.sections, config.allowedSections, config.requiredSections, issues);

  if (kind === 'object') {
    validateTable(path, parsed.sections.get('字段'), ['字段 key', '中文名', '类型', '必填', '业务说明', '别名'], issues);
    if (parsed.sections.has('关系')) validateTable(path, parsed.sections.get('关系'), ['关系', '目标对象', '说明', '证据'], issues);
    for (const source of stringList(path, parsed.frontmatter.sources, 'sources', issues)) {
      if (!/^(?:文档：.+|表格：.+)$/u.test(source)) {
        issues.push(`${path} 的 sources 只接受“文档：文件名 / 章节”或“表格：表名”：${source}`);
      }
    }
  }
  if (kind === 'operation') {
    validateTable(path, parsed.sections.get('输入'), ['参数 key', '中文名', '类型', '必填', '说明'], issues);
    validateTable(path, parsed.sections.get('输出'), ['输出 key', '中文名', '类型', '说明'], issues);
    if (!/^\d+[.)]\s+.+/mu.test(parsed.sections.get('流程') ?? '')) issues.push(`${path} 的流程必须使用编号列表`);
    validateNestedLists(path, parsed.frontmatter.uses, 'uses', ['objects', 'rules', 'metrics'], issues);
    validateOperationBodyReferences(path, parsed, issues);
  }
  if (kind === 'rule') {
    if (parsed.sections.has('判断条件')) validateTable(path, parsed.sections.get('判断条件'), ['条件', '结论'], issues);
    validateNestedLists(path, parsed.frontmatter.applies_to, 'applies_to', ['objects'], issues);
  }
  if (kind === 'metric') {
    if (!/^[-*]\s+.+/mu.test(parsed.sections.get('计算边界') ?? '')) issues.push(`${path} 的计算边界必须使用项目列表`);
    if (parsed.sections.has('空值策略')) validateTable(path, parsed.sections.get('空值策略'), ['字段', '处理'], issues);
    if (parsed.sections.has('维度')) validateTable(path, parsed.sections.get('维度'), ['维度 key', '说明'], issues);
    validateNestedLists(path, parsed.frontmatter.uses, 'uses', ['objects'], issues);
  }
  if (kind !== 'object') stringList(path, parsed.frontmatter.evidence, 'evidence', issues, true);
}

function validateIndex(kind: KnowledgeKind, files: Record<string, string>, detailKeys: Set<string>, issues: string[]) {
  const config = INDEX_CONFIG[kind];
  const content = files[config.path];
  if (!content) {
    if (detailKeys.size) issues.push(`${config.path} 缺失，无法索引 ${detailKeys.size} 个 ${kind} 详情`);
    return;
  }
  const parsed = parseMarkdown(config.path, content, issues);
  if (!parsed) return;
  validateKeys(config.path, parsed.frontmatter, ['resource', 'key', 'title'], ['resource', 'key', 'title'], issues);
  if (parsed.frontmatter.resource !== `${kind}_index`) issues.push(`${config.path} 的 resource 必须是 ${kind}_index`);
  if (parsed.frontmatter.key !== config.key) issues.push(`${config.path} 的 key 必须是 ${config.key}`);
  validateSections(config.path, parsed.sections, [config.title], [config.title], issues);
  const indexed = validateTable(config.path, parsed.sections.get(config.title), config.header, issues);
  for (const key of detailKeys) if (!indexed.has(key)) issues.push(`${config.path} 缺少详情 ${key}`);
  for (const key of indexed) if (!detailKeys.has(key)) issues.push(`${config.path} 引用了不存在的详情 ${key}`);
}

function validateReferences(
  parsedByPath: Map<string, ParsedMarkdown>,
  keys: Map<KnowledgeKind, Set<string>>,
  issues: string[],
) {
  for (const [path, parsed] of parsedByPath) {
    const references: Array<[KnowledgeKind, unknown, string]> = path.startsWith('operations/')
      ? [['object', nested(parsed.frontmatter.uses, 'objects'), 'uses.objects'],
        ['rule', nested(parsed.frontmatter.uses, 'rules'), 'uses.rules'],
        ['metric', nested(parsed.frontmatter.uses, 'metrics'), 'uses.metrics']]
      : path.startsWith('rules/')
        ? [['object', nested(parsed.frontmatter.applies_to, 'objects'), 'applies_to.objects']]
        : path.startsWith('metrics/')
          ? [['object', nested(parsed.frontmatter.uses, 'objects'), 'uses.objects']]
          : [];
    for (const [kind, value, label] of references) {
      for (const key of stringList(path, value, label, issues)) {
        if (!keys.get(kind)!.has(key)) issues.push(`${path} 的 ${label} 引用了不存在的 ${kind} ${key}`);
      }
    }
  }
}

function validateKeys(
  path: string,
  value: Record<string, unknown>,
  allowed: string[],
  required: string[],
  issues: string[],
) {
  for (const key of Object.keys(value)) if (!allowed.includes(key)) issues.push(`${path} 包含不支持的 frontmatter 字段 ${key}`);
  for (const key of required) {
    const item = value[key];
    if (item === undefined || item === null || item === '' || (Array.isArray(item) && !item.length)) {
      issues.push(`${path} 缺少 frontmatter 字段 ${key}`);
    }
  }
}

function validateSections(
  path: string,
  sections: Map<string, string>,
  allowed: string[],
  required: string[],
  issues: string[],
) {
  for (const name of sections.keys()) if (!allowed.includes(name)) issues.push(`${path} 包含不支持的章节 # ${name}`);
  for (const name of required) {
    if (!sections.get(name)?.trim()) issues.push(`${path} 缺少或留空章节 # ${name}`);
  }
}

function validateNestedLists(path: string, value: unknown, label: string, allowed: string[], issues: string[]) {
  if (value === undefined) return;
  if (!isRecord(value)) {
    issues.push(`${path} 的 ${label} 必须是嵌套对象，不能使用 ${label}.xxx 扁平键`);
    return;
  }
  for (const key of Object.keys(value)) if (!allowed.includes(key)) issues.push(`${path} 的 ${label} 包含不支持的字段 ${key}`);
  for (const key of allowed) if (value[key] !== undefined) stringList(path, value[key], `${label}.${key}`, issues);
}

function validateOperationBodyReferences(path: string, parsed: ParsedMarkdown, issues: string[]) {
  const items = (parsed.sections.get('规则、指标和注意事项') ?? '').split('\n')
    .map((line) => line.trim().match(/^[-*]\s+(.+)$/u)?.[1]).filter((item): item is string => Boolean(item));
  const bodyRules = items.filter((item) => item.startsWith('相关规则：rule:')).map((item) => item.slice('相关规则：rule:'.length));
  const bodyMetrics = items.filter((item) => item.startsWith('相关指标：metric:')).map((item) => item.slice('相关指标：metric:'.length));
  const frontmatterRules = stringList(path, nested(parsed.frontmatter.uses, 'rules'), 'uses.rules', issues);
  const frontmatterMetrics = stringList(path, nested(parsed.frontmatter.uses, 'metrics'), 'uses.metrics', issues);
  if (!sameValues(bodyRules, frontmatterRules)) {
    issues.push(`${path} 正文相关规则与 uses.rules 不一致：期望 ${frontmatterRules.join('、') || '空'}，实际 ${bodyRules.join('、') || '空'}；每项严格写成“- 相关规则：rule:<key>”`);
  }
  if (!sameValues(bodyMetrics, frontmatterMetrics)) {
    issues.push(`${path} 正文相关指标与 uses.metrics 不一致：期望 ${frontmatterMetrics.join('、') || '空'}，实际 ${bodyMetrics.join('、') || '空'}；每项严格写成“- 相关指标：metric:<key>”`);
  }
}

function validateTable(path: string, section: string | undefined, expected: string[], issues: string[]): Set<string> {
  const lines = String(section ?? '').split('\n').map((line) => line.trim()).filter(Boolean);
  const headerIndex = lines.findIndex((line) => line.startsWith('|'));
  if (headerIndex < 0) {
    issues.push(`${path} 缺少表格：${expected.join(' | ')}`);
    return new Set();
  }
  const actual = splitTableRow(lines[headerIndex]);
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    issues.push(`${path} 表头必须是 ${expected.join(' | ')}`);
  }
  const divider = lines[headerIndex + 1] ?? '';
  if (!splitTableRow(divider).every((cell) => /^:?-{3,}:?$/u.test(cell))) issues.push(`${path} 的 Markdown 表格缺少分隔行`);
  return new Set(lines.slice(headerIndex + 2).filter((line) => line.startsWith('|'))
    .map((line) => splitTableRow(line)[0]).filter((key) => key && key !== '—'));
}

function splitTableRow(line: string): string[] {
  return line.trim().replace(/^\|/u, '').replace(/\|$/u, '').split('|').map((cell) => cell.trim());
}

function stringList(
  path: string,
  value: unknown,
  label: string,
  issues: string[],
  required = false,
): string[] {
  if (value === undefined && !required) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || !item.trim())) {
    issues.push(`${path} 的 ${label} 必须是非空字符串列表`);
    return [];
  }
  if (required && !value.length) issues.push(`${path} 的 ${label} 不能为空`);
  return value.map((item) => String(item).trim());
}

function nested(value: unknown, key: string): unknown {
  return isRecord(value) ? value[key] : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function sameValues(left: string[], right: string[]): boolean {
  return JSON.stringify([...new Set(left)].sort()) === JSON.stringify([...new Set(right)].sort());
}
