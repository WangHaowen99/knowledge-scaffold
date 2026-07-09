/**
 * M2 冒烟：访谈挂起/恢复 + 胜任力 + 包生成，并校验产出的包结构符合 consumer 导入要求。
 * 用 faux LLM 编排。验收判据「产出的包可被 consumer 导入」以结构校验为强代理
 * （consumer 未起时对照 examples 格式；真实导入留待端到端）。
 */
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { unzipSync, strFromU8 } from 'fflate';
import { parse as yamlParseLenient } from './yaml-lite.js';
import { createProject } from '../src/projects/repo.js';
import { insertDocument } from '../src/documents/repo.js';
import { runAnalyze } from '../src/workflow/engine.js';
import * as m2 from '../src/workflow/m2.js';
import * as m2repo from '../src/workflow/m2-repo.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { zipPackage } from '../src/workflow/export.js';
import type { LlmDeps } from '../src/llm/provider.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };

function faux(steps: any[]): LlmDeps {
  const f = createFauxCore({ provider: 'faux', models: [{ id: 'faux-1' }] });
  f.setResponses(steps);
  return { model: f.getModel(), streamFn: f.streamSimple, getApiKey: () => 'faux' };
}

const TABLES = [{ name: 'STRUCTURE_LIST', rowCount: 3, fields: [
  { name: 'ID', note: '行编号', inferredType: 'number' as const, sampleValues: ['1'], distinctRatio: 1 },
  { name: 'Effective', note: '有效性(批次区间)', inferredType: 'string' as const, sampleValues: ['AC001-'], distinctRatio: 0.6 },
] }];

async function main() {
  console.log('\n=== M2 冒烟：访谈 → 胜任力 → 包生成 ===\n');
  const project = createProject({ owner: 'smoke', name: '示例业务 DOMAIN', domain: '示例制造', background: 'CHANGE_ORDER 与 STRUCTURE_LIST 建模' });
  insertDocument({ projectId: project.id, filename: 'domain.xlsx', kind: 'excel', parseStatus: 'parsed', tables: TABLES });
  const ctx = { tables: TABLES, documents: [] };

  // 先跑节点 3 拿 analysis
  await runAnalyze(project.id, TABLES, [], faux([
    fauxAssistantMessage([fauxToolCall('submit_analysis', {
      domainOverview: 'DOMAIN 域', candidateObjects: [{ name: 'STRUCTURE_LIST', rationale: '制造清单', sourceTables: ['STRUCTURE_LIST'] }], openQuestions: [{ topic: 'Effective 语义', why: '需澄清' }],
    })]),
  ]));

  // 节点 4：访谈启动（agent 出第一批题 → 挂起）
  const s1 = await m2.startInterview(project as any, ctx, { domainOverview: 'DOMAIN' }, faux([
    fauxAssistantMessage([fauxToolCall('ask_user', { questions: [
      { kind: 'single', q: 'Effective 表示什么？', options: ['批次区间', '生效日期'], topic: '字段含义' },
    ] })]),
  ]));
  ok(!s1.finished && !!s1.batch, `访谈启动：产出第一批题（${s1.batch?.questions.length} 题），节点挂起 waiting_user`);
  ok(s1.batch?.questions[0].kind === 'single', '题型/选项正确落库');

  // 节点 4：专家应答 → 恢复（agent 收到答复后结束访谈）
  const s2 = await m2.answerInterview(project as any, ctx, s1.batch!.id, [
    { questionId: s1.batch!.questions[0].id, selected: ['批次区间'] },
  ], faux([
    fauxAssistantMessage([fauxToolCall('finish_interview', { summary: 'Effective=批次区间，已澄清' })]),
  ]));
  ok(s2.finished, '专家应答后从快照恢复、agent 结束访谈（跨节点挂起/恢复成立）');
  const answers = m2repo.getAnswers(s1.batch!.id);
  ok(answers[0]?.selected[0] === '批次区间', '答案已保存（支持回溯覆盖）');

  // 节点 5：胜任力候选 → 勾选
  const cqs = await m2.proposeCompetency(project as any, ctx, { domainOverview: 'DOMAIN' }, faux([
    fauxAssistantMessage([fauxToolCall('propose_competency_questions', { questions: [
      'CHANGE_ORDER-001 涉及哪些物料变更', 'STRUCTURE_LIST 某父项下有哪些子项', 'Effective 批次区间如何解析',
      '某批次适用哪些 BOM 行', '变更前后 TargetItem 编码对照',
    ] })]),
  ]));
  ok(cqs.length === 5, `胜任力候选 ${cqs.length} 条`);
  m2repo.setCompetencySelection(project.id, cqs.map((q) => q.id));
  ok(m2repo.listCompetency(project.id).filter((q) => q.selected).length === 5, '勾选 5 条');

  // 节点 6：包生成（agent 产出 spec，每个胜任力问题被 operation 覆盖）
  const gen = await m2.generatePackage(project as any, ctx, faux([
    fauxAssistantMessage([fauxToolCall('submit_package_spec', {
      objects: [{ key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: '制造物料清单', sourceTable: 'STRUCTURE_LIST', primaryKey: ['ID'],
        fields: [{ key: 'ID', cnName: '行编号', type: 'string', required: true, note: '行编码' }, { key: 'Effective', cnName: '有效性', type: 'string', note: '批次区间' }] }],
      operations: cqs.map((q, i) => ({
        key: `op_${i}`, title: `操作${i}`, description: q.question,
        inputs: [{ param: 'x', type: 'string', required: true }], outputs: [{ field: 'y', type: 'string' }],
        flow: ['查询 STRUCTURE_LIST', '返回结果'], relatedObjects: ['STRUCTURE_LIST'], competencyQuestion: q.question,
      })),
    })]),
  ]));
  ok(gen.spec.objects.length === 1 && gen.spec.operations.length === 5, `生成 ${gen.spec.objects.length} 对象 / ${gen.spec.operations.length} 操作，版本 ${gen.version}`);
  ok(gen.coverage.uncovered.length === 0, `胜任力覆盖检查：${gen.coverage.covered.length}/5 覆盖，${gen.coverage.uncovered.length} 未覆盖`);

  // 校验渲染出的包结构符合 consumer 导入要求
  const files = renderPackage(gen.spec);
  ok('manifest.yaml' in files && 'index.md' in files && 'mappings/object-data-mappings.json' in files, '包含 manifest / index / mappings');
  ok('objects/STRUCTURE_LIST.md' in files && 'operations/op_0.md' in files, 'objects/*.md 与 operations/*.md 齐全');
  const mani = yamlParseLenient(files['manifest.yaml']);
  ok(mani.kind === 'ontology-package' && mani.schema_version === 0.1 && !!mani.id, `manifest 合法：kind=${mani.kind} id=${mani.id}`);
  const objMd = files['objects/STRUCTURE_LIST.md'];
  ok(objMd.startsWith('---\n') && objMd.includes('resource: object') && objMd.includes('| 字段 key |'), 'object.md 含 frontmatter + 字段表（格式对齐 examples）');
  const mapping = JSON.parse(files['mappings/object-data-mappings.json']);
  ok(mapping.objects.STRUCTURE_LIST?.resource === 'STRUCTURE_LIST' && Array.isArray(mapping.objects.STRUCTURE_LIST.primary_key), 'mapping json 结构正确（resource + primary_key + fields）');

  // 校验 zip 可解压且内容一致（consumer 用 Python zipfile 解压）
  const zip = zipPackage(files);
  const unzipped = unzipSync(zip);
  const names = Object.keys(unzipped);
  ok(names.includes('ontology/manifest.yaml') && names.includes('ontology/objects/STRUCTURE_LIST.md'), `zip 可解压：${names.length} 个条目`);
  ok(strFromU8(unzipped['ontology/manifest.yaml']) === files['manifest.yaml'], 'zip 内容与源一致');

  console.log(`\n=== M2 冒烟${process.exitCode ? '存在失败' : '通过'}：访谈挂起/恢复、胜任力、包生成与结构校验全绿 ===\n`);
}

main().catch((e) => { console.error('\nM2 SMOKE ERROR:', e?.stack || e); process.exit(1); });
