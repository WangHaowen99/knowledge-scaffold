import type { PackageSpec, SpecObject, SpecOperation } from '../types.js';

// PackageSpec → 标准知识包文件集（严格对照 examples 格式，保证 consumer 可导入）。
// 返回 { path: content } 映射，供落盘或打 zip。
export type PackageFiles = Record<string, string>;

const yamlEscape = (s: string) => (/[:#\n"]/.test(s) ? JSON.stringify(s) : s);

function frontmatter(pairs: [string, string][]): string {
  return ['---', ...pairs.map(([k, v]) => `${k}: ${yamlEscape(v)}`), '---', ''].join('\n');
}

function objectMd(o: SpecObject): string {
  const fm = frontmatter([
    ['resource', 'object'], ['key', o.key], ['title', o.title], ['description', o.description],
  ]);
  const rows = o.fields.map((f) =>
    `| ${f.key} | ${f.cnName ?? ''} | ${f.type ?? '未声明'} | ${f.required == null ? '未声明' : f.required ? '是' : '否'} | ${f.note ?? ''} | ${f.alias ?? ''} |`);
  return [
    fm,
    '# 业务定义', '', o.description, '',
    '# 字段', '',
    '| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |',
    '| --- | --- | --- | --- | --- | --- |',
    ...rows, '',
    '# 查询边界', '',
    o.queryBoundary || '真实数据查询应通过 mappings/object-data-mappings.json 解析 provider 与字段；未声明的类型/必填性不得自行推断。',
    '',
  ].join('\n');
}

function operationMd(op: SpecOperation): string {
  const fm = frontmatter([
    ['resource', 'operation'], ['key', op.key], ['title', op.title], ['description', op.description],
  ]);
  const inputs = op.inputs.map((i) => `| ${i.param} | ${i.type} | ${i.required ? '是' : '否'} | ${i.note ?? ''} |`);
  const outputs = op.outputs.map((o) => `| ${o.field} | ${o.type} | ${o.note ?? ''} |`);
  return [
    fm,
    '# 业务定义', '', op.description, '',
    '# 输入', '',
    '| 参数 | 类型 | 必填 | 说明 |', '| --- | --- | --- | --- |',
    ...(inputs.length ? inputs : ['| — | — | — | — |']), '',
    '# 输出', '',
    '| 字段 | 类型 | 说明 |', '| --- | --- | --- |',
    ...(outputs.length ? outputs : ['| — | — | — |']), '',
    '# 流程', '',
    ...op.flow.map((s, i) => `${i + 1}. ${s}`), '',
    '# 关联对象', '',
    ...op.relatedObjects.map((r) => `- ${r}`), '',
  ].join('\n');
}

function objectsIndex(objects: SpecObject[]): string {
  return [
    '# 对象目录', '',
    '| 对象 | 说明 |', '| --- | --- |',
    ...objects.map((o) => `| ${o.key} | ${o.description} |`), '',
  ].join('\n');
}

function operationsIndex(operations: SpecOperation[]): string {
  return [
    '# 操作目录', '',
    '| 操作 | 说明 |', '| --- | --- |',
    ...operations.map((o) => `| ${o.key} | ${o.description} |`), '',
  ].join('\n');
}

function rootIndex(spec: PackageSpec): string {
  const typical = spec.operations
    .filter((o) => o.competencyQuestion)
    .map((o) => `| ${o.competencyQuestion} | operation:${o.key} |`);
  return [
    '# 领域范围', '', spec.description, '',
    '# 典型问题', '',
    '| 问题 | 推荐入口 |', '| --- | --- |',
    ...(typical.length ? typical : ['| — | — |']), '',
    '# 目录', '',
    '- 对象目录：objects/index.md',
    '- 操作目录：operations/index.md',
    '- 脚本目录：scripts/index.md', '',
    '# Runtime 能力', '',
    '| 能力 | 状态 | 说明 |', '| --- | --- | --- |',
    '| 对象语义理解 | enabled | 通过 objects/index.md 和对象详情加载业务对象、字段和查询边界。 |',
    '| 操作流程理解 | enabled | 通过 operations/index.md 加载输入、输出、流程和关联对象。 |',
    '| 业务数据查询 | enabled | 通过 mappings/object-data-mappings.json 将对象映射到 provider alias、SQL 资源和字段。 |', '',
    '# 消费边界', '',
    '查询真实数据前必须先加载相关 object；判断类问题优先加载 operation 的业务说明；计算类问题必须引用对应 operation 的输入、输出和流程，不得编造指标口径或真实数据字段。', '',
  ].join('\n');
}

function manifest(spec: PackageSpec): string {
  return [
    'kind: ontology-package',
    'schema_version: 0.1',
    `id: ${spec.id}`,
    `name: ${spec.name}`,
    `description: ${yamlEscape(spec.description)}`,
    `version: ${spec.version}`,
    `language: ${spec.language}`,
    'status: draft',
    '',
  ].join('\n');
}

function mappingsJson(spec: PackageSpec): string {
  const objects: Record<string, unknown> = {};
  for (const o of spec.objects) {
    const fields: Record<string, unknown> = {};
    for (const f of o.fields) {
      fields[f.key] = { source: f.key, type: f.type || 'string', required: !!f.required, readable: true };
    }
    objects[o.key] = {
      provider_alias: spec.defaultProviderAlias,
      resource: o.sourceTable || o.key,
      primary_key: o.primaryKey && o.primaryKey.length ? o.primaryKey : [o.fields[0]?.key].filter(Boolean),
      fields,
    };
  }
  return JSON.stringify({
    schema_version: '0.1',
    default_provider_alias: spec.defaultProviderAlias,
    objects,
  }, null, 2);
}

export function renderPackage(spec: PackageSpec): PackageFiles {
  const files: PackageFiles = {};
  files['manifest.yaml'] = manifest(spec);
  files['index.md'] = rootIndex(spec);
  files['objects/index.md'] = objectsIndex(spec.objects);
  files['operations/index.md'] = operationsIndex(spec.operations);
  files['scripts/index.md'] = '# 脚本目录\n\n（暂无受控脚本）\n';
  files['mappings/object-data-mappings.json'] = mappingsJson(spec);
  for (const o of spec.objects) files[`objects/${o.key}.md`] = objectMd(o);
  for (const op of spec.operations) files[`operations/${op.key}.md`] = operationMd(op);
  return files;
}
