/**
 * M2 冒烟：已确认业务流程 + 包生成，并校验产出的包结构符合 consumer 导入要求。
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
import { getPackageArtifactStore } from '../src/artifacts/runtime.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { zipPackage } from '../src/workflow/export.js';
import type { LlmDeps } from '../src/llm/provider.js';
import type { FlowchartOutput } from '../src/authoring/workflow-extras.js';

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

const CONFIRMED_FLOWCHART: FlowchartOutput = {
  diagrams: [
    ['structure-list-by-id', '按行编号查询 STRUCTURE_LIST', '收到 STRUCTURE_LIST 行编号', '读取指定 STRUCTURE_LIST 行', 'STRUCTURE_LIST 行详情'],
    ['effective-range', '判断 Effective 批次区间', '收到 STRUCTURE_LIST 行与批次', '判断批次是否落在 Effective 区间', '有效性判断结果'],
    ['applicable-structure-list', '筛选批次适用的 STRUCTURE_LIST', '收到目标批次', '按 Effective 区间筛选 STRUCTURE_LIST 行', '适用 STRUCTURE_LIST 行'],
    ['missing-effective', '识别缺失的有效性条件', '开始检查 STRUCTURE_LIST 数据', '识别 Effective 为空的 STRUCTURE_LIST 行', '待补充有效性条件的行'],
    ['structure-list-summary', '汇总批次适用结果', '完成 STRUCTURE_LIST 有效性判断', '汇总适用与不适用的 STRUCTURE_LIST 行', '批次适用性汇总'],
  ].map(([id, title, trigger, action, output]) => ({
    id,
    title,
    processMeta: {
      trigger,
      actors: ['工程人员'],
      inputs: ['STRUCTURE_LIST 数据', '批次条件'],
      exceptions: ['行编号或批次条件缺失时明确返回待补充信息'],
      outputs: [output],
      handoffs: [],
    },
    nodes: [
      { id: `start:${id}`, label: trigger, kind: 'start' as const },
      { id: `action:${id}`, label: action, kind: 'action' as const },
      { id: `end:${id}`, label: output, kind: 'end' as const },
    ],
    edges: [
      { id: `edge:${id}:1`, from: `start:${id}`, to: `action:${id}`, kind: 'sequence' as const },
      { id: `edge:${id}:2`, from: `action:${id}`, to: `end:${id}`, kind: 'sequence' as const },
    ],
  })),
};

async function main() {
  console.log('\n=== M2 冒烟：已确认业务流程 → 包生成 ===\n');
  const project = createProject({ owner: 'smoke', name: '示例业务 DOMAIN', domain: '示例制造', background: 'CHANGE_ORDER 与 STRUCTURE_LIST 建模' });
  insertDocument({ projectId: project.id, filename: 'domain.xlsx', kind: 'excel', parseStatus: 'parsed', tables: TABLES });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'example-domain-agent',
    name: project.name,
    description: project.background,
  });
  const ctx = { tables: TABLES, documents: [] };

  // 先跑节点 3 拿 analysis
  await runAnalyze(project.id, TABLES, [], faux([
    fauxAssistantMessage([fauxToolCall('submit_analysis', {
      domainOverview: 'DOMAIN 域', candidateObjects: [{ name: 'STRUCTURE_LIST', rationale: '制造清单', sourceTables: ['STRUCTURE_LIST'] }], openQuestions: [{ topic: 'Effective 语义', why: '需澄清' }],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_artifact_analysis', {
      materialAssessments: [{
        source: 'STRUCTURE_LIST', sourceType: 'table', suitableFor: ['object'], rationale: 'STRUCTURE_LIST 提供制造清单对象及字段映射。',
      }],
      candidateArtifacts: [{
        kind: 'object', name: 'STRUCTURE_LIST', rationale: '制造清单数据表可直接建模为业务对象。',
        sources: ['STRUCTURE_LIST'], evidence: 'ID / Effective 字段', readiness: 'extractable',
      }],
    })]),
  ]));

  ok(CONFIRMED_FLOWCHART.diagrams.length === 5, '显式构造 5 条已确认业务流程');

  // 对象 → 规则/指标 → operation 分阶段生成，每条已确认流程都被 operation 覆盖
  const object = {
    candidateId: 'candidate_1', key: 'STRUCTURE_LIST', title: '制造物料清单', description: '制造物料清单',
    sourceTable: 'STRUCTURE_LIST', primaryKey: ['ID'],
    fields: [
      { key: 'ID', cnName: '行编号', type: 'string', required: true, note: '行编码' },
      { key: 'Effective', cnName: '有效性', type: 'string', note: '批次区间' },
    ],
  };
  const operations = CONFIRMED_FLOWCHART.diagrams.map((diagram, i) => ({
    key: `op_${i}`, title: diagram.title, description: `执行${diagram.title}`,
    inputs: [{ param: 'x', type: 'string', required: true }], outputs: [{ field: 'y', type: 'string' }],
    flow: ['查询 object:STRUCTURE_LIST', '返回结果'], relatedObjects: ['STRUCTURE_LIST'], competencyQuestionId: `process_${diagram.id}`,
  }));
  const gen = await m2.generatePackage(project as any, ctx, faux([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: operations.slice(0, 3) })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: operations.slice(3) })]),
  ]), CONFIRMED_FLOWCHART, { dataSourceType: 'neo4j' });
  ok(gen.spec.objects.length === 1 && gen.spec.operations.length === 5, `生成 ${gen.spec.objects.length} 对象 / ${gen.spec.operations.length} 操作，版本 ${gen.version}`);
  ok(gen.coverage.uncovered.length === 0, `流程覆盖检查：${gen.coverage.covered.length}/5 覆盖，${gen.coverage.uncovered.length} 未覆盖`);

  // 校验渲染出的包结构符合 consumer 导入要求
  const files = renderPackage(gen.spec);
  ok('manifest.yaml' in files && 'index.md' in files && 'mappings/object-data-mappings.json' in files, '包含 manifest / index / mappings');
  ok('objects/STRUCTURE_LIST.md' in files && 'operations/op_0.md' in files, 'objects/*.md 与 operations/*.md 齐全');
  const mani = yamlParseLenient(files['manifest.yaml']);
  ok(mani.kind === 'ontology-package' && mani.schema_version === 0.1 && !!mani.id, `manifest 合法：kind=${mani.kind} id=${mani.id}`);
  const objMd = files['objects/STRUCTURE_LIST.md'];
  ok(objMd.startsWith('---\n') && objMd.includes('resource: object') && objMd.includes('| 字段 key |'), 'object.md 含 frontmatter + 字段表（格式对齐 examples）');
  const mapping = JSON.parse(files['mappings/object-data-mappings.json']);
  const structureListMapping = mapping.objects.STRUCTURE_LIST;
  ok(
    structureListMapping?.resource === 'STRUCTURE_LIST'
      && JSON.stringify(structureListMapping.primary_key) === JSON.stringify(['ID'])
      && structureListMapping.fields?.ID?.source === 'ID'
      && structureListMapping.fields?.Effective?.source === 'Effective',
    'mapping json 的 object、resource、primary_key、fields 和 source 均使用稳定 key',
  );

  // 校验 zip 可解压且内容一致（consumer 用 Python zipfile 解压）
  const zip = zipPackage(files);
  const unzipped = unzipSync(zip);
  const names = Object.keys(unzipped);
  ok(names.includes('ontology/manifest.yaml') && names.includes('ontology/objects/STRUCTURE_LIST.md'), `zip 可解压：${names.length} 个条目`);
  ok(strFromU8(unzipped['ontology/manifest.yaml']) === files['manifest.yaml'], 'zip 内容与源一致');

  console.log(`\n=== M2 冒烟${process.exitCode ? '存在失败' : '通过'}：业务流程、包生成与结构校验全绿 ===\n`);
}

main().catch((e) => { console.error('\nM2 SMOKE ERROR:', e?.stack || e); process.exit(1); });
