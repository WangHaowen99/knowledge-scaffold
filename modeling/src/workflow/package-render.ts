import type {
  PackageSpec,
  SpecObject,
  SpecOperation,
  SpecRule,
  SpecMetric,
  SpecScript,
  SpecValueBinding,
} from '../types.js';
import { validateSpecKeys } from './keys.js';
import {
  mappingFieldName,
  mappingObjectName,
  mappingPrimaryNames,
} from './mapping-contract.js';
import { validateGeneratedScriptSafety } from './script-safety.js';
import { sourceLabelsFor, tableSourceFor } from './source-references.js';
import { packageMappingResource } from './package-data-source.js';
import { canonicalizePackageSpec } from './package-spec.js';
import {
  PACKAGE_MANIFEST_KIND,
  PACKAGE_MANIFEST_SCHEMA_VERSION,
  PACKAGE_STATUSES,
} from './package-contract.js';

// PackageSpec → 标准知识包文件集（遵循 consumer 当前知识包导入格式）。
// 返回 { path: content } 映射，供落盘或打 zip。
// 兼容性：库里存量 spec 没有后加字段（sourceRefs/sourceColumn/relations/rules/metrics/status…）。
// 只有绑定数据表的对象生成 mapping；文档语义对象仍可被 Agent 正常消费。
export type PackageFiles = Record<string, string>;
export interface RenderPackageOptions {
  includeDraftTemplate?: boolean;
  /** 只读诊断存量包时可跳过脚本执行契约门禁；所有写入路径保持默认严格校验。 */
  validateScripts?: boolean;
}

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
const evidenceSection = (evidence: string[] | undefined) => (
  evidence?.length ? ['# 知识证据', '', ...evidence.map((item) => `- ${item}`), ''] : []
);

// ---- mapping 单一来源：对象 frontmatter mapping 块与 mappings json 同源生成，保证两处一致 ----
function objectMapping(spec: PackageSpec, o: SpecObject) {
  const fields: Record<string, unknown> = {};
  for (const f of o.fields) {
    fields[mappingFieldName(f)] = {
      source: f.key,
      type: f.type || 'string',
      required: !!f.required,
      readable: true,
      ...(f.valueBindingRef ? { value_binding_ref: f.valueBindingRef } : {}),
    };
  }
  return {
    provider_alias: spec.defaultProviderAlias,
    resource: packageMappingResource(spec, o),
    primary_key: mappingPrimaryNames(o),
    fields,
    // policy（规范 §18 4.6）。规范未给默认值，取保守默认：只读、上限 1000 行；
    // require_user_filter 规范类型为字符串数组，「无必须过滤条件」用空数组表达。
    policy: { read_only: true, max_rows: 1000, require_user_filter: [] as string[] },
  };
}

function valueBindingJson(binding: SpecValueBinding): Record<string, unknown> {
  return {
    ...(binding.providerAlias ? { provider_alias: binding.providerAlias } : {}),
    resource: binding.resource,
    value_field: binding.valueField,
    semantic_field: binding.semanticField,
    ...(binding.aliasFields?.length ? { alias_fields: binding.aliasFields } : {}),
  };
}

function objectMd(spec: PackageSpec, o: SpecObject): string {
  const tableSource = tableSourceFor(o);
  const m = tableSource ? objectMapping(spec, o) : undefined;
  const fm = frontmatter({
    resource: 'object', key: o.key, title: o.title, description: o.description,
    aliases: o.aliases,
    mapping: m ? { provider_alias: m.provider_alias, resource: m.resource } : undefined,
    sources: sourceLabelsFor(o),
  });
  const rows = o.fields.map((f) =>
    `| ${cell(f.key)} | ${cell(f.cnName)} | ${cell(f.type ?? '未声明')} | ${f.required == null ? '未声明' : f.required ? '是' : '否'} | ${cell(f.note)} | ${cell(f.alias)} |`);
  const relations = (o.relations ?? []).map((r) =>
    `| ${cell(r.relation)} | ${cell(r.target)} | ${cell(r.note)} | ${cell(r.evidence?.join('；'))} |`);
  return [
    fm,
    '# 业务定义', '', o.description, '',
    '# 字段', '',
    '| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |',
    '| --- | --- | --- | --- | --- | --- |',
    ...rows, '',
    // 「# 关系」推荐章节（规范 §9）：无关系数据时省略整节
    ...(relations.length ? ['# 关系', '', '| 关系 | 目标对象 | 说明 | 证据 |', '| --- | --- | --- | --- |', ...relations, ''] : []),
    '# 查询边界', '',
    o.queryBoundary || (tableSource
      ? '真实数据查询应通过 mappings/object-data-mappings.json 解析数据源和字段；未声明的信息不得自行推断。'
      : '该对象来自业务资料，只能按已给出的定义和证据使用；缺少的信息不得自行补充。'),
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
    evidence: op.evidence,
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
    ...evidenceSection(op.evidence),
  ].join('\n');
}

function objectsIndex(objects: SpecObject[]): string {
  return [
    frontmatter({ resource: 'object_index', key: 'objects', title: '对象目录' }),
    '# 对象目录', '',
    '| object key | 标题 | 说明 | 来源Sheet |', '| --- | --- | --- | --- |',
    ...objects.map((o) => {
      const source = tableSourceFor(o)?.table ?? (sourceLabelsFor(o).join('；') || '—');
      return `| ${cell(o.key)} | ${cell(o.title)} | ${cell(o.description)} | ${cell(source)} |`;
    }), '',
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
    evidence: r.evidence,
  });
  const conditions = (r.conditions ?? []).map((c) => `| ${cell(c.condition)} | ${cell(c.conclusion)} |`);
  const exceptions = (r.exceptions ?? []).map((e) => `- ${e}`);
  return [
    fm,
    '# 规则内容', '', r.content || r.description || '', '',
    ...(conditions.length ? ['# 判断条件', '', '| 条件 | 结论 |', '| --- | --- |', ...conditions, ''] : []),
    ...(exceptions.length ? ['# 例外', '', ...exceptions, ''] : []),
    ...evidenceSection(r.evidence),
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
    evidence: m.evidence,
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
    ...evidenceSection(m.evidence),
  ].join('\n');
}

// ---- scripts/ 目录：受控 Python 脚本，由 operation 显式绑定 ----
// 规范 §16 的空目录示例（L643-649）没有 frontmatter，但 §16 模板（3.11.1）要求
// resource: script_index frontmatter——两处矛盾，按 3.11.1 为准（示例视为规范疏漏），
// 空目录也统一带 frontmatter，保证 Agent 恒可按 resource+key 消费。
function scriptsIndex(scripts: SpecScript[]): string {
  const rows = scripts.map((script) =>
    `| ${cell(script.key)} | ${cell(script.title)} | operation:${cell(script.boundOperation)} | ${cell(script.parameters.map((item) => item.name).join('、') || '—')} |`);
  return [
    frontmatter({ resource: 'script_index', key: 'scripts', title: '脚本目录' }),
    '# 脚本目录', '',
    ...(rows.length
      ? ['| script key | 标题 | 绑定操作 | 参数摘要 |', '| --- | --- | --- | --- |', ...rows]
      : ['当前本体包暂无已注册脚本。']), '',
  ].join('\n');
}

function scriptMd(script: SpecScript): string {
  const fm = frontmatter({
    resource: 'script', key: script.key, title: script.title, description: script.description,
    bound_operation: script.boundOperation, runtime: script.runtime,
    entrypoint: script.entrypoint, timeout_seconds: script.timeoutSeconds,
    evidence: script.evidence,
  });
  const parameters = script.parameters.map((item) =>
    `| ${cell(item.name)} | ${cell(item.flag)} | ${cell(item.type)} | ${item.required ? '是' : '否'} | ${cell(item.defaultValue == null ? '' : String(item.defaultValue))} | ${cell(item.description)} |`);
  const outputs = script.outputs.map((item) =>
    `| ${cell(item.key)} | ${cell(item.type)} | ${cell(item.description)} |`);
  return [
    fm,
    '# 脚本用途', '', script.description, '',
    '# 输入参数', '',
    '| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 |', '| --- | --- | --- | --- | --- | --- |',
    ...(parameters.length ? parameters : ['| — | — | — | — | — | — |']), '',
    '# 输出', '',
    '| 输出 key | 类型 | 说明 |', '| --- | --- | --- |',
    ...(outputs.length ? outputs : ['| — | — | — |']), '',
    '# 执行边界', '',
    ...script.boundaries.map((item) => `- ${item}`), '',
    ...evidenceSection(script.evidence),
  ].join('\n');
}

function rootIndex(spec: PackageSpec, includeDiagrams = false): string {
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
    ...(includeDiagrams ? ['- 流程图目录：diagrams/index.md'] : []),
    '- 对象目录：objects/index.md',
    '- 操作目录：operations/index.md',
    '- 规则目录：rules/index.md（可选）',
    '- 指标目录：metrics/index.md（可选）',
    '- 脚本目录：scripts/index.md（可选）', '',
    '# Runtime 能力', '',
    '| 能力 | 状态 | 说明 |', '| --- | --- | --- |',
    '| 对象语义理解 | enabled | 通过 objects/index.md 和对象详情加载业务对象、字段和查询边界。 |',
    '| 操作流程理解 | enabled | 通过 operations/index.md 加载输入、输出、流程和关联对象。 |',
    `| 受控脚本执行 | ${spec.scripts?.length ? 'enabled' : 'disabled'} | ${spec.scripts?.length ? '通过 operation.script 绑定 scripts/ 中的结构化计算脚本。' : '当前包未注册计算脚本。'} |`,
    `| 业务数据查询 | ${spec.objects.some((object) => !!tableSourceFor(object)) ? 'enabled' : 'disabled'} | ${spec.objects.some((object) => !!tableSourceFor(object)) ? '通过 mappings/object-data-mappings.json 将对象映射到数据源和字段。' : '当前包没有绑定业务数据表。'} |`, '',
    '# 消费边界', '',
    // 规范 §7 原文表述：判断类优先 rule、计算类优先 metric，无对应目录时按规范回退
    '查询真实数据前必须先加载相关 object；判断类问题优先加载相关 rule，没有规则目录时回到 operation 的业务说明；计算类问题优先加载相关 metric，没有指标目录时不得编造指标口径。', '',
  ].join('\n');
}

function diagramsIndex(): string {
  return [
    frontmatter({ resource: 'diagram_index', key: 'diagrams', title: '流程图目录' }),
    '# 流程图目录', '',
    '当前知识包尚未生成业务流程图。AI 辅助构建会在流程确认后为每个业务流程创建独立的 Mermaid 文件。', '',
  ].join('\n');
}

function manifest(spec: PackageSpec): string {
  const status = spec.status ?? 'draft';
  if (!(PACKAGE_STATUSES as readonly string[]).includes(status)) {
    throw new Error(`知识包 status 非法：「${status}」，允许值为 ${PACKAGE_STATUSES.join(' / ')}`);
  }
  // 全部取值字段过 yamlEscape：项目名/描述等含冒号、引号、# 时不得产出坏 YAML
  return [
    `kind: ${PACKAGE_MANIFEST_KIND}`,
    `schema_version: ${PACKAGE_MANIFEST_SCHEMA_VERSION}`,
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
  for (const o of spec.objects) {
    if (tableSourceFor(o)) objects[mappingObjectName(o)] = objectMapping(spec, o);
  }
  const valueBindings = Object.fromEntries((spec.valueBindings ?? []).map((binding) => [
    binding.key,
    valueBindingJson(binding),
  ]));
  return JSON.stringify({
    schema_version: '0.1',
    default_provider_alias: spec.defaultProviderAlias,
    objects,
    ...(Object.keys(valueBindings).length ? { value_bindings: valueBindings } : {}),
  }, null, 2);
}

export function renderPackage(spec: PackageSpec, options: RenderPackageOptions = {}): PackageFiles {
  // 渲染入包前强制校验：key 清洗（NFC+trim）、非法字符/长度、包内同类唯一；不合法直接抛错
  const s = validateSpecKeys(canonicalizePackageSpec(spec));
  if (options.validateScripts !== false) {
    for (const script of s.scripts ?? []) validateGeneratedScriptSafety(script);
  }
  const rules = s.rules ?? [];
  const metrics = s.metrics ?? [];
  const files: PackageFiles = {};
  files['manifest.yaml'] = manifest(s);
  files['index.md'] = rootIndex(s, options.includeDraftTemplate === true);
  if (options.includeDraftTemplate) files['diagrams/index.md'] = diagramsIndex();
  files['objects/index.md'] = objectsIndex(s.objects);
  files['operations/index.md'] = operationsIndex(s.operations);
  // 可选目录语义：无规则/指标时不生成目录（规范 §2.2）
  if (options.includeDraftTemplate || rules.length) {
    files['rules/index.md'] = rulesIndex(rules);
    for (const r of rules) files[`rules/${r.key}.md`] = ruleMd(r);
  }
  if (options.includeDraftTemplate || metrics.length) {
    files['metrics/index.md'] = metricsIndex(metrics);
    for (const m of metrics) files[`metrics/${m.key}.md`] = metricMd(m);
  }
  files['scripts/index.md'] = scriptsIndex(s.scripts ?? []);
  for (const script of s.scripts ?? []) {
    files[`scripts/${script.key}.md`] = scriptMd(script);
    files[`scripts/${script.entrypoint}`] = script.sourceCode.endsWith('\n') ? script.sourceCode : `${script.sourceCode}\n`;
  }
  files['mappings/object-data-mappings.json'] = mappingsJson(s);
  for (const o of s.objects) files[`objects/${o.key}.md`] = objectMd(s, o);
  for (const op of s.operations) files[`operations/${op.key}.md`] = operationMd(op);
  return files;
}
