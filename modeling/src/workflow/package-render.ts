import type { PackageSpec, SpecObject, SpecOperation, SpecRule, SpecMetric } from '../types.js';
import { validateSpecKeys } from './keys.js';

// PackageSpec → 标准知识包文件集（严格对照 examples/corporate-marketing-eval/
// ontology-package-final-design.md v0.2，保证 consumer 可导入）。
// 返回 { path: content } 映射，供落盘或打 zip。
// 兼容性：库里存量 spec 没有后加字段（sourceColumn/relations/rules/metrics/status…），
// 渲染对缺省一律回落，不得报错。
export type PackageFiles = Record<string, string>;

// YAML 标量转义：含特殊字符 / 首尾空白 / 空串时用 JSON 双引号形式（JSON 字符串是合法 YAML 标量）。
const yamlEscape = (s: string) =>
  (/[:#'"\n\r\t\\]|^[\s\-?*&!|>%@`[\]{},]|\s$|^$/.test(s) ? JSON.stringify(s) : s);

// ---- frontmatter 构造（支持嵌套块与列表，覆盖 mapping / uses / aliases 等规范字段） ----
type FmValue = string | number | boolean | FmValue[] | FmMap | undefined;
interface FmMap { [k: string]: FmValue }

function fmLines(m: FmMap, indent = 0): string[] {
  const pad = '  '.repeat(indent);
  const lines: string[] = [];
  for (const [k, v] of Object.entries(m)) {
    if (v === undefined) continue;
    if (Array.isArray(v)) {
      if (!v.length) continue; // 空列表 = 不声明该字段
      lines.push(`${pad}${k}:`);
      for (const item of v) lines.push(`${pad}  - ${typeof item === 'string' ? yamlEscape(item) : String(item)}`);
    } else if (typeof v === 'object') {
      const inner = fmLines(v as FmMap, indent + 1);
      if (!inner.length) continue; // 空块 = 不声明该字段
      lines.push(`${pad}${k}:`, ...inner);
    } else if (typeof v === 'string') {
      lines.push(`${pad}${k}: ${yamlEscape(v)}`);
    } else {
      lines.push(`${pad}${k}: ${v}`);
    }
  }
  return lines;
}

function frontmatter(m: FmMap): string {
  return ['---', ...fmLines(m), '---', ''].join('\n');
}

// markdown 表格单元格：清掉会破坏表格结构的 | 与换行
const cell = (s: string | undefined) => (s ?? '').replace(/\|/g, '\\|').replace(/\s*\n\s*/g, ' ');

// ---- mapping 单一来源：对象 frontmatter mapping 块与 mappings json 同源生成，保证两处一致 ----
function objectMapping(spec: PackageSpec, o: SpecObject) {
  const fields: Record<string, unknown> = {};
  for (const f of o.fields) {
    // source = 真实列名（sourceColumn），缺省回落字段 key（规范 §18「业务字段到真实字段」）
    fields[f.key] = { source: f.sourceColumn || f.key, type: f.type || 'string', required: !!f.required, readable: true };
  }
  return {
    provider_alias: spec.defaultProviderAlias,
    resource: o.sourceTable || o.key,
    primary_key: o.primaryKey && o.primaryKey.length ? o.primaryKey : [o.fields[0]?.key].filter(Boolean),
    fields,
    // policy（规范 §18 4.6）。规范未给默认值，取保守默认：只读、上限 1000 行；
    // require_user_filter 规范类型为字符串数组，「无必须过滤条件」用空数组表达。
    policy: { read_only: true, max_rows: 1000, require_user_filter: [] as string[] },
  };
}

function objectMd(spec: PackageSpec, o: SpecObject): string {
  const m = objectMapping(spec, o);
  const fm = frontmatter({
    resource: 'object', key: o.key, title: o.title, description: o.description,
    aliases: o.aliases,
    // 本工具恒启用业务数据查询（mappings 恒生成），按规范 §9 对象需声明 mapping；
    // 与 mappings/object-data-mappings.json 同源（objectMapping），两处一致。
    mapping: { provider_alias: m.provider_alias, resource: m.resource },
  });
  const rows = o.fields.map((f) =>
    `| ${cell(f.key)} | ${cell(f.cnName)} | ${cell(f.type ?? '未声明')} | ${f.required == null ? '未声明' : f.required ? '是' : '否'} | ${cell(f.note)} | ${cell(f.alias)} |`);
  const relations = (o.relations ?? []).map((r) =>
    `| ${cell(r.relation)} | ${cell(r.target)} | ${cell(r.note)} |`);
  return [
    fm,
    '# 业务定义', '', o.description, '',
    '# 字段', '',
    '| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |',
    '| --- | --- | --- | --- | --- | --- |',
    ...rows, '',
    // 「# 关系」推荐章节（规范 §9）：无关系数据时省略整节
    ...(relations.length ? ['# 关系', '', '| 关系 | 目标对象 | 说明 |', '| --- | --- | --- |', ...relations, ''] : []),
    '# 查询边界', '',
    o.queryBoundary || '真实数据查询应通过 mappings/object-data-mappings.json 解析 provider 与字段；未声明的类型/必填性不得自行推断。',
    '',
  ].join('\n');
}

function operationMd(op: SpecOperation): string {
  const fm = frontmatter({
    resource: 'operation', key: op.key, title: op.title, description: op.description,
    intent_examples: op.competencyQuestion ? [op.competencyQuestion] : undefined,
    uses: {
      objects: op.relatedObjects?.length ? op.relatedObjects : undefined,
      rules: op.usesRules?.length ? op.usesRules : undefined,
      metrics: op.usesMetrics?.length ? op.usesMetrics : undefined,
    },
    script: op.script ? { key: op.script.key, required: !!op.script.required } : undefined,
  });
  const inputs = op.inputs.map((i) =>
    `| ${cell(i.param)} | ${cell(i.cnName)} | ${cell(i.type)} | ${i.required ? '是' : '否'} | ${cell(i.note)} |`);
  const outputs = op.outputs.map((o) =>
    `| ${cell(o.field)} | ${cell(o.cnName)} | ${cell(o.type)} | ${cell(o.note)} |`);
  // 「# 规则、指标和注意事项」：uses.rules / uses.metrics 引用 + 注意事项素材；全空写「暂无」
  const ruleNotes = [
    ...(op.usesRules ?? []).map((r) => `- 相关规则：rule:${r}`),
    ...(op.usesMetrics ?? []).map((m) => `- 相关指标：metric:${m}`),
    ...(op.notes ?? []).map((n) => `- ${n}`),
  ];
  return [
    fm,
    '# 业务目标', '', op.description, '',
    '# 输入', '',
    '| 参数 key | 中文名 | 类型 | 必填 | 说明 |', '| --- | --- | --- | --- | --- |',
    ...(inputs.length ? inputs : ['| — | — | — | — | — |']), '',
    '# 输出', '',
    '| 输出 key | 中文名 | 类型 | 说明 |', '| --- | --- | --- | --- |',
    ...(outputs.length ? outputs : ['| — | — | — | — |']), '',
    '# 流程', '',
    ...op.flow.map((s, i) => `${i + 1}. ${s}`), '',
    '# 规则、指标和注意事项', '',
    ...(ruleNotes.length ? ruleNotes : ['- 暂无']), '',
  ].join('\n');
}

function objectsIndex(objects: SpecObject[]): string {
  return [
    frontmatter({ resource: 'object_index', key: 'objects', title: '对象目录' }),
    '# 对象目录', '',
    '| object key | 标题 | 说明 |', '| --- | --- | --- |',
    ...objects.map((o) => `| ${cell(o.key)} | ${cell(o.title)} | ${cell(o.description)} |`), '',
  ].join('\n');
}

function operationsIndex(operations: SpecOperation[]): string {
  return [
    frontmatter({ resource: 'operation_index', key: 'operations', title: '操作目录' }),
    '# 操作目录', '',
    '| operation key | 标题 | 输入 | 输出 |', '| --- | --- | --- | --- |',
    ...operations.map((o) =>
      `| ${cell(o.key)} | ${cell(o.title)} | ${cell(o.inputs.map((i) => i.param).join('、') || '—')} | ${cell(o.outputs.map((x) => x.field).join('、') || '—')} |`), '',
  ].join('\n');
}

// ---- rules/ 可选目录（规范 §12-13）----
function rulesIndex(rules: SpecRule[]): string {
  return [
    frontmatter({ resource: 'rule_index', key: 'rules', title: '规则目录' }),
    '# 规则目录', '',
    '| rule key | 标题 | 说明 |', '| --- | --- | --- |',
    ...rules.map((r) => `| ${cell(r.key)} | ${cell(r.title)} | ${cell(r.description)} |`), '',
  ].join('\n');
}

function ruleMd(r: SpecRule): string {
  const fm = frontmatter({
    resource: 'rule', key: r.key, title: r.title, description: r.description,
    applies_to: { objects: r.appliesToObjects?.length ? r.appliesToObjects : undefined },
  });
  const conditions = (r.conditions ?? []).map((c) => `| ${cell(c.condition)} | ${cell(c.conclusion)} |`);
  const exceptions = (r.exceptions ?? []).map((e) => `- ${e}`);
  return [
    fm,
    '# 规则内容', '', r.content || r.description || '', '',
    ...(conditions.length ? ['# 判断条件', '', '| 条件 | 结论 |', '| --- | --- |', ...conditions, ''] : []),
    ...(exceptions.length ? ['# 例外', '', ...exceptions, ''] : []),
  ].join('\n');
}

// ---- metrics/ 可选目录（规范 §14-15）----
function metricsIndex(metrics: SpecMetric[]): string {
  return [
    frontmatter({ resource: 'metric_index', key: 'metrics', title: '指标目录' }),
    '# 指标目录', '',
    '| metric key | 标题 | 说明 |', '| --- | --- | --- |',
    ...metrics.map((m) => `| ${cell(m.key)} | ${cell(m.title)} | ${cell(m.description)} |`), '',
  ].join('\n');
}

function metricMd(m: SpecMetric): string {
  const fm = frontmatter({
    resource: 'metric', key: m.key, title: m.title, description: m.description,
    unit: m.unit, grain: m.grain,
    uses: { objects: m.usesObjects?.length ? m.usesObjects : undefined },
  });
  const nullPolicy = (m.nullPolicy ?? []).map((n) => `| ${cell(n.field)} | ${cell(n.handling)} |`);
  const dims = (m.dimensions ?? []).map((d) => `| ${cell(d.key)} | ${cell(d.note)} |`);
  return [
    fm,
    '# 业务口径', '', m.caliber || m.description || '', '',
    '# 计算边界', '',
    ...((m.boundaries ?? []).length ? m.boundaries.map((b) => `- ${b}`) : ['- 未声明计算边界。']), '',
    ...(nullPolicy.length ? ['# 空值策略', '', '| 字段 | 处理 |', '| --- | --- |', ...nullPolicy, ''] : []),
    ...(dims.length ? ['# 维度', '', '| 维度 key | 说明 |', '| --- | --- |', ...dims, ''] : []),
  ].join('\n');
}

// ---- scripts/ 目录：生成链暂无受控脚本，保留空目录 ----
// 规范 §16 的空目录示例（L643-649）没有 frontmatter，但 §16 模板（3.11.1）要求
// resource: script_index frontmatter——两处矛盾，按 3.11.1 为准（示例视为规范疏漏），
// 空目录也统一带 frontmatter，保证 Agent 恒可按 resource+key 消费。
function scriptsIndex(): string {
  return [
    frontmatter({ resource: 'script_index', key: 'scripts', title: '脚本目录' }),
    '# 脚本目录', '',
    '当前本体包暂无已注册脚本。', '',
  ].join('\n');
}

function rootIndex(spec: PackageSpec): string {
  const typical = spec.operations
    .filter((o) => o.competencyQuestion)
    .map((o) => `| ${cell(o.competencyQuestion)} | operation:${cell(o.key)} |`);
  return [
    frontmatter({ resource: 'root', key: 'root', title: spec.name }),
    '# 领域范围', '', spec.description, '',
    '# 典型问题', '',
    '| 问题 | 推荐入口 |', '| --- | --- |',
    ...(typical.length ? typical : ['| — | — |']), '',
    // 「# 目录」按规范模板列五行，后三行标（可选）——rules/metrics 目录仅在有内容时生成
    '# 目录', '',
    '- 对象目录：objects/index.md',
    '- 操作目录：operations/index.md',
    '- 规则目录：rules/index.md（可选）',
    '- 指标目录：metrics/index.md（可选）',
    '- 脚本目录：scripts/index.md（可选）', '',
    '# Runtime 能力', '',
    '| 能力 | 状态 | 说明 |', '| --- | --- | --- |',
    '| 对象语义理解 | enabled | 通过 objects/index.md 和对象详情加载业务对象、字段和查询边界。 |',
    '| 操作流程理解 | enabled | 通过 operations/index.md 加载输入、输出、流程和关联对象。 |',
    '| 业务数据查询 | enabled | 通过 mappings/object-data-mappings.json 将对象映射到 provider alias、SQL 资源和字段。 |', '',
    '# 消费边界', '',
    // 规范 §7 原文表述：判断类优先 rule、计算类优先 metric，无对应目录时按规范回退
    '查询真实数据前必须先加载相关 object；判断类问题优先加载相关 rule，没有规则目录时回到 operation 的业务说明；计算类问题优先加载相关 metric，没有指标目录时不得编造指标口径。', '',
  ].join('\n');
}

const MANIFEST_STATUSES = ['draft', 'review', 'released', 'deprecated'] as const;

function manifest(spec: PackageSpec): string {
  const status = spec.status ?? 'draft';
  if (!(MANIFEST_STATUSES as readonly string[]).includes(status)) {
    throw new Error(`知识包 status 非法：「${status}」，允许值为 ${MANIFEST_STATUSES.join(' / ')}`);
  }
  // 全部取值字段过 yamlEscape：项目名/描述等含冒号、引号、# 时不得产出坏 YAML
  return [
    'kind: ontology-package',
    'schema_version: 0.1',
    `id: ${yamlEscape(spec.id)}`,
    `name: ${yamlEscape(spec.name)}`,
    `description: ${yamlEscape(spec.description)}`,
    `version: ${yamlEscape(spec.version)}`,
    `language: ${yamlEscape(spec.language)}`,
    `status: ${status}`,
    '',
  ].join('\n');
}

function mappingsJson(spec: PackageSpec): string {
  const objects: Record<string, unknown> = {};
  for (const o of spec.objects) objects[o.key] = objectMapping(spec, o);
  return JSON.stringify({
    schema_version: '0.1',
    default_provider_alias: spec.defaultProviderAlias,
    objects,
  }, null, 2);
}

export function renderPackage(spec: PackageSpec): PackageFiles {
  // 渲染入包前强制校验：key 清洗（NFC+trim）、非法字符/长度、包内同类唯一；不合法直接抛错
  const s = validateSpecKeys(spec);
  const files: PackageFiles = {};
  files['manifest.yaml'] = manifest(s);
  files['index.md'] = rootIndex(s);
  files['objects/index.md'] = objectsIndex(s.objects);
  files['operations/index.md'] = operationsIndex(s.operations);
  // 可选目录语义：无规则/指标时不生成目录（规范 §2.2）
  if (s.rules?.length) {
    files['rules/index.md'] = rulesIndex(s.rules);
    for (const r of s.rules) files[`rules/${r.key}.md`] = ruleMd(r);
  }
  if (s.metrics?.length) {
    files['metrics/index.md'] = metricsIndex(s.metrics);
    for (const m of s.metrics) files[`metrics/${m.key}.md`] = metricMd(m);
  }
  files['scripts/index.md'] = scriptsIndex();
  files['mappings/object-data-mappings.json'] = mappingsJson(s);
  for (const o of s.objects) files[`objects/${o.key}.md`] = objectMd(s, o);
  for (const op of s.operations) files[`operations/${op.key}.md`] = operationMd(op);
  return files;
}
