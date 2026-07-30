/**
 * 知识包格式回归（对照 examples/corporate-marketing-eval/ontology-package-final-design.md v0.2）。
 * 覆盖 2026-07 格式缺漏审计的 17 个修复点（H1-H4 / M1-M8 / L1-L5）：
 * 构造覆盖全部资源类型（对象/操作/规则/指标/脚本目录/映射）的 sample spec → 渲染 →
 * 逐项断言 frontmatter、表格列数、key 校验、manifest 转义、mapping 一致性、
 * 渲染产物层体检对异常文件内容的拦截等。
 * 运行：npx tsx scripts/verify-format-fix.ts（纯内存，不依赖 DB/LLM）。
 */
import { renderPackage } from '../src/workflow/package-render.js';
import { runFormatChecks, runRuleChecks } from '../src/workflow/package-check.js';
import { validateKey, assertSafeZipPath } from '../src/workflow/keys.js';
import { zipPackage } from '../src/workflow/export.js';
import type { PackageSpec } from '../src/types.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };
const throws = (fn: () => unknown, frag: string, m: string) => {
  try { fn(); ok(false, `${m}（预期抛错但未抛）`); }
  catch (e) { const msg = e instanceof Error ? e.message : String(e); ok(msg.includes(frag), `${m}（${msg.slice(0, 70)}）`); }
};

// ---- 覆盖全部资源类型的 sample spec（name/description 带 YAML 特殊字符，测转义）----
const SPEC: PackageSpec = {
  id: 'verify-agent',
  name: '格式验证: 演示"包"',
  description: '覆盖全部资源类型的样例：对象、操作、规则、指标。',
  version: '1.0.0',
  language: 'zh-CN',
  status: 'released',
  defaultProviderAlias: 'domain',
  objects: [
    {
      key: 'CHANGE_ORDER受影响对象', title: 'CHANGE_ORDER受影响对象',
      description: '记录一个 CHANGE_ORDER 变更影响到的设计对象、变更前后版本和处理意见。',
      aliases: ['CHANGE_ORDER影响对象', '设计变更影响对象'],
      queryBoundary: '查询前必须提供 CHANGE_ORDER 编号；不得将缺失版本解释为无影响。',
      relations: [{ relation: 'belongs_to', target: 'CHANGE_ORDER变更单', note: '受影响对象归属于一个 CHANGE_ORDER' }],
      sourceTable: 'CHANGE_ORDER_AFFECTED_OBJECT', primaryKey: ['eco_id'],
      fields: [
        { key: 'eco_id', cnName: 'CHANGE_ORDER编号', type: 'string', required: true, note: '工程变更单编号', alias: '变更单号', sourceColumn: 'CHANGE_ORDER_ID' },
        { key: 'affected_object_id', cnName: '受影响对象ID', type: 'string' },
      ],
    },
    {
      key: 'CHANGE_ORDER变更单', title: 'CHANGE_ORDER变更单', description: '工程变更单主对象。',
      sourceTable: 'CHANGE_ORDER_AFFECTED_OBJECT', primaryKey: ['eco_id'],
      fields: [{ key: 'eco_id', type: 'string', sourceColumn: 'CHANGE_ORDER_ID' }],
    },
  ],
  operations: [
    {
      key: 'eco_change_impact', title: 'CHANGE_ORDER变更影响分析', description: '分析 CHANGE_ORDER 对物料和库存成本的影响。',
      inputs: [{ param: 'eco_id', cnName: 'CHANGE_ORDER编号', type: 'string', required: true, note: '工程变更单编号' }],
      outputs: [{ field: 'affected_items', cnName: '受影响对象', type: 'array', note: '受影响明细' }],
      flow: ['加载 object:CHANGE_ORDER变更单，确认基础状态。', '加载 object:CHANGE_ORDER受影响对象，查询受影响对象。'],
      relatedObjects: ['CHANGE_ORDER变更单', 'CHANGE_ORDER受影响对象'],
      usesRules: ['变更生效批次判断规则'], usesMetrics: ['库存处理成本'],
      notes: ['缺少 CHANGE_ORDER 编号时必须追问。'],
      script: { key: 'eco_impact_script', required: false },
      competencyQuestion: 'CHANGE_ORDER-001 会影响哪些物料',
    },
    {
      key: 'plain_op', title: '空表操作', description: '输入输出为空、无规则指标的操作。',
      inputs: [], outputs: [], flow: ['直接回答'], relatedObjects: [],
    },
  ],
  rules: [{
    key: '变更生效批次判断规则', title: '变更生效批次判断规则', description: '判断设计变更从哪个批次开始生效。',
    appliesToObjects: ['CHANGE_ORDER变更单'],
    content: '当 CHANGE_ORDER 明确指定生效批次时，以 CHANGE_ORDER 生效批次为准。',
    conditions: [{ condition: 'CHANGE_ORDER 指定生效批次', conclusion: '使用指定批次作为候选生效点' }],
    exceptions: ['紧急适航指令可以覆盖常规切换规则，但必须说明依据。'],
  }],
  metrics: [{
    key: '库存处理成本', title: '库存处理成本', description: '旧物料不可用时的库存处理成本。',
    unit: 'CNY', grain: 'material + warehouse',
    usesObjects: ['CHANGE_ORDER受影响对象'],
    caliber: '库存处理成本 = 可处理数量 * 库存单价。',
    boundaries: ['只统计不能改制、不能替代、不能继续消耗的旧物料。'],
    nullPolicy: [{ field: '库存单价为空', handling: '不得按 0 计算，必须返回缺失证据' }],
    dimensions: [{ key: 'material_id', note: '物料' }],
  }],
};

// 旧数据形态（库里存量 spec：无任何新字段）——兼容性：仍可渲染，但来源体检不再放行
const LEGACY: PackageSpec = {
  id: 'legacy-agent', name: 'legacy', description: '旧版规格', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'domain',
  objects: [{ key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: '制造物料清单', fields: [{ key: 'ID' }] }],
  operations: [{ key: 'op1', title: '查询', description: '查询 STRUCTURE_LIST', inputs: [], outputs: [], flow: ['查询'], relatedObjects: ['STRUCTURE_LIST'] }],
};

function main() {
  console.log('\n=== 知识包格式回归（17 个修复点） ===\n');
  const files = renderPackage(SPEC);
  const fm = (p: string) => files[p].startsWith('---\n') ? files[p].slice(4, files[p].indexOf('\n---', 4)) : '';

  // ---- H1 四个（+rules/metrics 共六个）index.md 带 frontmatter ----
  console.log('· H1 索引 frontmatter');
  ok(fm('index.md').includes('resource: root') && fm('index.md').includes('key: root') && fm('index.md').includes('title:'), 'index.md 带 resource: root + key + title');
  ok(fm('objects/index.md').includes('resource: object_index') && fm('objects/index.md').includes('key: objects'), 'objects/index.md 带 resource: object_index');
  ok(fm('operations/index.md').includes('resource: operation_index') && fm('operations/index.md').includes('key: operations'), 'operations/index.md 带 resource: operation_index');
  ok(fm('scripts/index.md').includes('resource: script_index') && fm('scripts/index.md').includes('key: scripts'), 'scripts/index.md 带 resource: script_index（空目录也带，按规范 3.11.1）');
  ok(fm('rules/index.md').includes('resource: rule_index') && fm('rules/index.md').includes('key: rules'), 'rules/index.md 带 resource: rule_index');
  ok(fm('metrics/index.md').includes('resource: metric_index') && fm('metrics/index.md').includes('key: metrics'), 'metrics/index.md 带 resource: metric_index');

  // ---- H2 manifest 全字段 yamlEscape ----
  console.log('· H2 manifest 转义');
  const maniName = files['manifest.yaml'].split('\n').find((l) => l.startsWith('name:'))!;
  ok(maniName === `name: ${JSON.stringify(SPEC.name)}`, `含冒号/引号的 name 被 JSON 双引号转义（${maniName.slice(0, 40)}…）`);
  ok(JSON.parse(maniName.slice('name: '.length)) === SPEC.name, '转义后可无损还原原始 name');
  ok(files['manifest.yaml'].includes('kind: ontology-package') && files['manifest.yaml'].includes('schema_version: 0.1'), 'kind/schema_version 保持规范值');

  // ---- H3 key 校验与清洗 ----
  console.log('· H3 key 校验');
  const withKey = (key: string): PackageSpec => ({ ...LEGACY, objects: [{ ...LEGACY.objects[0], key }] });
  throws(() => renderPackage(withKey('a/b')), '非法字符', '渲染拒绝含 / 的对象 key');
  throws(() => renderPackage(withKey('a..b')), '..', '渲染拒绝含 .. 的对象 key');
  throws(() => renderPackage(withKey('a|b')), '非法字符', '渲染拒绝含 | 的对象 key（防表格破坏）');
  throws(() => renderPackage(withKey('a\nb')), '控制字符', '渲染拒绝含换行的对象 key');
  throws(() => renderPackage(withKey('x'.repeat(81))), '80', '渲染拒绝超 80 字符的 key');
  throws(() => renderPackage(withKey('  ')), '不能为空', '渲染拒绝空白 key');
  throws(() => renderPackage({ ...LEGACY, objects: [LEGACY.objects[0], { ...LEGACY.objects[0] }] }), '重复', '重复对象 key 报错而非静默覆盖');
  throws(() => renderPackage({ ...LEGACY, operations: [LEGACY.operations[0], { ...LEGACY.operations[0] }] }), '重复', '重复操作 key 报错而非静默覆盖');
  ok(validateKey(' STRUCTURE_LIST ') === 'STRUCTURE_LIST', 'key 清洗：去首尾空白');
  ok(validateKey('café') === 'café', 'key 清洗：NFC Unicode 规范化');
  const nfd = renderPackage(withKey('café'));
  ok('objects/café.md' in nfd, '渲染文件路径用规范化后的 key');
  throws(() => assertSafeZipPath('../evil.md'), '路径非法', 'zip 路径二次断言拦截 ..');
  throws(() => zipPackage({ 'a/../evil.md': 'x' }), '路径非法', 'zipPackage 拼路径前断言拦截穿越条目');
  ok(zipPackage(files).length > 0, '合法包可正常打 zip');

  // ---- H4 对象 frontmatter mapping 块（与 mappings json 同源一致）----
  console.log('· H4 对象 mapping 块');
  const objMd = files['objects/CHANGE_ORDER受影响对象.md'];
  const mapping = JSON.parse(files['mappings/object-data-mappings.json']);
  ok(fm('objects/CHANGE_ORDER受影响对象.md').includes('mapping:'), '对象 frontmatter 含 mapping 块');
  ok(fm('objects/CHANGE_ORDER受影响对象.md').includes('  provider_alias: domain') && fm('objects/CHANGE_ORDER受影响对象.md').includes('  resource: CHANGE_ORDER受影响对象'), 'mapping 块含 provider_alias/resource');
  ok(mapping.objects['CHANGE_ORDER受影响对象'].provider_alias === 'domain' && mapping.objects['CHANGE_ORDER受影响对象'].resource === 'CHANGE_ORDER受影响对象', '两处 provider_alias/resource 一致（同源生成）');

  // ---- M1 fields 的 key/source 均使用字段 key ----
  console.log('· M1 semantic mapping source');
  ok(mapping.objects['CHANGE_ORDER受影响对象'].fields.eco_id.source === 'eco_id', '字段 key/source 一致：eco_id -> eco_id');
  ok(mapping.objects['CHANGE_ORDER受影响对象'].fields.affected_object_id.source === 'affected_object_id', '字段 key/source 一致：affected_object_id -> affected_object_id');

  // ---- M2 mapping policy 块 ----
  console.log('· M2 mapping policy');
  const policy = mapping.objects['CHANGE_ORDER受影响对象'].policy;
  ok(policy?.read_only === true && policy?.max_rows === 1000 && Array.isArray(policy?.require_user_filter), 'policy 块齐全（read_only=true / max_rows=1000 / require_user_filter 数组）');

  // ---- M3 渲染产物层体检（拦截异常文件内容）----
  console.log('· M3 渲染产物层体检');
  ok(runFormatChecks(SPEC, files).length === 0, '纯渲染产物 0 发现（由构造保证）');
  const withOverride = (path: string, content: string) => ({ ...files, [path]: content });
  const cats = (fs2: ReturnType<typeof runFormatChecks>) => fs2.map((f) => f.category);
  ok(cats(runFormatChecks(SPEC, withOverride('objects/CHANGE_ORDER受影响对象.md', '# 被人工改坏：没有 frontmatter\n'))).includes('frontmatter 缺失'), '拦截：文件缺少 frontmatter');
  ok(cats(runFormatChecks(SPEC, withOverride('objects/index.md', files['objects/index.md'] + '| ghost_obj | 幽灵 | 索引声明但详情不存在 | CHANGE_ORDER_AFFECTED_OBJECT |\n'))).includes('索引与详情不一致'), '拦截：索引声明了不存在的详情文件');
  ok(cats(runFormatChecks(SPEC, withOverride('manifest.yaml', 'kind: something-else\n'))).includes('manifest 非法'), '拦截：manifest kind/必选字段被改坏');
  ok(cats(runFormatChecks(SPEC, withOverride('mappings/object-data-mappings.json', '{ broken'))).includes('mapping 非法'), '拦截：mappings JSON 不可解析');
  ok(cats(runFormatChecks(SPEC, withOverride('objects/CHANGE_ORDER受影响对象.md', objMd.replace('key: CHANGE_ORDER受影响对象', 'key: ../evil')))).includes('key 非法'), '拦截：frontmatter key 被改成路径穿越');
  ok(cats(runFormatChecks(SPEC, withOverride('objects/CHANGE_ORDER受影响对象.md', objMd.replace('resource: object', 'resource: operation')))).includes('resource 声明不符'), '拦截：resource 与路径不匹配');
  const noMani = { ...files } as Record<string, string>; delete noMani['manifest.yaml'];
  ok(cats(runFormatChecks(SPEC, noMani)).includes('必选文件缺失'), '拦截：必选文件缺失');

  // ---- M4 索引表格列数 ----
  console.log('· M4 索引列数');
  ok(files['objects/index.md'].includes('| object key | 标题 | 说明 | 来源Sheet |'), 'objects 索引四列（object key|标题|说明|来源Sheet）');
  ok(files['objects/index.md'].includes('| CHANGE_ORDER受影响对象 | CHANGE_ORDER受影响对象 | 记录一个 CHANGE_ORDER 变更影响到的设计对象、变更前后版本和处理意见。 | CHANGE_ORDER_AFFECTED_OBJECT |'), 'objects 索引行含标题与来源 Sheet 列');
  ok(files['operations/index.md'].includes('| operation key | 标题 | 输入 | 输出 |'), 'operations 索引四列（operation key|标题|输入|输出）');
  ok(files['operations/index.md'].includes('| eco_change_impact | CHANGE_ORDER变更影响分析 | eco_id | affected_items |'), 'operations 索引行含输入/输出摘要');

  // ---- M5 操作 frontmatter intent_examples / uses / script；正文去「# 关联对象」----
  console.log('· M5 操作 frontmatter');
  const opMd = files['operations/eco_change_impact.md'];
  const opFm = fm('operations/eco_change_impact.md');
  ok(opFm.includes('intent_examples:') && opFm.includes('- CHANGE_ORDER-001 会影响哪些物料'), 'intent_examples（来自 competencyQuestion）');
  ok(opFm.includes('uses:') && opFm.includes('objects:') && opFm.includes('- CHANGE_ORDER变更单') && opFm.includes('- CHANGE_ORDER受影响对象'), 'uses.objects（迁移自关联对象）');
  ok(opFm.includes('rules:') && opFm.includes('- 变更生效批次判断规则') && opFm.includes('metrics:') && opFm.includes('- 库存处理成本'), 'uses.rules / uses.metrics');
  ok(opFm.includes('script:') && opFm.includes('key: eco_impact_script') && opFm.includes('required: false'), 'script 块（有绑定脚本时）');
  ok(!opMd.includes('# 关联对象'), '正文「# 关联对象」章节已移除（信息进 frontmatter uses.objects）');

  // ---- M6 输入/输出表补「中文名」列 ----
  console.log('· M6 输入/输出中文名列');
  ok(opMd.includes('| 参数 key | 中文名 | 类型 | 必填 | 说明 |'), '输入表五列');
  ok(opMd.includes('| eco_id | CHANGE_ORDER编号 | string | 是 | 工程变更单编号 |'), '输入行含中文名');
  ok(opMd.includes('| 输出 key | 中文名 | 类型 | 说明 |'), '输出表四列');
  ok(opMd.includes('| affected_items | 受影响对象 | array | 受影响明细 |'), '输出行含中文名');
  const plainMd = files['operations/plain_op.md'];
  ok(plainMd.includes('| — | — | — | — | — |') && plainMd.includes('| — | — | — | — |'), '空输入/输出占位行列数同步（5/4 列）');

  // ---- M7 rules/metrics 能力 ----
  console.log('· M7 rules/metrics');
  const ruleMd = files['rules/变更生效批次判断规则.md'];
  ok(!!ruleMd && fm('rules/变更生效批次判断规则.md').includes('resource: rule'), 'rule 详情生成且 resource: rule');
  ok(fm('rules/变更生效批次判断规则.md').includes('applies_to:') && fm('rules/变更生效批次判断规则.md').includes('- CHANGE_ORDER变更单'), 'rule frontmatter applies_to.objects');
  ok(ruleMd.includes('# 规则内容') && ruleMd.includes('# 判断条件') && ruleMd.includes('| 条件 | 结论 |') && ruleMd.includes('# 例外'), 'rule 正文三章节（内容/判断条件表/例外）');
  ok(files['rules/index.md'].includes('| rule key | 标题 | 说明 |') && files['rules/index.md'].includes('| 变更生效批次判断规则 |'), 'rules 索引三列');
  const metricMd = files['metrics/库存处理成本.md'];
  ok(!!metricMd && fm('metrics/库存处理成本.md').includes('resource: metric'), 'metric 详情生成且 resource: metric');
  ok(fm('metrics/库存处理成本.md').includes('unit: CNY') && fm('metrics/库存处理成本.md').includes('grain: material + warehouse') && fm('metrics/库存处理成本.md').includes('uses:'), 'metric frontmatter unit/grain/uses.objects');
  ok(metricMd.includes('# 业务口径') && metricMd.includes('# 计算边界') && metricMd.includes('| 字段 | 处理 |') && metricMd.includes('| 维度 key | 说明 |'), 'metric 正文四章节（口径/边界/空值策略表/维度表）');
  ok(files['metrics/index.md'].includes('| metric key | 标题 | 说明 |'), 'metrics 索引三列');
  ok(files['index.md'].includes('查询真实数据前必须先加载相关 object；判断类问题优先加载相关 rule，没有规则目录时回到 operation 的业务说明；计算类问题优先加载相关 metric，没有指标目录时不得编造指标口径。'), '消费边界为规范原文表述');
  const legacyFiles = renderPackage(LEGACY);
  ok(!('rules/index.md' in legacyFiles) && !('metrics/index.md' in legacyFiles), '无规则/指标时不生成可选目录（旧数据零报错）');
  ok(runFormatChecks(LEGACY, legacyFiles).some((f) => f.category === '对象索引非法'), '旧数据仍可渲染，但缺少来源 Sheet 时体检明确报错');
  // 体检覆盖：规则/指标悬空引用与必需正文
  const badRef: PackageSpec = { ...LEGACY, operations: [{ ...LEGACY.operations[0], usesRules: ['ghost_rule'], usesMetrics: ['ghost_metric'] }], rules: [{ key: 'r1', title: 'r1', content: '' }], metrics: [{ key: 'm1', title: 'm1', caliber: '', boundaries: [] }] };
  const findings = runRuleChecks(badRef, [], []);
  ok(findings.some((f) => f.category === '悬空引用' && f.message.includes('ghost_rule')), '体检：操作引用不存在的规则报错');
  ok(findings.some((f) => f.category === '悬空引用' && f.message.includes('ghost_metric')), '体检：操作引用不存在的指标报错');
  ok(findings.some((f) => f.category === '规则不完整') && findings.some((f) => f.category === '指标不完整'), '体检：规则缺内容/指标缺口径与边界报错');

  // ---- M8 对象关系 ----
  console.log('· M8 对象关系');
  ok(objMd.includes('# 关系') && objMd.includes('| 关系 | 目标对象 | 说明 |') && objMd.includes('| belongs_to | CHANGE_ORDER变更单 | 受影响对象归属于一个 CHANGE_ORDER |'), '有 relations 时渲染「# 关系」表');
  ok(!files['objects/CHANGE_ORDER变更单.md'].includes('# 关系'), '无 relations 时省略「# 关系」章节');

  // ---- L1 queryBoundary / 字段 alias / 对象 aliases ----
  console.log('· L1 查询边界与别名');
  ok(objMd.includes('查询前必须提供 CHANGE_ORDER 编号；不得将缺失版本解释为无影响。'), 'queryBoundary 渲染真实内容');
  ok(files['objects/CHANGE_ORDER变更单.md'].includes('未声明的类型/必填性不得自行推断'), '无 queryBoundary 才用兜底文案');
  ok(objMd.includes('| 变更单号 |'), '字段别名进「别名」列');
  ok(fm('objects/CHANGE_ORDER受影响对象.md').includes('aliases:') && fm('objects/CHANGE_ORDER受影响对象.md').includes('- CHANGE_ORDER影响对象'), '对象 frontmatter aliases');

  // ---- L2 操作正文章节 ----
  console.log('· L2 操作正文');
  ok(opMd.includes('# 业务目标') && !opMd.includes('# 业务定义'), '操作正文章节名为「# 业务目标」');
  ok(opMd.includes('# 规则、指标和注意事项') && opMd.includes('- 相关规则：rule:变更生效批次判断规则') && opMd.includes('- 相关指标：metric:库存处理成本') && opMd.includes('- 缺少 CHANGE_ORDER 编号时必须追问。'), '「# 规则、指标和注意事项」含 rule:/metric: 引用与注意事项');
  ok(plainMd.includes('# 规则、指标和注意事项') && plainMd.includes('- 暂无'), '素材为空时写「暂无」');
  ok(opMd.includes('object:CHANGE_ORDER变更单'), '流程步骤保留 object:<key> 引用格式');

  // ---- L3 根 index「# 目录」五行 ----
  console.log('· L3 根目录五行');
  for (const line of ['- 对象目录：objects/index.md', '- 操作目录：operations/index.md', '- 规则目录：rules/index.md（可选）', '- 指标目录：metrics/index.md（可选）', '- 脚本目录：scripts/index.md（可选）']) {
    ok(files['index.md'].includes(line), `目录行：${line}`);
  }

  // ---- L4 空脚本目录文案 ----
  console.log('· L4 脚本目录文案');
  ok(files['scripts/index.md'].includes('当前本体包暂无已注册脚本。'), '空脚本目录文案与规范一致');

  // ---- L5 manifest status ----
  console.log('· L5 manifest status');
  ok(files['manifest.yaml'].includes('status: released'), 'spec.status 渲染进 manifest');
  ok(legacyFiles['manifest.yaml'].includes('status: draft'), '缺省回落 draft（旧数据）');
  throws(() => renderPackage({ ...LEGACY, status: 'bogus' as any }), 'status 非法', '非法 status 枚举被拒绝');

  console.log(`\n=== 格式回归${process.exitCode ? '存在失败' : '通过'} ===\n`);
}

main();
