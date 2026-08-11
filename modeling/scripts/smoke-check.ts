/**
 * 包体检 + 两段式修缮冒烟（faux）：
 * ① 规则引擎：对刻意做坏的规格断言各类发现（悬空引用/典型问题重复认领/未覆盖/主键失配…）
 * ② 语义检查（faux）：submit_findings 回路
 * ③ 修缮对话：咨询→文字回复；修改→计划→应用→新版本；删除计划→数量收缩被计划豁免；
 *    计划外丢失→拒绝入库
 */
import * as XLSX from 'xlsx';
import { parseDocument } from '../src/documents/parser.js';
import { createProject, getProject } from '../src/projects/repo.js';
import { getPackageVersion, listPackageVersions } from '../src/workflow/m2-repo.js';
import { getPackageArtifactStore } from '../src/artifacts/runtime.js';
import { runRuleChecks, runSemanticCheck } from '../src/workflow/package-check.js';
import {
  PackageCandidateRejectedError,
  packageChatPlan,
  packageChatApply,
} from '../src/workflow/package-chat.js';
import { findingsToSuggestions, extractCitedKeys } from '../src/evaluation/optimizer.js';
import { runDataQualityChecks } from '../src/workflow/data-quality.js';
import type { DataDictTable, PackageSpec } from '../src/types.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };

const TABLES: DataDictTable[] = [{ name: 'STRUCTURE_LIST', rowCount: 2, fields: [
  { name: 'ID', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 },
] }];

// 刻意做坏的规格
const BAD: PackageSpec = {
  id: 'demo', name: 'demo', description: 'd', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'domain',
  objects: [
    { key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: '短', sourceTable: 'STRUCTURE_LIST', primaryKey: ['NOPE'], fields: [{ key: 'ID' }] },
    { key: 'GHOST_SRC', title: '幽灵表对象', description: '来源表不存在于数据字典的对象', sourceTable: 'NOT_A_TABLE', fields: [{ key: 'X' }] },
  ],
  operations: [
    { key: 'OP-A', title: '甲', description: 'd', inputs: [{ param: 'change_order_id', type: 'string', required: true }], outputs: [], flow: ['查 STRUCTURE_LIST'], relatedObjects: ['STRUCTURE_LIST'], usesRules: ['MISSING-RULE'], competencyQuestion: '问题一' },
    { key: 'OP-B', title: '乙', description: 'd', inputs: [], outputs: [], flow: [], relatedObjects: ['DANGLING'], competencyQuestion: '问题一' },
  ],
  rules: [{ key: 'KEEP-RULE', title: '保留规则', content: '修缮操作时不得丢失。', appliesToObjects: ['STRUCTURE_LIST'] }],
  metrics: [{ key: 'KEEP-METRIC', title: '保留指标', caliber: '记录数', boundaries: ['STRUCTURE_LIST 表'], usesObjects: ['STRUCTURE_LIST'] }],
};

const CHAT: PackageSpec = {
  ...BAD,
  status: 'released',
  objects: [{
    key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: '用于修缮对话回归的完整业务对象。',
    sourceTable: 'STRUCTURE_LIST', primaryKey: ['ID'], fields: [{ key: 'ID' }],
  }],
  operations: [{
    key: 'OP-A', title: '甲', description: '查询 STRUCTURE_LIST 记录', inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:STRUCTURE_LIST', '执行 script:keep_script', '返回结果'], relatedObjects: ['STRUCTURE_LIST'], competencyQuestion: '问题一',
    script: { key: 'keep_script', required: true },
  }],
  scripts: [{
    key: 'keep_script', title: '保留脚本', description: '修缮其他资源时不得丢失。', boundOperation: 'OP-A',
    runtime: 'python', entrypoint: 'keep_script.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'result', type: 'object' }], boundaries: ['仅处理结构化输入'],
    sourceCode: [
      'import argparse',
      'import json',
      'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)',
      'args = parser.parse_args()',
      'print(json.dumps({"result": json.loads(args.input_json)}))',
    ].join('\n'),
  }],
};

const CHAT_SPEC: PackageSpec = {
  ...BAD,
  objects: [
    {
      ...BAD.objects[0],
      description: '用于验证知识包修缮流程的制造物料清单对象',
      primaryKey: ['ID'],
    },
    {
      ...BAD.objects[1],
      description: '与 STRUCTURE_LIST 共用来源 Sheet 的第二个业务实体',
      sourceTable: 'STRUCTURE_LIST',
      primaryKey: ['ID'],
      fields: [{ key: 'ID' }],
    },
  ],
};

async function main() {
  console.log('\n=== 包体检 + 两段式修缮冒烟 ===\n');

  // ⓪ Excel 解析回归（真实验收暴露：中文数据行被注释启发式吃掉 + 前导零编码变数字）
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
    ['ID', 'ParentPart'], ['行号', '父项件号'], ['1', 'A001'], ['2', 'A001'],
  ]), 'WithNote');
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
    ['CHANGE_ORDER_ID', 'Code', 'Type', 'Opinion'],
    ['CHANGE_ORDER-001', '0001', '物料替换', '建议处置'],
    ['CHANGE_ORDER-001', '0002', '物料替换', '建议处置'],
  ]), 'CjkData');
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
    ['K', 'V'], ['唯一', '一行'],
  ]), 'OneRow');
  const parsed = await parseDocument('excel', XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' }) as Buffer);
  const tb = Object.fromEntries((parsed.tables ?? []).map((t: any) => [t.name, t]));
  ok(tb.WithNote.rowCount === 2 && tb.WithNote.fields[1].note === '父项件号', '注释行模板：注释识别 + 数据 2 行');
  ok(tb.CjkData.rowCount === 2 && tb.CjkData.rows[0][1] === '0001', '中文数据行不再被吃掉（此前丢首行）');
  ok(tb.CjkData.fields[1].inferredType === 'string', '前导零编码推断为 string（防 0002→2）');
  ok(tb.OneRow.rowCount === 1, '仅一行数据时宁可当数据不当注释');

  // ⓪b 资料质量评估（确定性）：各类缺陷逐条触发
  const qTables: any[] = [
    { name: 'NoNote', rowCount: 5, rows: [['1','在库'],['2','在制'],['3','在库'],['4','总装在制'],['5','在库']],
      fields: [
        { name: 'ID', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 },
        { name: 'STATUS', inferredType: 'string', sampleValues: ['在库','在制','总装在制'], distinctRatio: 0.4 },
      ] },
    { name: 'Empty', rowCount: 0, rows: [], fields: [{ name: 'X', inferredType: 'unknown', sampleValues: [], distinctRatio: 0 }] },
    { name: 'NoPK', rowCount: 4, rows: [['a'],['a'],['b'],['b']], fields: [{ name: 'V', inferredType: 'string', sampleValues: ['a','b'], distinctRatio: 0.5 }] },
    { name: 'Child', rowCount: 4, rows: [['1'],['2'],['9'],['9']], fields: [{ name: 'ID', inferredType: 'string', sampleValues: ['1','9'], distinctRatio: 0.75 }] },
  ];
  // Child.ID 引用 NoNote.ID（主键），命中率 50% → 外键悬空
  const qf = runDataQualityChecks(qTables, []);
  const hasQ = (cat: string) => qf.some((f) => f.category === cat);
  ok(hasQ('规则来源缺失'), '质量：无文档 → 规则来源缺失');
  ok(hasQ('字段无注释'), '质量：整表无注释');
  ok(hasQ('枚举语义不明'), '质量：无注释枚举列（在库/在制…）');
  ok(hasQ('空表'), '质量：空表');
  ok(hasQ('主键候选缺失'), '质量：无唯一值字段');
  ok(hasQ('外键悬空'), '质量：同名键列取值互证失败');
  const qf2 = runDataQualityChecks([qTables[0]], [{ filename: 'r.md', textContent: 'NoNote 表说明……' + '规'.repeat(600) }]);
  ok(!qf2.some((f) => f.category === '规则来源缺失'), '质量：有文档时不再报规则来源缺失');
  ok(!qf2.some((f) => f.category === '文档未提及' && f.target === 'NoNote'), '质量：文档提及表名不报未提及');

  // ① 规则引擎
  const fs = runRuleChecks(BAD, TABLES, ['问题一', '没人认领的问题']);
  const has = (cat: string) => fs.some((f) => f.category === cat);
  ok(has('典型问题重复认领'), `规则：典型问题重复认领（OP-A/OP-B 双认领）`);
  ok(has('悬空引用'), '规则：悬空引用（OP-B → DANGLING）');
  ok(has('操作不完整'), '规则：操作缺 flow（OP-B）');
  ok(has('领域问题未覆盖'), '规则：选中领域问题未被认领');
  ok(has('主键失配'), '规则：主键不在字段清单');
  ok(has('数据映射失效'), '规则：来源表不在数据字典');
  ok(has('输入未使用'), '规则：必填输入未在流程中引用');
  ok(has('描述过简'), '规则：对象描述过短');
  const shortClaim: PackageSpec = {
    ...CHAT,
    operations: [{ ...CHAT.operations[0], competencyQuestion: '产品' }],
  };
  ok(runRuleChecks(shortClaim, TABLES, ['应该推荐什么产品？']).some((f) => f.category === '领域问题未覆盖'),
    '规则：短子串不能虚假覆盖完整领域问题');

  // ①b 体检发现 → 优化建议（确定性转换）
  const sugs = findingsToSuggestions(fs);
  ok(sugs.length > 0 && sugs.every((s) => s.title.startsWith('[体检]')), `体检发现转建议 ${sugs.length} 条（带 [体检] 前缀）`);
  ok(!sugs.some((s) => s.detail.includes('描述过短')), '提示级发现不转建议（过滤噪音）');
  ok(sugs.find((s) => s.title.includes('典型问题重复认领'))?.kind === 'modify_operation', '重复认领 → modify_operation');
  const missingRule = sugs.find((s) => s.kind === 'add_rule');
  ok(missingRule?.target === 'MISSING-RULE', '缺失规则引用 → add_rule，目标是缺失的 rule key');

  // ①c 执行痕迹提取
  const cited = extractCitedKeys('本次通过 OP-A 查询，并参考对象 STRUCTURE_LIST 的字段；OP-Z 未出现', BAD);
  ok(cited.includes('OP-A') && cited.includes('STRUCTURE_LIST') && !cited.includes('OP-B'), `执行痕迹提取：${cited.join(',')}`);

  // ② 语义检查（faux 回路）
  const project = createProject({ owner: 'smoke', name: '体检演示', domain: 'DOMAIN', background: 'x' });
  const sem = await runSemanticCheck(getProject(project.id)!, BAD);
  ok(sem.length >= 1 && sem[0].source === 'semantic' && !!sem[0].suggestion, `语义检查（faux）产出 ${sem.length} 条发现且带修复建议`);

  // ③ 两段式修缮
  const artifacts = getPackageArtifactStore();
  const initialRevision = await artifacts.ensureProject(project.id);
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initialRevision.oid,
    spec: CHAT,
    coverage: { covered: [], uncovered: [] },
    note: '包体检冒烟基线',
    action: {
      cause: 'smoke_check_baseline',
      summary: '创建包体检冒烟基线',
      actorKind: 'system',
      actorName: '冒烟脚本',
    },
  });
  const consult = await packageChatPlan(getProject(project.id)!, [], '解释一下 OP-A 是干什么的');
  ok(!!consult.reply && !consult.plan, '咨询类：直接文字回复，不出计划');

  const planRes = await packageChatPlan(getProject(project.id)!, [], '增加一个查询 CHANGE_ORDER 的操作');
  ok(!!planRes.plan && planRes.plan.changes.length >= 1, `修改类：返回计划（${planRes.plan?.changes.length} 条 change，含风险 ${planRes.plan?.risks?.length ?? 0} 条）`);

  const applied = await packageChatApply(getProject(project.id)!, [], '增加一个查询 CHANGE_ORDER 的操作', planRes.plan!, TABLES);
  ok(applied.version === '1.0.1-rc.1', `确认应用：生成 v${applied.version}`);
  ok(listPackageVersions(project.id).length === 2, '版本历史 +1');
  const revised = getPackageVersion(project.id, '1.0.1-rc.1')?.spec;
  ok(revised?.status === 'review', '修缮版本进入发布候选评审状态');
  ok(revised?.rules?.some((r) => r.key === 'KEEP-RULE') === true, '修缮对话保留已有规则');
  ok(revised?.metrics?.some((m) => m.key === 'KEEP-METRIC') === true, '修缮对话保留已有指标');
  ok(revised?.scripts?.some((script) => script.key === 'keep_script') === true, '修缮对话保留已有脚本');

  const rulePlan: any = {
    summary: '修改保留规则',
    changes: [{ targetKind: 'rule', targetKey: 'KEEP-RULE', action: 'modify', description: '补充规则的适用条件' }],
  };
  const ruleApplied = await packageChatApply(
    getProject(project.id)!, [], '修改 KEEP-RULE，补充适用条件', rulePlan, TABLES,
  );
  ok(ruleApplied.version === '1.0.1-rc.2', `规则修缮：生成 v${ruleApplied.version}`);
  const revisedRule = getPackageVersion(project.id, '1.0.1-rc.2')?.spec.rules?.find((r) => r.key === 'KEEP-RULE');
  ok(revisedRule?.content !== BAD.rules?.[0].content, '修缮对话可以实际修改规则');

  // 删除计划：数量收缩被计划豁免
  const delPlan = await packageChatPlan(getProject(project.id)!, [], '删除第一个操作');
  ok(delPlan.plan?.changes.some((c) => c.action === 'remove'), '删除类：计划含 remove 条目');
  const delApplied = await packageChatApply(
    getProject(project.id)!, [], '删除第一个操作', delPlan.plan!, TABLES,
  );
  ok(delApplied.version === '1.0.1-rc.3', `计划内删除放行：生成 v${delApplied.version}`);

  // 已删除目标不能再次形成空转版本。
  const stalePlan = { summary: 'stale-remove', changes: [{ targetKind: 'operation' as const, targetKey: 'OP-A', action: 'remove' as const, description: '再次删除 OP-A' }] };
  const versionCountBeforeStaleApply = listPackageVersions(project.id).length;
  const guarded = await packageChatApply(getProject(project.id)!, [], '再次删除 OP-A', stalePlan, TABLES)
    .then((r) => ({ okApplied: true as const, r }))
    .catch((error: unknown) => ({ okApplied: false as const, error }));
  ok(
    !guarded.okApplied
      && guarded.error instanceof PackageCandidateRejectedError
      && guarded.error.code === 'PACKAGE_CANDIDATE_REJECTED'
      && /目标不存在/.test(guarded.error.feedback)
      && listPackageVersions(project.id).length === versionCountBeforeStaleApply,
    '防护：计划目标已不存在时拒绝空转版本',
  );

  console.log(`\n=== 冒烟${process.exitCode ? '存在失败' : '通过'} ===\n`);
}

main().catch((e) => { console.error('\nSMOKE ERROR:', e?.stack || e); process.exit(1); });
