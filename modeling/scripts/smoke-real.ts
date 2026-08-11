/**
 * 真实 LLM 端点全流程冒烟：
 * 初析 → 已确认业务流程驱动包生成 → Judge → 优化器（建议 + 修订）。
 * 与 faux 冒烟互补：faux 验证编排逻辑，这里验证真实模型能按工具协议产出合格结果。
 * 断言按「形状 + 底线质量」写（真实模型输出非确定）。
 * 用法：set -a && source .env && set +a && DATA_DIR=$(mktemp -d) npx tsx scripts/smoke-real.ts
 * 注意：不回显任何 env 配置值。
 */
import { createProject } from '../src/projects/repo.js';
import { runAnalyze } from '../src/workflow/engine.js';
import * as m2 from '../src/workflow/m2.js';
import { getPackageVersion } from '../src/workflow/m2-repo.js';
import { getPackageArtifactStore } from '../src/artifacts/runtime.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { judge } from '../src/evaluation/judge.js';
import { proposeOptimizations, applyOptimizations } from '../src/evaluation/optimizer.js';
import { checkCoverage } from '../src/workflow/m2.js';
import { nextValidationCandidateVersion } from '../src/workflow/package-version-lifecycle.js';
import { config } from '../src/config.js';
import type { FlowchartOutput } from '../src/authoring/workflow-extras.js';
import type { DataDictTable, PackageSpec } from '../src/types.js';
import type { OptSuggestion } from '../src/workflow/m4-repo.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };
const step = (m: string) => console.log(`\n· ${m}`);

const TABLES: DataDictTable[] = [
  { name: 'STRUCTURE_LIST', rowCount: 120, fields: [
    { name: 'ID', note: '行编号', inferredType: 'number', sampleValues: ['1', '2'], distinctRatio: 1 },
    { name: 'ParentPart', note: '父项件号', inferredType: 'string', sampleValues: ['A001'], distinctRatio: 0.2 },
    { name: 'ChildPart', note: '子项件号', inferredType: 'string', sampleValues: ['B010', 'B011'], distinctRatio: 0.8 },
    { name: 'Effective', note: '有效性（批次区间）', inferredType: 'string', sampleValues: ['BATCH001-BATCH050'], distinctRatio: 0.3 },
  ] },
  { name: 'CHANGE_ORDER', rowCount: 45, fields: [
    { name: 'ChangeOrderNo', note: '变更单号', inferredType: 'string', sampleValues: ['CHANGE_ORDER-2024-001'], distinctRatio: 1 },
    { name: 'AffectedPart', note: '受影响件号', inferredType: 'string', sampleValues: ['B010'], distinctRatio: 0.6 },
    { name: 'Status', note: '审批状态', inferredType: 'string', sampleValues: ['已会签', '待工艺审批'], distinctRatio: 0.1 },
  ] },
];
const DOCS = [{
  id: 'doc-change-order-flow',
  filename: 'change-order-flow.md',
  textContent: 'CHANGE_ORDER 变更管理规范：所有 CHANGE_ORDER 需依次经工艺、质量双会签后方可生效；生效后按批次区间更新 STRUCTURE_LIST 的 Effective 字段。',
}];

const CONFIRMED_FLOWCHART: FlowchartOutput = {
  diagrams: [{
    id: 'change-order-approval',
    title: 'CHANGE_ORDER 双会签与生效',
    description: 'CHANGE_ORDER 依次经工艺、质量会签后生效，并交接给 STRUCTURE_LIST 更新环节。',
    processMeta: {
      trigger: '收到待审批 CHANGE_ORDER',
      actors: ['工艺工程师', '质量工程师'],
      inputs: ['CHANGE_ORDER 变更单'],
      exceptions: ['任一会签未通过时不得生效'],
      outputs: ['已生效 CHANGE_ORDER'],
      handoffs: ['交接 STRUCTURE_LIST 更新'],
    },
    nodes: [
      { id: 'change-order-start', label: '收到待审批 CHANGE_ORDER', kind: 'start' },
      { id: 'process-sign', label: '完成工艺会签', kind: 'action' },
      { id: 'quality-sign', label: '完成质量会签', kind: 'action' },
      { id: 'change-order-end', label: 'CHANGE_ORDER 生效', kind: 'end' },
    ],
    edges: [
      { id: 'change-order-edge-1', from: 'change-order-start', to: 'process-sign', kind: 'sequence' },
      { id: 'change-order-edge-2', from: 'process-sign', to: 'quality-sign', kind: 'sequence' },
      { id: 'change-order-edge-3', from: 'quality-sign', to: 'change-order-end', kind: 'sequence' },
    ],
  }, {
    id: 'structure_list-applicability',
    title: 'STRUCTURE_LIST 批次适用性查询',
    description: '按批次条件读取并筛选 STRUCTURE_LIST 的 Effective 区间。',
    processMeta: {
      trigger: '收到目标批次与物料条件',
      actors: ['工程人员'],
      inputs: ['目标批次', '物料条件', 'STRUCTURE_LIST 数据'],
      exceptions: ['批次条件缺失时返回待补充信息'],
      outputs: ['适用 STRUCTURE_LIST 行'],
      handoffs: [],
    },
    nodes: [
      { id: 'structure_list-start', label: '收到查询条件', kind: 'start' },
      { id: 'read-structure_list', label: '读取 STRUCTURE_LIST', kind: 'action' },
      { id: 'filter-effective', label: '按 Effective 区间筛选', kind: 'decision' },
      { id: 'structure_list-end', label: '返回适用 STRUCTURE_LIST 行', kind: 'end' },
    ],
    edges: [
      { id: 'structure_list-edge-1', from: 'structure_list-start', to: 'read-structure_list', kind: 'sequence' },
      { id: 'structure_list-edge-2', from: 'read-structure_list', to: 'filter-effective', kind: 'sequence' },
      { id: 'structure_list-edge-3', from: 'filter-effective', to: 'structure_list-end', kind: 'condition', label: '批次适用', conditionText: '目标批次落在 Effective 区间内' },
    ],
  }],
};

async function main() {
  if (!config.llm.baseUrl || config.llm.baseUrl === 'faux') {
    console.error('✗ 需配置真实端点（source .env），当前为 faux/未配置');
    process.exit(1);
  }
  console.log('\n=== 真实 LLM 端点全流程冒烟（输出非确定，断言取形状+底线） ===');
  const t0 = Date.now();
  const project = createProject({ owner: 'smoke-real', name: '示例工程变更', domain: 'DOMAIN', background: '示例业务 STRUCTURE_LIST 与工程变更管理' });
  const artifactStore = getPackageArtifactStore();
  await artifactStore.initializeDraftPackage(project.id, {
    id: 'example-domain-agent',
    name: project.name,
    description: project.background,
  });
  const ctx = { tables: TABLES, documents: DOCS };

  step('节点 3 初步分析（agent：探查工具 + submit_analysis）');
  const { analysis } = await runAnalyze(project.id, TABLES, DOCS);
  ok((analysis.candidateObjects?.length ?? 0) >= 1, `候选对象 ${analysis.candidateObjects?.length} 个：${(analysis.candidateObjects ?? []).map((o: any) => o.name).join('、')}`);
  ok((analysis.openQuestions?.length ?? 0) >= 1, `开放问题 ${analysis.openQuestions?.length} 条`);
  ok((analysis.materialAssessments?.length ?? 0) >= 1, `材料适用性判断 ${analysis.materialAssessments?.length} 条`);
  ok(!analysis.domainOverview.includes('未提交'), '领域概览为结构化提交（非兜底）');

  step('已确认业务流程（显式 FlowchartOutput）');
  ok(CONFIRMED_FLOWCHART.diagrams.length === 2, '准备 CHANGE_ORDER 审批与 STRUCTURE_LIST 查询两条业务流程');

  step('包生成（agent：对象 → 规则/指标 → 操作分阶段生成）');
  const gen = await m2.generatePackage(
    project,
    ctx,
    undefined,
    CONFIRMED_FLOWCHART,
    { dataSourceType: 'neo4j' },
  );
  ok(gen.spec.objects.length >= 1 && gen.spec.operations.length >= 1,
    `规格：对象 ${gen.spec.objects.length} / 操作 ${gen.spec.operations.length}`);
  const files = renderPackage(gen.spec);
  ok(!!files['manifest.yaml'] && !!files['index.md'], '渲染出 manifest.yaml / index.md');
  const covered = gen.coverage.covered.length;
  ok(
    covered === CONFIRMED_FLOWCHART.diagrams.length && gen.coverage.uncovered.length === 0,
    `流程覆盖检查：覆盖 ${covered}/${CONFIRMED_FLOWCHART.diagrams.length}`,
  );

  step('Judge（评测判分）');
  const v1 = await judge('STRUCTURE_LIST 的有效性如何表达？', '按批次区间（如 BATCH001-BATCH050）表达', 'STRUCTURE_LIST 的 Effective 字段按批次区间表达有效性，例如 BATCH001-BATCH050。');
  ok(v1.verdict === 'pass', `正确回答判 ${v1.verdict}（预期 pass）· 理由：${v1.reason.slice(0, 40)}`);
  const v2 = await judge('CHANGE_ORDER 审批流程是什么？', '需经工艺、质量双会签', '不知道。');
  ok(v2.verdict === 'fail', `未答判 ${v2.verdict}（预期 fail）`);

  step('优化器（propose 建议 → apply 修订 → 版本迭代）');
  const failedResults = [{
    id: 'x', roundId: 'x', testcaseId: 'x',
    question: '某 CHANGE_ORDER 生效后应如何更新 STRUCTURE_LIST？', groundTruth: 'CHANGE_ORDER 双会签生效后按批次区间更新 STRUCTURE_LIST 的 Effective 字段',
    finalAnswer: '无法回答。', verdict: 'fail' as const, reason: '包内缺少该口径', overridden: false, createdAt: '',
  }];
  const raw = await proposeOptimizations(project, gen.spec, failedResults as any);
  ok(raw.length >= 1 && raw.every((s) => s.title && s.detail && s.kind), `建议 ${raw.length} 条，字段完整（kind：${raw.map((s) => s.kind).join('、')}）`);
  const adopted: OptSuggestion[] = raw.map((s, i) => ({
    id: `sg_${i}`, projectId: project.id, roundId: 'x', basedVersion: gen.version,
    title: s.title, detail: s.detail, kind: s.kind as any, target: s.target,
    relatedQuestion: s.relatedQuestion, status: 'pending', createdAt: '',
  }));
  const revised = await applyOptimizations(project, gen.spec, adopted);
  ok(revised.objects.length >= gen.spec.objects.length && revised.operations.length >= gen.spec.operations.length,
    `修订规格保留既有内容（对象 ${gen.spec.objects.length}→${revised.objects.length}，操作 ${gen.spec.operations.length}→${revised.operations.length}）`);
  const version = nextValidationCandidateVersion({
    currentVersion: gen.version,
    currentStatus: gen.spec.status ?? 'review',
  });
  const newSpec: PackageSpec = { ...gen.spec, version, status: 'review', ...revised };
  const optimizedCoverage = checkCoverage(
    newSpec,
    [...gen.coverage.covered.map((item) => item.question), ...gen.coverage.uncovered],
  );
  await artifactStore.savePackageVersion(project.id, {
    expectedRevision: gen.revisionId,
    spec: newSpec,
    coverage: optimizedCoverage,
    note: '真实端点冒烟：优化迭代',
    action: {
      cause: 'smoke_optimization',
      summary: `真实端点冒烟优化至 ${version}`,
      actorKind: 'agent',
      actorName: '冒烟脚本',
    },
  });
  ok(!!getPackageVersion(project.id, version), `新版本 ${version} 已入库`);

  console.log(`\n=== 真实端点冒烟${process.exitCode ? '存在失败' : '通过'}（耗时 ${Math.round((Date.now() - t0) / 1000)}s） ===\n`);
}

main().catch((e) => { console.error('\nREAL SMOKE ERROR:', e?.message ?? e); process.exit(1); });
