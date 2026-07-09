/**
 * 真实 LLM 端点全流程冒烟：覆盖全部 6 个 agent 角色 ——
 * 初析 → 访谈（多批答题至结束）→ 胜任力 → 包生成 → Judge → 优化器（建议 + 修订）。
 * 与 faux 冒烟互补：faux 验证编排逻辑，这里验证真实模型能按工具协议产出合格结果。
 * 断言按「形状 + 底线质量」写（真实模型输出非确定）。
 * 用法：set -a && source .env && set +a && DATA_DIR=$(mktemp -d) npx tsx scripts/smoke-real.ts
 * 注意：不回显任何 env 配置值。
 */
import { createProject, getProject } from '../src/projects/repo.js';
import { runAnalyze } from '../src/workflow/engine.js';
import * as m2 from '../src/workflow/m2.js';
import { listPackageVersions, getPackageVersion, insertPackageVersion } from '../src/workflow/m2-repo.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { judge } from '../src/evaluation/judge.js';
import { proposeOptimizations, applyOptimizations } from '../src/evaluation/optimizer.js';
import { checkCoverage } from '../src/workflow/m2.js';
import { config } from '../src/config.js';
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
  filename: 'eco-flow.md',
  textContent: 'CHANGE_ORDER 变更管理规范：所有 CHANGE_ORDER 需依次经工艺、质量双会签后方可生效；生效后按批次区间更新 STRUCTURE_LIST 的 Effective 字段。',
}];

async function main() {
  if (!config.llm.baseUrl || config.llm.baseUrl === 'faux') {
    console.error('✗ 需配置真实端点（source .env），当前为 faux/未配置');
    process.exit(1);
  }
  console.log('\n=== 真实 LLM 端点全流程冒烟（输出非确定，断言取形状+底线） ===');
  const t0 = Date.now();
  const project = createProject({ owner: 'smoke-real', name: '示例工程变更', domain: 'DOMAIN', background: '示例制造 STRUCTURE_LIST 与工程变更管理' });
  const ctx = { tables: TABLES, documents: DOCS };

  step('节点 3 初步分析（agent：探查工具 + submit_analysis）');
  const { analysis } = await runAnalyze(project.id, TABLES, DOCS);
  ok((analysis.candidateObjects?.length ?? 0) >= 1, `候选对象 ${analysis.candidateObjects?.length} 个：${(analysis.candidateObjects ?? []).map((o: any) => o.name).join('、')}`);
  ok((analysis.openQuestions?.length ?? 0) >= 1, `开放问题 ${analysis.openQuestions?.length} 条`);
  ok(!analysis.domainOverview.includes('未提交'), '领域概览为结构化提交（非兜底）');

  step('节点 4 建模访谈（agent：ask_user 挂起/恢复 → finish_interview）');
  let r = await m2.startInterview(getProject(project.id)!, ctx, analysis);
  let batches = 0;
  while (!r.finished && r.batch && batches < 4) {
    batches++;
    const qs = r.batch.questions as any[];
    ok(qs.length >= 1 && qs.every((q) => q.q && ['single', 'multi', 'text'].includes(q.kind)),
      `第 ${batches} 批 ${qs.length} 题，题型/字段合法`);
    const answers = qs.map((q) => q.kind === 'text' || !q.options?.length
      ? { questionId: q.id, selected: [], other: '有效性按批次区间（如 BATCH001-BATCH050）判定；CHANGE_ORDER 需工艺、质量双会签后生效。' }
      : { questionId: q.id, selected: [q.options[0]] });
    r = await m2.answerInterview(getProject(project.id)!, ctx, r.batch.id, answers);
  }
  ok(batches >= 1, `完成 ${batches} 批访谈答题`);
  if (!r.finished) console.log('  ⚠  4 批后 agent 仍未主动结束（不判失败，真实模型行为差异）');

  step('节点 5 胜任力问题（agent：propose_competency_questions）');
  const cqs = await m2.proposeCompetency(getProject(project.id)!, ctx, analysis);
  ok(cqs.length >= 5, `候选胜任力问题 ${cqs.length} 条（≥5）`);
  const { setCompetencySelection } = await import('../src/workflow/m2-repo.js');
  setCompetencySelection(project.id, cqs.slice(0, 5).map((q) => q.id));

  step('节点 6 包生成（agent：submit_package_spec → 渲染标准包）');
  const gen = await m2.generatePackage(getProject(project.id)!, ctx);
  ok(gen.spec.objects.length >= 1 && gen.spec.operations.length >= 1,
    `规格：对象 ${gen.spec.objects.length} / 操作 ${gen.spec.operations.length}`);
  const files = renderPackage(gen.spec);
  ok(!!files['manifest.yaml'] && !!files['index.md'], '渲染出 manifest.yaml / index.md');
  const covered = gen.coverage.covered.length;
  ok(covered + gen.coverage.uncovered.length === 5, `胜任力覆盖检查：覆盖 ${covered}/5`);

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
  const raw = await proposeOptimizations(getProject(project.id)!, gen.spec, failedResults as any);
  ok(raw.length >= 1 && raw.every((s) => s.title && s.detail && s.kind), `建议 ${raw.length} 条，字段完整（kind：${raw.map((s) => s.kind).join('、')}）`);
  const adopted: OptSuggestion[] = raw.map((s, i) => ({
    id: `sg_${i}`, projectId: project.id, roundId: 'x', basedVersion: gen.version,
    title: s.title, detail: s.detail, kind: s.kind as any, target: s.target,
    relatedQuestion: s.relatedQuestion, status: 'pending', createdAt: '',
  }));
  const revised = await applyOptimizations(getProject(project.id)!, gen.spec, adopted);
  ok(revised.objects.length >= gen.spec.objects.length && revised.operations.length >= gen.spec.operations.length,
    `修订规格保留既有内容（对象 ${gen.spec.objects.length}→${revised.objects.length}，操作 ${gen.spec.operations.length}→${revised.operations.length}）`);
  const version = `1.${listPackageVersions(project.id).length}.0`;
  const newSpec: PackageSpec = { ...gen.spec, version, objects: revised.objects, operations: revised.operations };
  insertPackageVersion(project.id, version, newSpec, checkCoverage(newSpec, []), '真实端点冒烟：优化迭代');
  ok(!!getPackageVersion(project.id, version), `新版本 ${version} 已入库`);

  console.log(`\n=== 真实端点冒烟${process.exitCode ? '存在失败' : '通过'}（耗时 ${Math.round((Date.now() - t0) / 1000)}s） ===\n`);
}

main().catch((e) => { console.error('\nREAL SMOKE ERROR:', e?.message ?? e); process.exit(1); });
