import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getDb } from '../db/index.js';
import { insertDocument } from '../documents/repo.js';
import { saveProjectDataSource } from '../evaluation/data-source-repo.js';
import { createProject } from '../projects/repo.js';
import type { PackageSpec, Project } from '../types.js';
import { addTestcases } from '../workflow/m3-repo.js';
import { markAcceptanceProject } from './projects.js';
import { enqueuePackageImportedProjectEvent } from '../agent/project-event-outbox.js';

export const ACCEPTANCE_SCENARIOS = [
  ['SCN-CLOSED', '闭域资源', '对象、规则、操作和指标均含可定位边界'],
  ['SCN-AMBIGUOUS', '同名歧义', '不同稳定 key 使用相同可见名称'],
  ['SCN-REFERENCES', '引用影响', '包含被引用对象和无引用指标'],
  ['SCN-BINDING', '数据映射', '三字段对象及主键明确的合成表'],
  ['SCN-GENERATION', '正常生成', '三份互不冲突的合成建模资料'],
  ['SCN-LONG', '长资料', '较长重复章节及三项独立校验关注点'],
  ['SCN-SCRIPT', '脚本生成', '待绑定脚本的合成汇总操作'],
  ['SCN-EVALUATION', '评测停止', '已确认知识包、三条人工用例和无数据源配置'],
  ['SCN-UI-BATCH', '批量审阅', '五个资源和八组独立候选变化'],
  ['SCN-UI-RESUME', '恢复上下文', '适合建立长对话、运行和待确认状态的基线'],
] as const;

export type AcceptanceScenarioId = typeof ACCEPTANCE_SCENARIOS[number][0];

const scenarioIds = new Set<string>(ACCEPTANCE_SCENARIOS.map(([id]) => id));

export function isAcceptanceScenarioId(value: string): value is AcceptanceScenarioId {
  return scenarioIds.has(value);
}

function closedSpec(base: PackageSpec): PackageSpec {
  return {
    ...base,
    version: '1.0.0',
    status: 'review',
    objects: [{
      key: 'member_profile',
      title: '合成会员档案',
      description: '只描述浏览器验收使用的合成会员基本信息。',
      queryBoundary: '不包含真实会员、联系方式或生产数据。',
      primaryKey: ['member_id'],
      fields: [
        { key: 'member_id', cnName: '合成会员编号', type: 'string', required: true },
        { key: 'member_name', cnName: '合成会员名称', type: 'string' },
        { key: 'status', cnName: '测试状态', type: 'string' },
      ],
    }],
    operations: [{
      key: 'review_synthetic_order',
      title: '复核合成订单',
      description: '依据合成资料复核测试订单，不访问外部系统。',
      inputs: [{ param: 'order_id', cnName: '合成订单编号', type: 'string', required: true }],
      outputs: [{ field: 'review_result', cnName: '复核结果', type: 'string' }],
      flow: ['读取合成订单', '核对折扣资格', '返回复核结果'],
      relatedObjects: ['member_profile'],
      usesRules: ['discount_eligibility'],
      usesMetrics: ['review_pass_rate'],
      notes: ['仅用于浏览器验收。'],
    }],
    rules: [{
      key: 'discount_eligibility',
      title: '折扣资格规则',
      description: '判断合成会员是否满足测试折扣资格。',
      appliesToObjects: ['member_profile'],
      appliesToOperations: ['review_synthetic_order'],
      content: '合成会员测试状态为有效时，可以进入折扣资格复核。',
      conditions: [{ condition: '测试状态为有效', conclusion: '进入折扣资格复核' }],
      exceptions: ['测试状态缺失时转人工确认'],
    }],
    metrics: [{
      key: 'review_pass_rate',
      title: '复核通过率',
      description: '合成订单复核通过的比例。',
      unit: '%',
      grain: '按验收批次',
      usesObjects: ['member_profile'],
      caliber: '复核通过数 / 已完成复核数 × 100%',
      boundaries: ['只统计已经完成的合成订单复核', '分母为零时不计算'],
    }],
  };
}

function scenarioSpec(id: AcceptanceScenarioId, base: PackageSpec): PackageSpec {
  const closed = closedSpec(base);
  if (id === 'SCN-CLOSED' || id === 'SCN-EVALUATION') return closed;
  if (id === 'SCN-REFERENCES') {
    return {
      ...closed,
      metrics: [...(closed.metrics ?? []), {
        key: 'unreferenced_test_metric', title: '无引用测试指标',
        description: '没有被其他资源引用，专用于删除预览。',
        caliber: '合成完成数', boundaries: ['仅统计浏览器验收记录'],
      }],
    };
  }
  if (id === 'SCN-AMBIGUOUS') {
    return {
      ...closed,
      operations: [...closed.operations, {
        key: 'review_synthetic_refund', title: '复核合成退款',
        description: '包含与另一操作相同的可见步骤，验证歧义澄清。',
        inputs: [{ param: 'refund_id', type: 'string', required: true }],
        outputs: [{ field: 'review_result', type: 'string' }],
        flow: ['读取合成订单', '返回复核结果'], relatedObjects: ['member_profile'],
      }],
      metrics: [
        ...(closed.metrics ?? []),
        { key: 'weekly_review_rate', title: '同名测试指标', caliber: '每周通过数 / 每周复核数', boundaries: ['按自然周'] },
        { key: 'monthly_review_rate', title: '同名测试指标', caliber: '每月通过数 / 每月复核数', boundaries: ['按自然月'] },
      ],
    };
  }
  if (id === 'SCN-BINDING') {
    return {
      ...base,
      version: '1.0.0', status: 'review', dataSourceType: 'nl2sql',
      objects: [{
        key: 'synthetic_member', title: '测试对象', description: '等待重新绑定的三字段合成对象。',
        primaryKey: ['member_id'],
        fields: [
          { key: 'member_id', cnName: '编号', type: 'string', required: true },
          { key: 'member_name', cnName: '名称', type: 'string' },
          { key: 'member_level', cnName: '等级', type: 'string' },
        ],
      }],
      operations: [], rules: [], metrics: [], scripts: [],
    };
  }
  if (id === 'SCN-SCRIPT') {
    return {
      ...base,
      version: '1.0.0', status: 'review', objects: [], rules: [], metrics: [], scripts: [],
      operations: [{
        key: 'synthetic_summary', title: '合成汇总', description: '汇总两个非负测试整数。',
        inputs: [
          { param: 'left', cnName: '左值', type: 'integer', required: true },
          { param: 'right', cnName: '右值', type: 'integer', required: true },
        ],
        outputs: [{ field: 'total', cnName: '合计', type: 'integer' }],
        flow: ['校验非负整数', '计算合计', '返回 JSON'], relatedObjects: [],
      }],
    };
  }
  if (id === 'SCN-UI-BATCH') {
    return {
      ...closed,
      objects: [
        ...closed.objects,
        { key: 'synthetic_order', title: '合成订单', description: '浏览器批量审阅订单。', fields: [{ key: 'order_id', type: 'string', required: true }], primaryKey: ['order_id'] },
        { key: 'synthetic_product', title: '合成商品长名称资源用于检验标题不会挤压主操作', description: '浏览器批量审阅商品。', fields: [{ key: 'product_id', type: 'string', required: true }], primaryKey: ['product_id'] },
      ],
      metrics: [...(closed.metrics ?? []), {
        key: 'synthetic_order_count', title: '合成订单数', caliber: '已完成合成订单数量', boundaries: ['只统计验收项目'],
      }],
    };
  }
  if (id === 'SCN-UI-RESUME') return closed;
  return base;
}

function seedDocuments(project: Project, id: AcceptanceScenarioId): void {
  if (id === 'SCN-BINDING') {
    insertDocument({
      projectId: project.id, filename: 'synthetic_members.xlsx', kind: 'excel', parseStatus: 'parsed', purpose: 'schema',
      tables: [{
        name: 'synthetic_members', rowCount: 3,
        fields: [
          { name: 'member_id', inferredType: 'string', sampleValues: ['member_001'], distinctRatio: 1 },
          { name: 'member_name', inferredType: 'string', sampleValues: ['synthetic_name_001'], distinctRatio: 1 },
          { name: 'member_level', inferredType: 'string', sampleValues: ['A'], distinctRatio: 0.67 },
        ],
      }],
    });
    return;
  }
  if (id !== 'SCN-GENERATION' && id !== 'SCN-LONG' && id !== 'SCN-UI-RESUME') return;
  const documents = id === 'SCN-LONG'
    ? [1, 2, 3, 4].map((index) => ({
      filename: `long_synthetic_material_${index}.md`,
      textContent: Array.from({ length: 120 }, (_, section) => (
        `## 合成章节 ${index}-${section + 1}\n合成会员、合成订单和折扣复核均为人工测试内容。`
        + ` 本章节强调边界 ${section % 3 + 1}，不得生成未要求的脚本。`
      )).join('\n\n'),
    }))
    : [
      { filename: 'synthetic_objects.md', textContent: '合成会员档案包含编号、名称和测试状态。编号唯一，状态缺失时需要人工确认。' },
      { filename: 'synthetic_process.md', textContent: '复核合成订单时先读取订单，再核对折扣资格，最后返回复核结果。' },
      { filename: 'synthetic_rules_metrics.md', textContent: '有效测试状态可进入折扣复核。复核通过率等于通过数除以已完成复核数。' },
    ];
  for (const document of documents) {
    insertDocument({
      projectId: project.id,
      filename: document.filename,
      kind: 'text',
      parseStatus: 'parsed',
      purpose: 'evidence',
      readingMode: 'full',
      textContent: document.textContent,
    });
  }
}

async function seedReviewGroups(projectId: string): Promise<void> {
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.readPackageFiles(projectId);
  const groups = Array.from({ length: 8 }, (_, index) => ({
    id: `review-group-${index + 1}`,
    summary: index === 7 ? '高风险：核对合成折扣资格边界' : `审阅合成资源变化 ${index + 1}`,
    dependsOn: index ? [`review-group-${index}`] : [],
    changes: [{
      kind: 'write' as const,
      path: `package/diagrams/synthetic-review-${index + 1}.mmd`,
      content: `flowchart TD\n  start${index + 1}[合成变化 ${index + 1}] --> review${index + 1}[人工审阅]\n`,
    }],
  }));
  const candidate = await artifacts.createCandidateRevision(projectId, {
    expectedRevision: current.revision.oid,
    groups,
    action: {
      cause: 'acceptance_ui_batch', summary: '建立八组浏览器验收候选',
      actorKind: 'system', actorName: '浏览器验收环境',
    },
  });
  const statement = getDb().prepare(`INSERT OR REPLACE INTO agent_candidate_group_metadata
    (candidate_id, group_id, tier, title, impact_json, warning) VALUES (?,?,?,?,?,?)`);
  for (const [index, group] of groups.entries()) {
    statement.run(
      candidate.id, group.id, index === 7 ? 'caliber' : 'structural', group.summary,
      JSON.stringify([`合成资源 ${index + 1}`]), index === 7 ? '该组改变测试折扣资格边界，需要完整复核。' : null,
    );
  }
}

export async function createAcceptanceScenario(
  owner: string,
  id: AcceptanceScenarioId,
): Promise<{ project: Project; scenarioId: AcceptanceScenarioId }> {
  const timestamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
  const project = createProject({
    owner,
    runtime: 'agent',
    name: `浏览器验收 ${id} ${timestamp}`,
    domain: '合成浏览器验收',
    background: `由可见验收场景页创建的 ${id} 人工合成项目，不包含真实业务数据。`,
  });
  markAcceptanceProject(project.id, owner, id);
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(project.id, {
    id: `browser-acceptance-${id.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${project.id}`,
    name: `浏览器验收 ${id}`,
    description: '只包含人工构造的合成会员、合成订单和测试操作。',
  });
  let baselineRevision = initialized.revision.oid;
  if (!['SCN-GENERATION', 'SCN-LONG'].includes(id)) {
    const saved = await artifacts.savePackageVersion(project.id, {
      expectedRevision: initialized.revision.oid,
      spec: scenarioSpec(id, initialized.spec),
      coverage: initialized.coverage,
      note: `建立 ${id} 合成基线`,
      action: {
        cause: 'acceptance_scenario_seeded', summary: `建立 ${id} 合成基线`,
        actorKind: 'system', actorName: '浏览器验收环境',
      },
    });
    baselineRevision = saved.revision.oid;
  }
  enqueuePackageImportedProjectEvent(project.id, {
    eventId: `acceptance-package-import:${project.id}`,
    baselineRevision,
    packageName: `浏览器验收 ${id}`,
    packageVersion: '1.0.0',
    sourceFilename: 'synthetic_acceptance_package.zip',
    fileCount: 1,
  });
  seedDocuments(project, id);
  if (id === 'SCN-EVALUATION') {
    addTestcases(project.id, [
      { question: '合成会员状态为有效时是否进入折扣复核？', groundTruth: '可以进入折扣资格复核。' },
      { question: '测试状态缺失时如何处理？', groundTruth: '转人工确认。' },
      { question: '复核通过率如何计算？', groundTruth: '复核通过数除以已完成复核数，再乘以 100%。' },
    ]);
    saveProjectDataSource({ projectId: project.id, mode: 'none', workspaceId: `acceptance-${project.id}` });
  }
  if (id === 'SCN-UI-BATCH') await seedReviewGroups(project.id);
  return { project, scenarioId: id };
}
